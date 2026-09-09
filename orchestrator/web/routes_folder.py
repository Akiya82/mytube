"""Folder-wide add: `GET /add-folder/{path:path}`, the confirmation screen
this phase is named after.

Shows a whole season folder as the series it would become -- one title
(new or existing), every episode's own recognised number, one set of
folder-wide track choices, one action for the whole folder -- before
anything is probed beyond the click that opened it and before anything at
all is written. The save this screen's own form points at is a later
plan's own work; this module is the read side only, computing everything
the template renders as plain data.

Every rule `orchestrator.web.routes_share`'s own module docstring states
applies here too, for the identical reason: this route shares an event
loop with `GET /v/{slug}` and `/healthz`, so every filesystem call and
every `ffprobe` this module runs goes through `asyncio.to_thread`, and the
per-file work below (`_process_file`) is additionally bounded by
`MAX_CONCURRENT_FOLDER_PROBES` -- the share is a network mount that also
carries every viewer's playback, and a folder can hold two dozen files.
"""
import asyncio
import shutil
import uuid
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from mytube_media.compat import (
    ProbeError,
    analyze,
    external_audio_track,
    moov_before_mdat,
    probe,
)
from mytube_media.share import ShareEscape, ShareUnavailable, list_dir, relative_to_root
from mytube_media.stereo3d import LAYOUTS as STEREO_LAYOUTS
from mytube_media.subtitles import (
    SUBTITLE_EXTENSIONS,
    external_audio_tracks,
    match_by_episode_number,
    walk_sidecars,
)
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import FolderValidation, plan_slug, required_free_bytes, validate_folder_submission
from orchestrator.recognize import Recognised, normalize_season, recognize_filename
from orchestrator.slugs import SlugError, normalize_slug
from orchestrator.web.routes_share import (
    MIN_VIDEO_FILES_FOR_FOLDER_ADD,
    _breadcrumb,
    _folder_href,
    _is_video_file,
    _resolve_share_path,
    _resolve_sidecar_language,
    _resolve_track_kind_from_folders,
    _unreachable_response,
)
from orchestrator.web.templates import render

router = APIRouter()

# T-05-29: the share is a network mount that also carries every viewer's
# playback, so an unbounded burst of probes over a folder of twenty-six
# files is the wrong default even though a single probe is cheap. Each
# unit of work this bounds is one file's whole share of this route's own
# blocking calls (its probe, its moov scan, its sidecar walks and the
# probe of every external audio candidate they turn up). Widened from the
# preview render's own MAX_CONCURRENT_PREVIEWS=2 (a CPU-bound decode) since
# an ffprobe header read over NFS is I/O-bound, not CPU-bound -- a starting
# figure from 05-RESEARCH.md's own Pattern 4 assumption entry (A3), to be
# measured against a real folder rather than trusted as a permanent
# ceiling.
MAX_CONCURRENT_FOLDER_PROBES = 6
_PROBE_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_FOLDER_PROBES)

# The same two named subfolders `mytube_media.subtitles._NAMED_SUBTITLE_
# FOLDERS` checks before falling back to a full recursive walk -- kept as
# its own tuple here rather than importing that module's private constant,
# since this is the one other call site in the whole project that needs to
# know the shape of a subtitle sidecar search at all.
_SUBTITLE_FOLDER_NAMES = ("Subs", "Subtitles")


def _group_key(recognised: Recognised) -> tuple[str, int | None]:
    """D-152: files group by their own normalised name and season -- a
    normalisation failure (a name that transliterates to nothing usable)
    still has to sort somewhere, so it falls back to a plain lowercase
    compare rather than raising out of a route that has not refused
    anything yet.
    """
    try:
        name_key = normalize_slug(recognised.name)
    except SlugError:
        name_key = recognised.name.strip().lower()
    return name_key, recognised.season


def _group_files(entries, recognised_by_path: dict) -> list[list]:
    order: list[tuple] = []
    groups: dict[tuple, list] = {}
    for entry in entries:
        key = _group_key(recognised_by_path[entry.rel_path])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)
    return [groups[key] for key in order]


def _effective_season(group_recognised: list[Recognised], folder_name: str) -> tuple[int, str]:
    """The season this group's title carries, and where it came from.

    D-152: the folder name is consulted only as the *last* source, when
    none of the group's own files carry a season at all -- every file in
    one group already shares the same season value by construction of
    `_group_key`, so the first non-`None` provenance found is the whole
    group's answer. Defaults to season 1 with provenance `"none"` when
    nothing anywhere names one.
    """
    for recognised in group_recognised:
        if recognised.season is not None:
            return recognised.season, recognised.season_provenance
    _, folder_season, _ = normalize_season(folder_name, None)
    if folder_season is not None:
        return folder_season, "detected_folder"
    return 1, "none"


def _title_name_and_explicit_slug(base_name: str, season: int) -> tuple[str, str | None]:
    """D-150/D-151's season suffix rule: a first season carries no suffix
    even when the release itself names it ("Horimiya" -> "horimiya"), a
    later season carries its number in both the display name and the
    address ("Mushoku Tensei S3" -> "mushoku-tensei-3"). The address
    suffix is a plain `-{season}` appended after the base name's own slug,
    never a re-slugified "Name S3" (which would keep the literal "s" and
    produce "mushoku-tensei-s3" instead).

    Returns `(title_name, explicit_slug)`; `explicit_slug` is `None` for a
    first season, letting the caller's own `plan_slug` derive the address
    from `title_name` exactly as the single-file form already does.
    Raises `SlugError` when `base_name` itself transliterates to nothing
    usable -- the caller answers the same "cannot reach" state a rejected
    path already uses.
    """
    if season and season > 1:
        base_slug = normalize_slug(base_name)
        return f"{base_name} S{season}", f"{base_slug}-{season}"
    return base_name, None


def _matches_default(default_signature, file_signatures: frozenset) -> bool:
    """Whether one file "has" the folder-wide chosen track (D-171): true
    when there is no folder-wide choice to match at all (nothing found
    anywhere, or the default is "no subtitles"), or when this file's own
    signature set contains the chosen one. A signature is an opaque,
    hashable identity -- `("embedded", language, title)` or
    `("external", folder_label)` -- the same shape for audio and
    subtitles, so this one function serves both track-mismatch checks
    (Screen B item 7).
    """
    return default_signature is None or default_signature in file_signatures


async def _process_file(root: Path, abs_path: Path, entry, recognised: Recognised) -> dict:
    """Everything one file's own row needs that only a probe or a
    filesystem walk can answer -- the whole unit of work
    `MAX_CONCURRENT_FOLDER_PROBES` bounds. A failed probe keeps this file's
    row (T-04-01's own rule, reused here): `verdict` is `None`, never a
    raised exception reaching the route.
    """
    async with _PROBE_SEMAPHORE:
        probe_json = None
        try:
            probe_json = await asyncio.to_thread(probe, abs_path)
        except ProbeError:
            pass

        verdict = None
        if probe_json is not None:
            try:
                moov_first = await asyncio.to_thread(moov_before_mdat, abs_path)
            except OSError:
                moov_first = False
            verdict = analyze(probe_json, moov_first=moov_first, filename=entry.name)

        subtitle_candidates = []
        audio_candidates: list[tuple] = []
        if recognised.episode is not None:
            # D-173: paired by the episode number in the sidecar's own
            # filename, the same folder-wide search for both kinds -- a
            # file with no recognised number has no number to pair a
            # sidecar against, so the search does not even run for it.
            try:
                subtitle_candidates = await asyncio.to_thread(
                    walk_sidecars,
                    abs_path,
                    root,
                    extensions=SUBTITLE_EXTENSIONS,
                    folder_names=_SUBTITLE_FOLDER_NAMES,
                    match=match_by_episode_number(recognised.episode),
                )
            except (OSError, TypeError):
                subtitle_candidates = []

            try:
                raw_audio_candidates = await asyncio.to_thread(
                    external_audio_tracks, abs_path, root, recognised.episode
                )
            except (OSError, TypeError):
                raw_audio_candidates = []
            for candidate in raw_audio_candidates:
                folder_label = "/".join(candidate.folder_segments)
                try:
                    probed = await asyncio.to_thread(
                        external_audio_track, candidate.path, folder_label=folder_label
                    )
                except ProbeError:
                    # A dub the walker found but ffprobe cannot read is
                    # skipped, never shown as a choice with nothing behind
                    # it -- the same rule the single-file form already
                    # applies for the identical case.
                    continue
                audio_candidates.append((candidate, probed))

    return {
        "entry": entry,
        "verdict": verdict,
        "probe_failed": probe_json is None,
        "subtitle_candidates": subtitle_candidates,
        "audio_candidates": audio_candidates,
    }


def _build_audio_options(file_results: list[dict]) -> list[dict]:
    """Every distinct audio source found across the whole active group,
    folded by its own signature -- an embedded track identified by its
    language and title, or a sidecar folder identified by its folder
    label (D-171). Order is first-seen, so the folder's own default track
    (almost always the first file's own default) sorts first and becomes
    the folder-wide default choice.
    """
    options: list[dict] = []
    seen: set = set()
    for result in file_results:
        verdict = result["verdict"]
        if verdict is not None:
            for track in verdict.tracks:
                signature = ("embedded", track.language, track.title)
                if signature in seen:
                    continue
                seen.add(signature)
                options.append(
                    {
                        "kind": "embedded",
                        "signature": signature,
                        "language": track.language,
                        "title": track.title,
                    }
                )
        for candidate, probed in result["audio_candidates"]:
            folder_label = probed.folder_label or ""
            signature = ("external", folder_label)
            if signature in seen:
                continue
            seen.add(signature)
            options.append(
                {
                    "kind": "external",
                    "signature": signature,
                    "folder_label": folder_label,
                    "language": _resolve_sidecar_language(
                        probed.language, candidate.suffix, *candidate.folder_segments
                    ),
                    "codec": probed.codec,
                }
            )
    return options


def _build_subtitle_options(file_results: list[dict]) -> list[dict]:
    """The subtitle twin of `_build_audio_options` -- embedded tracks plus
    sidecar folders, folded by signature the same way. Unlike audio, the
    folder-wide default subtitle choice is "no subtitles" (matching the
    single-file form's own opt-in default, D-140): the options built here
    are what the fieldset offers, not what every row is matched against.
    """
    options: list[dict] = []
    seen: set = set()
    for result in file_results:
        verdict = result["verdict"]
        if verdict is not None:
            for track in verdict.subtitle_tracks:
                signature = ("embedded", track.language, track.title)
                if signature in seen:
                    continue
                seen.add(signature)
                options.append(
                    {
                        "kind": "embedded",
                        "signature": signature,
                        "language": track.language,
                        "title": track.title,
                    }
                )
        for candidate in result["subtitle_candidates"]:
            folder_label = "/".join(candidate.folder_segments)
            signature = ("external", folder_label)
            if signature in seen:
                continue
            seen.add(signature)
            options.append(
                {
                    "kind": "external",
                    "signature": signature,
                    "folder_label": folder_label,
                    "language": _resolve_sidecar_language(
                        candidate.suffix, *candidate.folder_segments
                    ),
                    "track_kind": _resolve_track_kind_from_folders(candidate.folder_segments),
                }
            )
    return options


def _file_audio_signatures(result: dict) -> frozenset:
    signatures = set()
    verdict = result["verdict"]
    if verdict is not None:
        for track in verdict.tracks:
            signatures.add(("embedded", track.language, track.title))
    for _candidate, probed in result["audio_candidates"]:
        signatures.add(("external", probed.folder_label or ""))
    return frozenset(signatures)


def _file_subtitle_signatures(result: dict) -> frozenset:
    signatures = set()
    verdict = result["verdict"]
    if verdict is not None:
        for track in verdict.subtitle_tracks:
            signatures.add(("embedded", track.language, track.title))
    for candidate in result["subtitle_candidates"]:
        signatures.add(("external", "/".join(candidate.folder_segments)))
    return frozenset(signatures)


async def _build_rows(conn, root: Path, active_entries: list, recognised_by_path: dict) -> dict:
    """Everything the confirmation screen renders about the active group's
    own files, computed once: the per-file rows, the folder-wide track
    options, and the plan for the title itself. Nothing here writes
    anything.
    """
    abs_paths = {entry.rel_path: root / entry.rel_path for entry in active_entries}
    results_list = await asyncio.gather(
        *(
            _process_file(root, abs_paths[entry.rel_path], entry, recognised_by_path[entry.rel_path])
            for entry in active_entries
        )
    )
    results_by_path = {r["entry"].rel_path: r for r in results_list}

    already_added_rows = await queries.editions_by_source_paths(
        conn, [entry.rel_path for entry in active_entries]
    )
    already_added_by_path = {row["source_path"]: dict(row) for row in already_added_rows}

    # D-160: a moved row takes no part in the folder-wide track choices and
    # is never highlighted -- its edition already exists and is only being
    # repointed, so only a not-yet-added file's own probe feeds the
    # folder-wide options below.
    unmoved_results = [
        r for r in results_list if r["entry"].rel_path not in already_added_by_path
    ]
    audio_options = _build_audio_options(unmoved_results)
    subtitle_options = _build_subtitle_options(unmoved_results)
    default_audio_signature = audio_options[0]["signature"] if audio_options else None
    # D-140: the folder-wide subtitle default is "no subtitles", exactly
    # as the single-file form's own default -- never an assumed burn-in.
    default_subtitle_signature = None

    group_name = recognised_by_path[active_entries[0].rel_path].name
    group_season, season_provenance = _effective_season(
        [recognised_by_path[e.rel_path] for e in active_entries], root.name
    )
    title_name, explicit_slug = _title_name_and_explicit_slug(group_name, group_season)
    slug_plan = await plan_slug(conn, title_name, explicit_slug=explicit_slug)
    title_match = await queries.title_by_current_slug(conn, slug_plan.candidate)
    taken_numbers: set[int] = set()
    if title_match is not None:
        taken_numbers = await queries.taken_episode_numbers(conn, title_match["title_id"])

    # First pass: every fact that does not depend on any other row.
    draft_rows = []
    for entry in active_entries:
        recognised = recognised_by_path[entry.rel_path]
        result = results_by_path[entry.rel_path]
        verdict = result["verdict"]
        already_added = already_added_by_path.get(entry.rel_path)
        moved = already_added is not None

        if moved:
            number = already_added.get("number")
            checked = True  # D-160: "move into this series" is checked by default
        else:
            number = recognised.episode
            checked = number is not None

        highlighted = False
        audio_mismatch = False
        subtitle_mismatch = False
        audio_fallback = []
        subtitle_fallback = []
        if not moved and verdict is not None:
            audio_signatures = _file_audio_signatures(result)
            subtitle_signatures = _file_subtitle_signatures(result)
            audio_mismatch = not _matches_default(default_audio_signature, audio_signatures)
            subtitle_mismatch = not _matches_default(
                default_subtitle_signature, subtitle_signatures
            )
            highlighted = audio_mismatch or subtitle_mismatch
            if audio_mismatch:
                audio_fallback = [
                    o for o in audio_options if o["signature"] in audio_signatures
                ]
            if subtitle_mismatch:
                subtitle_fallback = [
                    o for o in subtitle_options if o["signature"] in subtitle_signatures
                ]

        draft_rows.append(
            {
                "name": entry.name,
                "rel_path": entry.rel_path,
                "number": number,
                "checked": checked,
                "moved": moved,
                "already_added": already_added,
                "verdict": verdict,
                "probe_failed": result["probe_failed"],
                "compatible": bool(verdict is not None and verdict.eligible),
                "reason_key": (
                    "reason.unreadable"
                    if result["probe_failed"]
                    else (verdict.reason_key if verdict is not None else None)
                ),
                "reason_params": verdict.reason_params if verdict is not None else {},
                "highlighted": highlighted,
                "audio_mismatch": audio_mismatch,
                "subtitle_mismatch": subtitle_mismatch,
                "audio_fallback": audio_fallback,
                "subtitle_fallback": subtitle_fallback,
                "number_missing": not moved and number is None,
                "number_taken": (not moved and number is not None and number in taken_numbers),
                "size_bytes": verdict.size_bytes if verdict and verdict.size_bytes else 0,
                # The write side's own submission-preserving state (Task 1,
                # plan 08): defaults here are exactly what the template
                # already rendered before a refusal existed at all --
                # "move" ticked by default (D-160), no fallback pre-picked.
                # A refused POST overrides all three from what the
                # operator actually sent; nothing else here changes.
                "move_checked": True,
                "selected_audio_fallback_index": None,
                "selected_subtitle_fallback_index": None,
            }
        )

    # Second pass: a duplicate number can only be known once every row's
    # own number is on hand.
    checked_numbers = Counter(
        row["number"] for row in draft_rows if row["checked"] and not row["moved"]
    )
    for row in draft_rows:
        row["number_duplicate"] = (
            row["checked"]
            and not row["moved"]
            and row["number"] is not None
            and checked_numbers[row["number"]] > 1
        )

    estimate_size = sum(row["size_bytes"] for row in draft_rows if row["checked"])

    return {
        "rows": draft_rows,
        "audio_options": audio_options,
        "subtitle_options": subtitle_options,
        "default_audio_signature": default_audio_signature,
        "default_subtitle_signature": default_subtitle_signature,
        "title_name": title_name,
        "slug_plan": slug_plan,
        "title_match": title_match,
        "season": group_season,
        "season_provenance": season_provenance,
        "estimate_size_bytes": estimate_size,
        # The write side's own state (Task 2/3): the per-file probe result
        # each row's own audio/subtitle choice is resolved against at save
        # time (`_resolve_audio_selection`/`_resolve_subtitle_selection`),
        # and the numbers already claimed on the target title, read inside
        # the same fetch `row["number_taken"]` above already used -- so a
        # POST re-validates a submitted (possibly hand-edited) number
        # against this same set rather than only the one the GET route
        # happened to recognise.
        "results_by_path": results_by_path,
        "taken_numbers": taken_numbers,
    }


@router.get("/add-folder/{path:path}")
async def folder_confirm_route(request: Request, path: str, group: str = ""):
    settings = get_settings()
    root = Path(settings.share_root)

    # `_resolve_share_path` is the identical `safe_join`-backed containment
    # check every other share route in this project runs -- a second entry
    # point into one mechanism, not a second mechanism (T-05-27).
    try:
        target, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)

    if is_file:
        return _unreachable_response(request, status_code=400)

    try:
        entries = await asyncio.to_thread(list_dir, target, root)
    except ShareUnavailable:
        return _unreachable_response(request, status_code=503)

    video_entries = sorted(
        (e for e in entries if not e.is_dir and _is_video_file(e.name)),
        key=lambda e: e.name.casefold(),
    )
    if len(video_entries) < MIN_VIDEO_FILES_FOR_FOLDER_ADD:
        # The same answer an unopenable path already gives (D-166's own
        # gate, re-checked here since the route is reachable directly by
        # address, not only through the button that already hides itself
        # below this bound) -- never an empty screen.
        return _unreachable_response(request, status_code=400)

    # D-169: recognised once, up front -- every step below (grouping, the
    # season, the title, the number) reads this same result.
    recognised_by_path = {e.rel_path: recognize_filename(e.name) for e in video_entries}

    groups_list = _group_files(video_entries, recognised_by_path)

    # T-05-28: the group-scope query parameter is validated against the
    # groups this request just computed, never used as a filter
    # expression or a path -- a crafted value out of range refuses the
    # same way an unopenable path already does, selecting nothing rather
    # than something unintended.
    active_index = 0
    if group:
        try:
            parsed_index = int(group)
        except ValueError:
            return _unreachable_response(request, status_code=400)
        if not (0 <= parsed_index < len(groups_list)):
            return _unreachable_response(request, status_code=400)
        active_index = parsed_index

    group_summaries = [
        {
            "index": i,
            "title": recognised_by_path[items[0].rel_path].name,
            "count": len(items),
        }
        for i, items in enumerate(groups_list)
    ]
    active_entries = groups_list[active_index]

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            built = await _build_rows(conn, root, active_entries, recognised_by_path)
        except SlugError:
            # A group whose recognised name transliterates to nothing
            # usable (pure emoji/punctuation) -- the same "cannot reach"
            # state the single-file form already answers for the
            # identical case, rather than a raw 500.
            return _unreachable_response(request, status_code=400)

    rows = built["rows"]
    mode = "existing" if built["title_match"] is not None else "new"

    unmoved_rows = [row for row in rows if not row["moved"]]
    all_added = bool(rows) and not unmoved_rows
    nothing_recognized = bool(unmoved_rows) and all(
        row["number"] is None for row in unmoved_rows
    )
    checked_unmoved = [row for row in unmoved_rows if row["checked"]]
    incompatible_checked = [row for row in checked_unmoved if not row["compatible"]]
    all_probes_failed = bool(unmoved_rows) and all(row["probe_failed"] for row in unmoved_rows)

    estimate_seconds = 0.0
    if settings.add_estimate_mb_per_second > 0:
        estimate_seconds = built["estimate_size_bytes"] / (
            settings.add_estimate_mb_per_second * 1024 * 1024
        )

    default_stereo_layout = next(
        (row["verdict"].stereo_layout for row in unmoved_rows if row["verdict"] is not None),
        "2d",
    )

    # T-05-27-adjacent, deferred here by 05-07: the as-is button's own
    # disabled-with-reason state (05-UI-SPEC.md's folder-wide action row
    # table), server-known and unchanged by any checkbox -- the required
    # bytes summed once over the *initially checked* unmoved set, the same
    # `required_free_bytes` formula every other add path shares (D-52). A
    # moved row costs nothing here: it is a repoint, never a scratch write.
    not_enough_space = False
    required_total = sum(
        required_free_bytes(
            row["size_bytes"],
            already_correct=bool(row["verdict"] is not None and row["verdict"].already_correct),
        )
        for row in checked_unmoved
    )
    if required_total:
        free_bytes = await asyncio.to_thread(lambda: shutil.disk_usage(settings.work_dir).free)
        not_enough_space = free_bytes < required_total

    return render(
        request,
        "folder_confirm.html",
        rel_path=rel_path,
        breadcrumb=_breadcrumb(rel_path),
        group_summaries=group_summaries,
        active_index=active_index,
        rows=rows,
        mode=mode,
        title_name=built["title_name"],
        slug_plan=built["slug_plan"],
        title_match=built["title_match"],
        season=built["season"],
        season_provenance_key="folder.season." + built["season_provenance"],
        audio_options=built["audio_options"],
        subtitle_options=built["subtitle_options"],
        audio_choice_index=0 if built["audio_options"] else None,
        subtitle_choice_index=None,
        default_stereo_layout=default_stereo_layout,
        all_added=all_added,
        nothing_recognized=nothing_recognized,
        incompatible_count=len(incompatible_checked),
        all_probes_failed=all_probes_failed,
        not_enough_space=not_enough_space,
        estimate_size_bytes=built["estimate_size_bytes"],
        as_is_is_primary=not incompatible_checked,
        estimate_seconds=estimate_seconds,
        form_action=_folder_href(rel_path),
        default_preview_path=rows[0]["rel_path"] if rows else "",
        refusal_message_key=None,
    )


# --- The save (Task 1/2/3): validation, the write, and the move -----------


def _parse_option_choice(value: str) -> tuple[str, int] | None:
    """`(kind, index)` from a compound `"embedded:0"`/`"external:2"` form
    value -- the folder screen's own grammar for the folder-wide
    `audio_choice`/`subtitle_choice` fields and every per-row
    `audio_fallback_N`/`subtitle_fallback_N` field, all of which name a
    position in a list this same request just rebuilt (`audio_options`,
    `subtitle_options`, or one row's own `audio_fallback`/
    `subtitle_fallback`) rather than an absolute ffprobe stream index --
    unlike the single-file form's `audio_choice`/`subtitle_choice`
    (`orchestrator.media.add.parse_audio_choice`/`parse_subtitle_choice`),
    which this screen does not use, since one file's embedded index can
    differ from another's for the identical language/title signature
    (D-171). `None` for anything malformed; the caller answers its own
    refusal shape.
    """
    if ":" not in value:
        return None
    kind, _, idx_raw = value.partition(":")
    if kind not in ("embedded", "external"):
        return None
    if not idx_raw.isdigit():
        return None
    return kind, int(idx_raw)


def _row_number(form, index: int) -> int | None:
    """The submitted `episode_number_{index}` field, parsed exactly the
    way `routes_titles._parse_episode_number` already parses the single-
    file form's identical field: `None` for blank, negative, or anything
    that is not a plain whole number -- never a guess, never a raise. A
    negative number never reaches this far as a plain string (`"-1"` is
    not `.isdigit()`), so "below zero" and "not a whole number" collapse
    onto the identical `None` outcome the "no number" refusal already
    covers.
    """
    raw = (form.get(f"episode_number_{index}") or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _resolve_audio_selection(
    result: dict, signature: tuple | None
) -> tuple[str, int | None, str | None, int | None] | None:
    """`(kind, stream_index, external_path, channels)` for `signature` as
    it exists in *this one file's* own probe result -- the write-time twin
    of `_file_audio_signatures`/`_build_audio_options`: the same
    (`"embedded"`, language, title) or (`"external"`, folder_label)
    identity that matches across files by D-171's own rule can sit at a
    different absolute stream index, or in a different sidecar file,
    file to file, so resolving "which track" always happens against this
    file's own probe, never the folder-wide option list directly.

    `external_path` is the absolute, already-contained path
    `mytube_media.subtitles.external_audio_tracks`/`walk_sidecars` already
    resolved (T-04-01) -- the caller still turns it into a share-relative
    string via `relative_to_root` before it reaches a job row. Returns
    `None` when `signature` is `None` (no folder-wide choice at all) or
    cannot be found in this file's own tracks/candidates at all -- the
    caller falls back to the file's own first embedded track in that case,
    the same "something must be selected" floor the single-file form's own
    `audio_choice` gives (D-42: never optional).
    """
    if signature is None:
        return None
    kind = signature[0]
    if kind == "embedded":
        _, language, title = signature
        verdict = result["verdict"]
        if verdict is not None:
            for track in verdict.tracks:
                if (track.language, track.title) == (language, title):
                    return "embedded", track.index, None, track.channels
    else:
        _, folder_label = signature
        for _candidate, probed in result["audio_candidates"]:
            if (probed.folder_label or "") == folder_label:
                return "external", None, probed.external_path, probed.channels
    return None


def _fallback_audio_selection(result: dict) -> tuple[str, int | None, str | None, int | None]:
    """The floor `_resolve_audio_selection` falls back to when a
    signature cannot be found in this file at all: this file's own first
    embedded track, since an audio choice is never optional (D-42) and a
    file with no embedded track at all could never have been marked
    compatible to begin with.
    """
    verdict = result["verdict"]
    if verdict is not None and verdict.tracks:
        track = verdict.tracks[0]
        return "embedded", track.index, None, track.channels
    return "embedded", 0, None, None


def _resolve_subtitle_selection(
    result: dict, signature: tuple | None
) -> tuple[str, int | None, str | None]:
    """The subtitle twin of `_resolve_audio_selection`: `(kind,
    stream_index, external_path)`, `("none", None, None)` when `signature`
    is `None` (no burn-in chosen, D-140's own default) or cannot be found
    in this file's own embedded/sidecar candidates at all.
    """
    if signature is None:
        return "none", None, None
    kind = signature[0]
    if kind == "embedded":
        _, language, title = signature
        verdict = result["verdict"]
        if verdict is not None:
            for track in verdict.subtitle_tracks:
                if (track.language, track.title) == (language, title):
                    return "embedded", track.stream_index, None
    else:
        _, folder_label = signature
        for candidate in result["subtitle_candidates"]:
            if "/".join(candidate.folder_segments) == folder_label:
                return "external", None, str(candidate.path)
    return "none", None, None


def _render_refused_folder_screen(
    request: Request,
    *,
    rel_path: str,
    built: dict,
    resolved_rows: list[dict],
    mode: str,
    submitted_name: str,
    submitted_slug: str,
    audio_choice_index: int | None,
    subtitle_choice_index: int | None,
    stereo_layout: str,
    validation: FolderValidation,
    active_index: int,
    group_summaries: list[dict],
):
    """The refusal half of Screen B item 10: the identical screen the GET
    route renders, with every value the operator actually submitted --
    every tick, every typed number, every folder-wide and per-row track
    choice -- carried back in place of what `_build_rows` freshly
    recognised, plus the one save-blocked sentence naming what to fix.
    Nothing here is re-probed a second time within this same request; only
    the render-time captions (`number_missing`/`number_duplicate`/
    `number_taken`) are recomputed, against the submission rather than the
    recognised defaults, exactly as they already render unconditionally
    (not gated on `highlighted`) in `partials/folder_file_row.html`.
    """
    # A submitted "existing" mode naming a target this fresh read no
    # longer confirms (a stale page, or a crafted target) has nothing
    # left to show as the matched title -- the screen falls back to "new
    # title" mode for rendering only, the same state a title-match-free
    # folder already renders in, rather than a template reaching through
    # a None.
    if mode == "existing" and built["title_match"] is None:
        mode = "new"

    ticked_numbers = Counter(r["number"] for r in resolved_rows if r["ticked"])

    rows = []
    for original, resolved in zip(built["rows"], resolved_rows):
        row = dict(original)
        moved = row["moved"]
        row["checked"] = resolved["ticked"] if not moved else row["checked"]
        row["move_checked"] = resolved["ticked"] if moved else row["move_checked"]
        row["number"] = resolved["number"]
        row["number_missing"] = resolved["ticked"] and resolved["number"] is None
        row["number_duplicate"] = (
            resolved["ticked"]
            and resolved["number"] is not None
            and ticked_numbers[resolved["number"]] > 1
        )
        row["number_taken"] = resolved["number_taken"]
        row["selected_audio_fallback_index"] = resolved.get("selected_audio_fallback_index")
        row["selected_subtitle_fallback_index"] = resolved.get("selected_subtitle_fallback_index")
        rows.append(row)

    unmoved_rows = [row for row in rows if not row["moved"]]
    all_added = bool(rows) and not unmoved_rows
    nothing_recognized = bool(unmoved_rows) and all(row["number"] is None for row in unmoved_rows)
    checked_unmoved = [row for row in unmoved_rows if row["checked"]]
    incompatible_checked = [row for row in checked_unmoved if not row["compatible"]]
    all_probes_failed = bool(unmoved_rows) and all(row["probe_failed"] for row in unmoved_rows)

    return render(
        request,
        "folder_confirm.html",
        rel_path=rel_path,
        breadcrumb=_breadcrumb(rel_path),
        group_summaries=group_summaries,
        active_index=active_index,
        rows=rows,
        mode=mode,
        title_name=submitted_name or built["title_name"],
        slug_plan=type(built["slug_plan"])(
            candidate=built["slug_plan"].candidate,
            chosen=submitted_slug or built["slug_plan"].chosen,
            similar=built["slug_plan"].similar,
            is_tombstone=built["slug_plan"].is_tombstone,
        ),
        title_match=built["title_match"],
        season=built["season"],
        season_provenance_key="folder.season." + built["season_provenance"],
        audio_options=built["audio_options"],
        subtitle_options=built["subtitle_options"],
        audio_choice_index=audio_choice_index,
        subtitle_choice_index=subtitle_choice_index,
        default_stereo_layout=stereo_layout,
        all_added=all_added,
        nothing_recognized=nothing_recognized,
        incompatible_count=len(incompatible_checked),
        all_probes_failed=all_probes_failed,
        not_enough_space=validation.message_key == "reason.storage_full",
        estimate_size_bytes=sum(row["size_bytes"] for row in rows if row["checked"]),
        as_is_is_primary=not incompatible_checked,
        estimate_seconds=0.0,
        form_action=_folder_href(rel_path),
        default_preview_path=rows[0]["rel_path"] if rows else "",
        refusal_message_key=validation.message_key,
    )


@router.post("/add-folder/{path:path}")
async def folder_confirm_submit_route(request: Request, path: str):
    """The write this whole screen exists for (Task 2/3): validate the
    operator's submission against the folder's own state read fresh
    inside this request (T-05-33 -- never against what the GET route
    computed minutes ago), then either write the whole folder -- every new
    episode, every moved episode, and one ordinary job per new edition --
    in a single transaction under the address-claim lock, or refuse and
    re-render the identical screen with every choice preserved
    (`_render_refused_folder_screen`).

    Re-derives the containment check, the video listing, the recognition
    pass, the grouping, and the whole of `_build_rows` exactly as the GET
    route does -- nothing about the folder is trusted from a page that may
    have been open for minutes; only the operator's own ticks, numbers and
    track choices come from this request's own form body.
    """
    settings = get_settings()
    root = Path(settings.share_root)

    try:
        target, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)
    if is_file:
        return _unreachable_response(request, status_code=400)

    try:
        entries = await asyncio.to_thread(list_dir, target, root)
    except ShareUnavailable:
        return _unreachable_response(request, status_code=503)

    video_entries = sorted(
        (e for e in entries if not e.is_dir and _is_video_file(e.name)),
        key=lambda e: e.name.casefold(),
    )
    if len(video_entries) < MIN_VIDEO_FILES_FOR_FOLDER_ADD:
        return _unreachable_response(request, status_code=400)

    recognised_by_path = {e.rel_path: recognize_filename(e.name) for e in video_entries}
    groups_list = _group_files(video_entries, recognised_by_path)

    form = await request.form()

    group_raw = form.get("group", "0")
    try:
        active_index = int(group_raw)
    except ValueError:
        return _unreachable_response(request, status_code=400)
    if not (0 <= active_index < len(groups_list)):
        return _unreachable_response(request, status_code=400)
    active_entries = groups_list[active_index]

    group_summaries = [
        {
            "index": i,
            "title": recognised_by_path[items[0].rel_path].name,
            "count": len(items),
        }
        for i, items in enumerate(groups_list)
    ]

    action = "convert" if form.get("convert") is not None else "add"
    is_convert = action == "convert"

    stereo_layout = form.get("stereo_layout", "2d")
    if stereo_layout not in STEREO_LAYOUTS:
        return _unreachable_response(request, status_code=400)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            built = await _build_rows(conn, root, active_entries, recognised_by_path)
        except SlugError:
            return _unreachable_response(request, status_code=400)

        submitted_title_mode = form.get("title_mode", "")
        mode = (
            submitted_title_mode
            if submitted_title_mode in ("new", "existing")
            else ("existing" if built["title_match"] is not None else "new")
        )
        # T-05-36: a crafted (or stale) existing-title mode naming a title
        # that is no longer real or no longer active refuses -- checked
        # against what was just read, not what the operator's page showed.
        submitted_target_title_id = form.get("target_title_id", "")
        target_ok = True
        if mode == "existing":
            target_ok = (
                built["title_match"] is not None
                and str(built["title_match"]["title_id"]) == submitted_target_title_id
            )

        submitted_name = form.get("name", "").strip()
        submitted_slug = form.get("slug", "").strip()

        # T-05-33: the folder-wide audio/subtitle choice is re-derived from
        # the submission and re-checked against the option list this same
        # request just rebuilt -- an index out of range only ever comes
        # from a crafted request, refused outright rather than clamped.
        audio_choice_raw = form.get("audio_choice", "")
        default_audio_signature = built["default_audio_signature"]
        audio_choice_index: int | None = 0 if built["audio_options"] else None
        if audio_choice_raw:
            parsed = _parse_option_choice(audio_choice_raw)
            if (
                parsed is None
                or not (0 <= parsed[1] < len(built["audio_options"]))
                or built["audio_options"][parsed[1]]["kind"] != parsed[0]
            ):
                return _unreachable_response(request, status_code=400)
            audio_choice_index = parsed[1]
            default_audio_signature = built["audio_options"][audio_choice_index]["signature"]

        subtitle_choice_raw = form.get("subtitle_choice", "none")
        default_subtitle_signature = None
        subtitle_choice_index: int | None = None
        if subtitle_choice_raw not in ("none", ""):
            parsed = _parse_option_choice(subtitle_choice_raw)
            if (
                parsed is None
                or not (0 <= parsed[1] < len(built["subtitle_options"]))
                or built["subtitle_options"][parsed[1]]["kind"] != parsed[0]
            ):
                return _unreachable_response(request, status_code=400)
            subtitle_choice_index = parsed[1]
            default_subtitle_signature = built["subtitle_options"][subtitle_choice_index][
                "signature"
            ]

        # D-140: a stereo source refuses a burn-in outright, the identical
        # check the single-file form already makes, folder-wide here.
        if stereo_layout != "2d" and default_subtitle_signature is not None:
            return _unreachable_response(request, status_code=400)

        results_by_path = built["results_by_path"]
        resolved_rows = []
        for i, row in enumerate(built["rows"]):
            rel = row["rel_path"]
            moved = row["moved"]
            if moved:
                ticked = form.get(f"move_{i}") is not None
            else:
                ticked = form.get(f"include_{i}") is not None

            number = _row_number(form, i)
            number_taken = (
                not moved
                and number is not None
                and mode == "existing"
                and number in built["taken_numbers"]
            )

            chosen_audio_signature = default_audio_signature
            audio_resolved = True
            selected_audio_fallback_index = None
            if not moved and row["audio_mismatch"]:
                raw = form.get(f"audio_fallback_{i}")
                if not raw:
                    audio_resolved = not ticked
                else:
                    parsed = _parse_option_choice(raw)
                    fallback = row["audio_fallback"]
                    if (
                        parsed is None
                        or not (0 <= parsed[1] < len(fallback))
                        or fallback[parsed[1]]["kind"] != parsed[0]
                    ):
                        return _unreachable_response(request, status_code=400)
                    selected_audio_fallback_index = parsed[1]
                    chosen_audio_signature = fallback[parsed[1]]["signature"]

            chosen_subtitle_signature = default_subtitle_signature
            subtitle_resolved = True
            selected_subtitle_fallback_index = None
            if not moved and row["subtitle_mismatch"]:
                raw = form.get(f"subtitle_fallback_{i}")
                if not raw:
                    subtitle_resolved = not ticked
                elif raw == "none":
                    chosen_subtitle_signature = None
                    selected_subtitle_fallback_index = "none"
                else:
                    parsed = _parse_option_choice(raw)
                    fallback = row["subtitle_fallback"]
                    if (
                        parsed is None
                        or not (0 <= parsed[1] < len(fallback))
                        or fallback[parsed[1]]["kind"] != parsed[0]
                    ):
                        return _unreachable_response(request, status_code=400)
                    selected_subtitle_fallback_index = parsed[1]
                    chosen_subtitle_signature = fallback[parsed[1]]["signature"]

            resolved_rows.append(
                {
                    "index": i,
                    "rel_path": rel,
                    "moved": moved,
                    "ticked": ticked,
                    "number": number,
                    "number_taken": number_taken,
                    "compatible": row["compatible"],
                    "highlighted": row["highlighted"],
                    "audio_resolved": audio_resolved,
                    "subtitle_resolved": subtitle_resolved,
                    "audio_signature": chosen_audio_signature,
                    "subtitle_signature": chosen_subtitle_signature,
                    "selected_audio_fallback_index": selected_audio_fallback_index,
                    "selected_subtitle_fallback_index": selected_subtitle_fallback_index,
                    "already_added": row["already_added"],
                    "size_bytes": row["size_bytes"],
                    "verdict": row["verdict"],
                }
            )

        validation = validate_folder_submission(
            mode=mode,
            target_ok=target_ok,
            action=action,
            rows=[
                {
                    "index": r["index"],
                    "ticked": r["ticked"],
                    "number": r["number"],
                    "moved": r["moved"],
                    "compatible": r["compatible"],
                    "highlighted": r["highlighted"],
                    "audio_resolved": r["audio_resolved"],
                    "subtitle_resolved": r["subtitle_resolved"],
                    "number_taken": r["number_taken"],
                }
                for r in resolved_rows
            ],
        )

        if not validation.ok:
            return _render_refused_folder_screen(
                request,
                rel_path=rel_path,
                built=built,
                resolved_rows=resolved_rows,
                mode=mode,
                submitted_name=submitted_name,
                submitted_slug=submitted_slug,
                audio_choice_index=audio_choice_index,
                subtitle_choice_index=subtitle_choice_index,
                stereo_layout=stereo_layout,
                validation=validation,
                active_index=active_index,
                group_summaries=group_summaries,
            )

        new_rows = [r for r in resolved_rows if r["ticked"] and not r["moved"]]
        move_rows = [r for r in resolved_rows if r["ticked"] and r["moved"]]

        # The as-is free-space precheck, applied once over the whole
        # ticked-and-new set (D-52) -- unchanged by 04-22's own decision
        # that conversion carries no interface-side precheck of its own.
        # Reachable here only by a request bypassing the already-disabled
        # button the GET render shows for the identical condition, so a
        # plain refusal is answered rather than a friendly re-render.
        if action == "add":
            required_total = 0
            for r in new_rows:
                verdict = r["verdict"]
                already_correct = bool(verdict is not None and verdict.already_correct)
                selection = _resolve_audio_selection(
                    results_by_path[r["rel_path"]], r["audio_signature"]
                )
                if selection is not None and selection[0] == "external":
                    already_correct = False
                required_total += required_free_bytes(
                    r["size_bytes"], already_correct=already_correct
                )
            if required_total:
                free_bytes = await asyncio.to_thread(
                    lambda: shutil.disk_usage(settings.work_dir).free
                )
                if free_bytes < required_total:
                    return _unreachable_response(request, status_code=400)

        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
            )

            edition_specs = []
            for r in new_rows:
                result = results_by_path[r["rel_path"]]
                edition_id = uuid.uuid4()
                object_key = f"{edition_id}.mp4"
                selection = _resolve_audio_selection(result, r["audio_signature"])
                if selection is None:
                    selection = _fallback_audio_selection(result)
                audio_kind, audio_index, audio_external, audio_channels = selection
                external_audio_rel_path = (
                    relative_to_root(root, Path(audio_external))
                    if audio_kind == "external" and audio_external
                    else None
                )
                r["audio_stream_index"] = audio_index if audio_kind == "embedded" else None
                r["external_audio_rel_path"] = external_audio_rel_path
                edition_specs.append(
                    {
                        "number": r["number"],
                        "edition_id": edition_id,
                        "object_key": object_key,
                        "source_path": r["rel_path"],
                        "audio_stream_index": r["audio_stream_index"],
                        "audio_channels": audio_channels,
                        "stereo_layout": None if is_convert else stereo_layout,
                    }
                )

            if mode == "new":
                title_id, _episode_ids = await queries.create_title_with_episodes(
                    conn,
                    submitted_name or built["title_name"],
                    submitted_slug or built["slug_plan"].chosen,
                    edition_specs,
                    season_number=built["season"],
                )
            else:
                title_id = built["title_match"]["title_id"]
                if edition_specs:
                    await queries.attach_episodes_to_title(conn, title_id, edition_specs)

            for r in move_rows:
                already_added = r["already_added"]
                await queries.reparent_edition_to_episode(
                    conn,
                    edition_id=already_added["edition_id"],
                    old_title_id=already_added["title_id"],
                    old_episode_id=already_added["episode_id"],
                    old_slug=already_added["slug"],
                    target_title_id=title_id,
                    number=r["number"],
                )

            # One ordinary job per new edition (D-119) -- a moved row
            # enqueues nothing, nothing is being processed for it, only
            # repointed.
            for r, spec in zip(new_rows, edition_specs):
                if is_convert:
                    result = results_by_path[r["rel_path"]]
                    sub_kind, sub_index, sub_path = _resolve_subtitle_selection(
                        result, r["subtitle_signature"]
                    )
                    subtitle_path = (
                        relative_to_root(root, Path(sub_path)) if sub_path else None
                    )
                    await queries.enqueue_job(
                        conn,
                        kind="transcode",
                        title_id=title_id,
                        edition_id=spec["edition_id"],
                        source_path=r["rel_path"],
                        audio_stream_index=r["audio_stream_index"],
                        subtitle_kind=sub_kind,
                        subtitle_stream_index=sub_index,
                        subtitle_path=subtitle_path,
                        stereo_layout=stereo_layout,
                        external_audio_path=r["external_audio_rel_path"],
                    )
                else:
                    await queries.enqueue_job(
                        conn,
                        kind="remux",
                        title_id=title_id,
                        edition_id=spec["edition_id"],
                        source_path=r["rel_path"],
                        audio_stream_index=r["audio_stream_index"],
                        external_audio_path=r["external_audio_rel_path"],
                    )

            await queries.touch_title_changed(conn, title_id)

    return RedirectResponse("/library", status_code=302)
