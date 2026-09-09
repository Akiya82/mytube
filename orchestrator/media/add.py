"""The slug-planning sequence the CLI and the web add form both call
(D-45..D-48), the one free-space formula both the add form and the future
background worker call (D-52), and the one `subtitle_choice` form-field
parser both the preview route and the convert POST branch call (so the two
never grow two slightly different ideas of what a submitted subtitle
choice means). Extracted so every caller shares one implementation instead
of two that drift.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath

import asyncpg

from mytube_media.subtitles import embedded_subtitle_kind as embedded_subtitle_kind
from orchestrator.db import queries
from orchestrator.slugs import (
    SlugError,
    normalize_edition_slug,
    normalize_slug,
    pick_unique_slug,
)

# D-52/D-72: the doubled space +faststart's rewrite needs, plus the small,
# fixed scratch budget a poster frame costs (one JPEG, D-72) -- see
# required_free_bytes below for why this budget is added in both branches
# rather than only the ffmpeg one.
POSTER_SCRATCH_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class SlugPlan:
    """The four facts the CLI's `_add` already derives today, extracted
    verbatim so the web add form derives the exact same four facts instead
    of a second, subtly different sequence.
    """

    candidate: str
    chosen: str
    similar: list[str]
    is_tombstone: bool


async def plan_slug(
    conn: asyncpg.Connection, name: str, explicit_slug: str | None = None
) -> SlugPlan:
    """Normalize `name` (or `explicit_slug`, when given) into a candidate
    slug, then resolve the CLI's `_add` sequence: tombstone check on the
    raw candidate, then either the tombstone-reuse branch or collision
    suffixing -- never both, and always in that order.

    Raises `SlugError` (from `normalize_slug`) exactly as the CLI's own
    `_candidate_slug` does today -- this function does not catch it, so
    each caller decides how to surface it: the CLI turns it into a
    `typer.Exit(1)`, the web route into whatever its own error state is.

    **The tombstone check MUST run on the raw candidate before any
    auto-suffixing.** `taken_slugs()` (used below by `pick_unique_slug`)
    deliberately includes tombstoned slugs, so if this check ran after
    suffixing, a tombstoned candidate would just get quietly bumped to
    "-2" and the reuse-confirmation path would never fire. This ordering
    is load-bearing, not incidental -- see `cli/__main__.py`'s own history
    of `test_add_on_tombstoned_slug_without_flag_refuses` (Rule 1) for the
    bug this exact ordering was written to prevent the first time.
    """
    candidate = normalize_slug(explicit_slug) if explicit_slug else normalize_slug(name)

    existing = await queries.resolve_slug(conn, candidate)
    is_tombstone = existing is not None and existing["status"] != "active"

    if is_tombstone:
        # Explicit, not an optimisation: taken_slugs() includes tombstoned
        # slugs on purpose (it is what makes reuse detectable at all), so
        # handing this candidate to pick_unique_slug would come back
        # "-2" with the original listed as "similar" -- the caller asked
        # to reuse `candidate`; it would silently receive a different
        # address plus a warning that never used to appear. The current
        # behavior is correct by intent; dropping these two explicit
        # assignments turns it into a bug no test written against a
        # substring match would ever report (see cli/__main__.py's own
        # comment on this exact failure mode).
        return SlugPlan(candidate=candidate, chosen=candidate, similar=[], is_tombstone=True)

    taken = await queries.taken_slugs(conn)
    chosen, similar = pick_unique_slug(candidate, taken)
    return SlugPlan(candidate=candidate, chosen=chosen, similar=similar, is_tombstone=False)


def required_free_bytes(source_size: int, *, already_correct: bool) -> int:
    """The free-space rule (D-52), defined once here -- both the add form
    and the future worker call this function; neither restates the
    arithmetic.

    The source and the ffmpeg output do **not** coexist in scratch: the
    source lives on the read-only share and is never copied there. What
    doubles is ffmpeg's own `-movflags +faststart` rewrite, which produces
    its output in full before the input is fully consumed -- the doubling
    is a safe margin for that rewrite, not a second copy of the input.

    - **Not already correct:** `2 * source_size + POSTER_SCRATCH_BYTES`.
    - **Already correct (D-44):** `POSTER_SCRATCH_BYTES` alone. This
      branch never invokes ffmpeg on the video and uploads straight from
      the share; the only byte it writes to scratch is one poster JPEG.
      Demanding double the source's size to upload a file that touches
      scratch for a thumbnail would refuse the one path that costs
      almost nothing.
    """
    if already_correct:
        return POSTER_SCRATCH_BYTES
    return 2 * source_size + POSTER_SCRATCH_BYTES


# --- the subtitle_choice form field (D-121, D-122, D-140) ------------------

# The vocabulary a submitted subtitle_choice value may carry: no burn-in
# at all, an embedded stream picked by its absolute ffprobe index (text
# and bitmap both -- the different rendering path is not the operator's
# choice to encode), or a sidecar file named by its share-relative path
# (whether it was one this form found on its own or one the operator
# typed by hand -- both resolve to the same shape by the time they reach
# a route, see parse_subtitle_choice below).
_SUBTITLE_KIND_NONE = "none"
_SUBTITLE_KIND_EMBEDDED_PREFIX = "embedded:"
_SUBTITLE_KIND_EXTERNAL_PREFIX = "external:"
_SUBTITLE_KIND_MANUAL = "manual"


class SubtitleChoiceRefused(Exception):
    """`subtitle_choice` (or the manual path beside it) is malformed --
    never a catalog key shown to the operator, just a signal for the
    caller to answer its own refusal shape.
    """


def parse_subtitle_choice(
    value: str, *, manual_path: str, rel_path: str
) -> tuple[str, int | None, str | None]:
    """`(kind, stream_index, path)` from the add form's single
    `subtitle_choice` radio value -- `kind` is one of `"none"`,
    `"embedded"` or `"external"`; `path` is always a share-relative
    string, never an absolute one and never trusted un-contained -- the
    caller still has to resolve it through `mytube_media.share.safe_join`
    before it touches the filesystem (T-04-01), this function only shapes
    the value.

    Raises `SubtitleChoiceRefused` for anything malformed.
    """
    if value == _SUBTITLE_KIND_NONE or not value:
        return "none", None, None

    if value.startswith(_SUBTITLE_KIND_EMBEDDED_PREFIX):
        raw_index = value[len(_SUBTITLE_KIND_EMBEDDED_PREFIX):]
        try:
            return "embedded", int(raw_index), None
        except ValueError:
            raise SubtitleChoiceRefused() from None

    if value.startswith(_SUBTITLE_KIND_EXTERNAL_PREFIX):
        rel = value[len(_SUBTITLE_KIND_EXTERNAL_PREFIX):]
        if not rel:
            raise SubtitleChoiceRefused()
        return "external", None, rel

    if value == _SUBTITLE_KIND_MANUAL:
        if not manual_path:
            raise SubtitleChoiceRefused()
        # Relative to the source's own folder (the copywriting contract's
        # own placeholder text says so) -- joined onto the source's
        # share-relative directory, not onto the share root directly, so
        # "../Subs/movie.srt" reaches a sibling folder the way an operator
        # typing it beside the source file expects.
        combined = (PurePosixPath(rel_path).parent / manual_path).as_posix()
        return "external", None, combined

    raise SubtitleChoiceRefused()


# `embedded_subtitle_kind` used to be defined here and is now imported at
# the top of this module: the converter needs the same resolution and
# imports nothing from `orchestrator`, so it moved down to
# `mytube_media.subtitles`, beside the enumeration it walks. The name is
# still exported from here, which is where the rest of the service
# already reaches for it.


# --- the audio_choice form field (D-180, D-181, T-05-13) -------------------

_AUDIO_KIND_EMBEDDED_PREFIX = "embedded:"
_AUDIO_KIND_EXTERNAL_PREFIX = "external:"


class AudioChoiceRefused(Exception):
    """`audio_choice` is malformed -- never a catalog key shown to the
    operator, just a signal for the caller to answer its own refusal
    shape, the same role `SubtitleChoiceRefused` already plays.
    """


def parse_audio_choice(value: str) -> tuple[str, int | None, str | None]:
    """`(kind, stream_index, path)` from the add form's single
    `audio_choice` radio value -- `kind` is one of `"embedded"` or
    `"external"`; there is no `"none"` (an audio choice is never optional,
    D-42) and no `"manual"` (a hand-typed audio path is joined into an
    `external:` value by the form's own script before it ever reaches this
    parser -- see static/app.js -- so this grammar stays exactly two
    shapes wide).

    `path` is always a share-relative string when `kind` is `"external"`,
    never an absolute one and never trusted un-contained -- the caller
    still has to resolve it through `mytube_media.share.safe_join` before
    it touches the filesystem (T-05-13), this function only shapes the
    value and performs no filesystem access of its own.

    Raises `AudioChoiceRefused` for anything malformed: no recognised
    prefix, an embedded index that is not a plain integer, an external
    path that is empty, or an external path that is absolute (a leading
    `/` or a Windows drive letter) -- an absolute path could only ever
    have been crafted by hand, since every path this form itself ever
    emits is share-relative.
    """
    if value.startswith(_AUDIO_KIND_EMBEDDED_PREFIX):
        raw_index = value[len(_AUDIO_KIND_EMBEDDED_PREFIX):]
        try:
            return "embedded", int(raw_index), None
        except ValueError:
            raise AudioChoiceRefused() from None

    if value.startswith(_AUDIO_KIND_EXTERNAL_PREFIX):
        rel = value[len(_AUDIO_KIND_EXTERNAL_PREFIX):]
        if not rel:
            raise AudioChoiceRefused()
        posix = PurePosixPath(rel)
        if posix.is_absolute() or (len(rel) > 1 and rel[1] == ":"):
            raise AudioChoiceRefused()
        return "external", None, rel

    raise AudioChoiceRefused()


# --- the edition slug (D-177, D-178, D-179) ---------------------------------


class EditionSlugTaken(Exception):
    """Raised when the chosen edition slug already names another edition
    of the same episode -- D-177's uniqueness scope, checked inside the
    caller's own locked transaction.
    """


def suggest_edition_slug(
    *,
    existing: dict,
    candidate: dict,
    taken_audio_languages: frozenset[str] = frozenset(),
) -> str:
    """D-177: the first real difference between `candidate` (what the add
    or convert action about to run would produce) and `existing` (the
    episode's current default edition), checked in this fixed order:
    audio language, resolution, subtitles, conversion. Both dicts describe
    one edition each through the same keys -- `audio_language`,
    `audio_studio_folder` (set only for an external dub), `width`,
    `height`, `subtitle_language`, `subtitle_signs_only`, `is_transcode`,
    `video_codec` -- built by the caller from whatever it already knows (a
    fresh probe, a stored edition row); a key either dict omits is treated
    as unknown/absent, never as a match. This function performs no lookups
    of its own and is pure.

    Worked examples (D-177): a Russian dub against a Japanese default ->
    "dub-ru"; a second Russian dub from a different studio, once
    `taken_audio_languages` already carries "ru" -> "dub-ru-<studio-slug>"
    ("dub-ru-dublyazhnaya"); a 720p output against a 1080p default ->
    "720p"; a Russian subtitle burn against an unsubtitled default ->
    "sub-ru", or "sub-ru-signs" when it resolved signs-only; a conversion
    to h264 against an as-is (never-transcoded) default -> "h264".
    Nothing differing -> "", the "type your own" case D-177 asks for.
    """
    candidate_language = candidate.get("audio_language")
    if candidate_language and candidate_language != existing.get("audio_language"):
        slug = f"dub-{candidate_language}"
        studio = candidate.get("audio_studio_folder")
        if candidate_language in taken_audio_languages and studio:
            try:
                slug += f"-{normalize_slug(studio)}"
            except SlugError:
                pass
        return slug

    candidate_height = candidate.get("height")
    if candidate_height and (candidate.get("width"), candidate_height) != (
        existing.get("width"),
        existing.get("height"),
    ):
        return f"{candidate_height}p"

    candidate_subtitle = candidate.get("subtitle_language")
    if candidate_subtitle and candidate_subtitle != existing.get("subtitle_language"):
        slug = f"sub-{candidate_subtitle}"
        if candidate.get("subtitle_signs_only"):
            slug += "-signs"
        return slug

    if (
        candidate.get("is_transcode")
        and not existing.get("is_transcode")
        and candidate.get("video_codec")
    ):
        return candidate["video_codec"]

    return ""


async def plan_edition_slug(conn: asyncpg.Connection, episode_id, explicit_slug: str) -> str:
    """Validate a submitted edition-slug the way `plan_slug` validates a
    title slug -- through the shape rule (`normalize_edition_slug`, D-177's
    not-all-digits refusal, which also refuses a blank string exactly as
    `normalize_slug` already does for an empty title name) -- and, unlike
    a title slug, a plain uniqueness refusal rather than an auto-suffix:
    D-179 leaves no room for an edition slug to silently become a
    different string than the one the operator saw and typed.

    Raises `SlugError`/`EditionSlugRefused` (shape) or `EditionSlugTaken`
    (collision). Must be called inside the caller's own locked
    transaction (the same `SLUG_CLAIM_LOCK_KEY` `create_title_with_edition`
    already documents), since the uniqueness check below is a plain read
    with nothing else serialising it.
    """
    chosen = normalize_edition_slug(explicit_slug)
    if await queries.edition_slug_taken_on_episode(conn, episode_id, chosen):
        raise EditionSlugTaken(chosen)
    return chosen
