"""Share browsing: GET /, GET /add/{path:path}, GET /lang/{code}.

Every request path that turns into a filesystem call goes through
`safe_join` first (T-02-01) -- a rejected path renders the
"cannot reach the share" state, never a raw exception page. Breadcrumb and
row links are always built here, from the server-validated relative path,
never by echoing the raw request path back into a link (T-02-01).

The file branch of `share_listing_route` is where `ffprobe` runs (D-56):
once, when the add form opens, and never while drawing a listing. The
directory branch never imports or calls anything from
`mytube_media.compat` for exactly that reason.

**Nothing synchronous is called bare from this module either** -- the rule
`orchestrator.media.queue`'s docstring states, for the same reason and on
the same event loop. `ffprobe` is a subprocess with a 30-second timeout,
the atom scan walks headers over a network mount with no timeout at all,
and a directory scan is one round trip per entry: run bare, one slow file
on the share freezes video resolution and `/healthz` for every viewer
while the add form draws. Every filesystem and subprocess call here goes
through `asyncio.to_thread`.
"""
import asyncio
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from mytube_media.compat import (
    DURATION_MISMATCH_THRESHOLD_SECONDS,
    ProbeError,
    analyze,
    duration_gap_seconds,
    external_audio_track,
    moov_before_mdat,
    probe,
)
from mytube_media.subtitles import external_audio_tracks, external_tracks
from mytube_media.share import (
    ShareEscape,
    ShareUnavailable,
    list_dir,
    relative_to_root,
    safe_join,
)
from orchestrator import auth
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.language import language_from_tokens, resolve_track_kind
from orchestrator.media.add import SlugPlan, plan_slug, required_free_bytes
from orchestrator.recognize import recognize_filename
from orchestrator.slugs import SlugError
from orchestrator.web.templates import LOCALES, render

router = APIRouter()

# T-05-14's closed vocabulary for the sidecar picker route -- a kind
# outside this set is refused outright rather than defaulting to one of
# the two searches.
_PICKER_KINDS = frozenset({"subtitle", "audio"})


def _folder_segments_relative_to(source: Path, candidate: Path) -> tuple[str, ...]:
    """The folder names between `source`'s own directory and `candidate`
    -- the same relative shape `mytube_media.subtitles.walk_sidecars`
    already computes for its own `SidecarCandidate.folder_segments`,
    re-derived here for a `SubtitleTrack` (`external_tracks`'s own return
    shape), which does not carry that field itself. Not a second
    directory scan -- this is a pure path computation over two paths the
    caller already resolved.
    """
    try:
        rel = candidate.relative_to(source.parent.resolve())
    except ValueError:
        return ()
    return rel.parent.parts


def _resolve_sidecar_language(*tokens: str | None) -> str | None:
    """D-184's chain -- a tag, then a filename suffix, then folder tokens,
    in that order -- as one call: `None` entries (a tag or suffix the
    candidate simply does not carry) are dropped before the resolver ever
    sees them, since `resolve_language` has no real token to check a
    missing one against.
    """
    tag, _source_token = language_from_tokens([token for token in tokens if token])
    return tag


def _resolve_track_kind_from_folders(folder_segments) -> str | None:
    """D-186: signs-only versus full, read from the folder tokens between
    the source and the sidecar. The first token that resolves to either
    word wins; a folder that names neither is neither invented.
    """
    for token in folder_segments:
        kind = resolve_track_kind(token)
        if kind in ("signs_only", "full"):
            return kind
    return None


def _href(rel_path: str) -> str:
    """`/add/{rel_path}`, each segment percent-encoded so a folder or file
    name with a space or a reserved character still produces a working
    link -- the "/" separators between segments are never encoded, since
    those are ours, not part of any one name.
    """
    if not rel_path:
        return "/add"
    return "/add/" + "/".join(quote(part, safe="") for part in rel_path.split("/"))


def _breadcrumb(rel_path: str) -> list[dict]:
    """One entry per path segment plus the root, each carrying the label to
    display and the href to navigate to -- built from the validated path
    the server already resolved, never from the raw request path.
    """
    crumbs = [{"label": None, "href": "/add", "is_root": True}]
    if not rel_path:
        return crumbs
    parts = rel_path.split("/")
    accumulated: list[str] = []
    for part in parts:
        accumulated.append(part)
        crumbs.append({"label": part, "href": _href("/".join(accumulated)), "is_root": False})
    return crumbs


async def _resolve_share_path(root: Path, rel: str) -> tuple[Path, str, bool]:
    """Validate `rel` against the mount root and answer the three facts
    every share route needs from it -- the resolved target, its canonical
    share-relative path, and whether it is a file -- in one thread hop
    rather than three separate stat round trips on the event loop.

    Raises `ShareEscape` exactly as `safe_join` does; the caller renders
    the "cannot reach the share" state from it.
    """

    def _work() -> tuple[Path, str, bool]:
        target = safe_join(root, rel)
        return target, relative_to_root(root, target), target.is_file()

    return await asyncio.to_thread(_work)


def _unreachable_response(request: Request, status_code: int):
    return render(
        request,
        "share_listing.html",
        status_code=status_code,
        share_unreachable=True,
        entries=[],
        breadcrumb=_breadcrumb(""),
    )


async def _render_add_form(request: Request, target: Path, rel_path: str):
    """Screen 2 (02-UI-SPEC.md): the verdict, the tracks, the address, and
    whether this exact source has been added before. `ffprobe` runs once,
    right here, for this one file -- D-56.
    """
    settings = get_settings()
    entry_name = target.name

    try:
        probe_json = await asyncio.to_thread(probe, target)
    except ProbeError:
        probe_json = None

    verdict = None
    if probe_json is not None:
        try:
            moov_first = await asyncio.to_thread(moov_before_mdat, target)
        except OSError:
            # The probe above already proved the file is readable media;
            # a byte-level read failing here would be a race with the
            # share (file removed/moved mid-request) rather than a real
            # verdict -- treat it the same as "not faststart", the safe
            # default that never claims already_correct incorrectly.
            moov_first = False
        verdict = analyze(probe_json, moov_first=moov_first, filename=entry_name)

    # External subtitle sidecars (D-122) -- a filesystem-only search, no
    # probe needed, but scoped to a readable/probed source anyway (Screen
    # 2 hierarchy item 2: an unprobed file shows no track picker of any
    # kind, embedded or external). Runs off the event loop like every
    # other filesystem walk this route makes.
    root = Path(settings.share_root)
    subtitle_tracks_external: list[dict] = []
    audio_tracks_external: list[dict] = []
    if verdict is not None:
        try:
            found = await asyncio.to_thread(external_tracks, target, root)
        except (OSError, TypeError):
            # Best-effort: a sidecar scan going wrong (an unreadable
            # directory, or -- in one existing test's own deliberate
            # dangerous-path double -- a stand-in object that is not a
            # real Path) never breaks the rest of the form (T-04-01
            # already covers the containment side of this, not the
            # filesystem-availability side).
            found = []
        for track in found:
            candidate_path = Path(track.path)
            try:
                rel = relative_to_root(root, candidate_path)
            except ValueError:
                continue
            folder_segments = _folder_segments_relative_to(target, candidate_path)
            subtitle_tracks_external.append(
                {
                    "rel_path": rel,
                    "name": candidate_path.name,
                    "folder_label": "/".join(folder_segments),
                    # D-184: the tag inside the file first -- a bare
                    # sidecar text file carries none, so only the suffix
                    # (`track.language`, `_match_sidecar`'s own raw
                    # return) and the folder tokens are real candidates
                    # here.
                    "language": _resolve_sidecar_language(track.language, *folder_segments),
                    "track_kind": _resolve_track_kind_from_folders(folder_segments),
                }
            )

        # External audio dubs (D-180, D-181): matched by the episode
        # number this single file's own name recognises, the same D-173
        # number match the folder-wide flow will later reuse. A file
        # nothing recognises an episode number from has no number to pair
        # a dub against, so the search is simply not run for it rather
        # than guessed -- an empty audio_tracks_external list, not an
        # error.
        recognised = recognize_filename(entry_name)
        if recognised.episode is not None:
            try:
                audio_candidates = await asyncio.to_thread(
                    external_audio_tracks, target, root, recognised.episode
                )
            except (OSError, TypeError):
                audio_candidates = []
            for candidate in audio_candidates:
                try:
                    audio_rel = relative_to_root(root, candidate.path)
                except ValueError:
                    continue
                folder_label = "/".join(candidate.folder_segments)
                try:
                    probed = await asyncio.to_thread(
                        external_audio_track, candidate.path, folder_label=folder_label
                    )
                except ProbeError:
                    # A dub the walker found but ffprobe cannot read is
                    # skipped, never shown as a choice with nothing behind
                    # it -- the same degrade-to-absent rule the sidecar
                    # walk itself already applies to a filesystem error.
                    continue
                gap = duration_gap_seconds(verdict.duration_seconds, probed.duration_seconds)
                audio_tracks_external.append(
                    {
                        "rel_path": audio_rel,
                        "name": Path(candidate.path).name,
                        "folder_label": folder_label,
                        "language": _resolve_sidecar_language(
                            probed.language, candidate.suffix, *candidate.folder_segments
                        ),
                        "codec": probed.codec,
                        "duration_warning": (
                            gap is not None and gap > DURATION_MISMATCH_THRESHOLD_SECONDS
                        ),
                        "duration_gap_seconds": gap,
                    }
                )

    pool = request.app.state.db_pool
    convert_estimate_value = None
    async with pool.acquire() as conn:
        try:
            slug_plan: SlugPlan = await plan_slug(conn, entry_name)
        except SlugError:
            # A filename that transliterates to nothing usable (pure
            # emoji/punctuation) is a real, if rare, edge on a share this
            # code does not control the naming of -- render the same
            # "cannot reach" state a rejected path already uses rather
            # than a raw 500, and let the operator rename the file on the
            # share and try again.
            return _unreachable_response(request, status_code=400)

        already_added_rows = await queries.editions_by_source_paths(conn, [rel_path])

        if verdict is not None and verdict.duration_seconds:
            # Local import: routes_titles.py already imports this module
            # for _resolve_share_path/_unreachable_response, so importing
            # it back at module scope here would be circular. Deferred
            # until this function actually runs, by which point both
            # modules have finished loading.
            from orchestrator.web.routes_titles import convert_estimate_seconds

            convert_estimate_value = await convert_estimate_seconds(
                conn, verdict.duration_seconds, settings
            )

    already_added = already_added_rows[0] if already_added_rows else None

    reuse_confirmed = request.query_params.get("confirm_reuse") == "1"
    add_anyway_confirmed = request.query_params.get("confirm_add_anyway") == "1"

    try:
        size_bytes = await asyncio.to_thread(lambda: target.stat().st_size)
    except OSError:
        size_bytes = 0
    if verdict is not None and verdict.size_bytes:
        size_bytes = verdict.size_bytes

    already_correct = verdict.already_correct if verdict is not None else False
    free_bytes = await asyncio.to_thread(
        lambda: shutil.disk_usage(settings.work_dir).free
    )
    not_enough_space = free_bytes < required_free_bytes(
        size_bytes, already_correct=already_correct
    )

    estimate_seconds = 0.0
    if settings.add_estimate_mb_per_second > 0:
        estimate_seconds = size_bytes / (settings.add_estimate_mb_per_second * 1024 * 1024)

    # D-147: the second-edition gate applies to at most one of the two
    # actions at a time -- the existing edition is either a remux or a
    # transcode, never both, so only the action that would create a
    # *different* kind ever needs the confirm-first control.
    differs_add = already_added is not None and already_added["edition_kind"] == "transcode"
    differs_convert = already_added is not None and already_added["edition_kind"] == "remux"

    can_add = bool(
        verdict is not None
        and verdict.eligible
        and (not slug_plan.is_tombstone or reuse_confirmed)
        and not not_enough_space
        and not (differs_add and not add_anyway_confirmed)
    )

    can_convert = bool(
        verdict is not None
        and verdict.tracks
        # A conversion needs an audio track chosen exactly as a copy does
        # (Amendment 2) -- a source with none has no add path either way
        # (see analyze()'s own reason.no_audio branch).
        and (not slug_plan.is_tombstone or reuse_confirmed)
        and not (differs_convert and not add_anyway_confirmed)
    )

    # Computed here, not concatenated in the template: the template-key
    # scan test_i18n.py::test_every_template_translation_key_exists_in_both_catalogs
    # runs looks for a literal t("...") string and would otherwise flag
    # the shared prefix "layout.detected." itself as if it were a key.
    layout_detected_key = (
        "layout.detected." + verdict.stereo_layout_source if verdict is not None else None
    )

    return render(
        request,
        "add_form.html",
        entry_name=entry_name,
        rel_path=rel_path,
        verdict=verdict,
        layout_detected_key=layout_detected_key,
        probe_failed=probe_json is None,
        slug_plan=slug_plan,
        already_added=already_added,
        differs_add=differs_add,
        differs_convert=differs_convert,
        reuse_confirmed=reuse_confirmed,
        add_anyway_confirmed=add_anyway_confirmed,
        size_bytes=size_bytes,
        estimate_seconds=estimate_seconds,
        not_enough_space=not_enough_space,
        convert_estimate_seconds=convert_estimate_value,
        subtitle_tracks_external=subtitle_tracks_external,
        audio_tracks_external=audio_tracks_external,
        can_add=can_add,
        can_convert=can_convert,
        reuse_href=_href(rel_path) + "?confirm_reuse=1",
        add_anyway_href=_href(rel_path) + "?confirm_add_anyway=1",
        form_action=_href(rel_path),
    )


@router.get("/")
async def share_root_route():
    return RedirectResponse("/add", status_code=302)


@router.get("/add")
@router.get("/add/{path:path}")
async def share_listing_route(request: Request, path: str = ""):
    settings = get_settings()
    root = Path(settings.share_root)

    # `rel_path` is canonical from here on, never the raw request segment:
    # an edition's source_path is an identity, and `Movies/../a.mkv`
    # naming the same file as `a.mkv` under a second string is what makes
    # the "already added" chip stop matching (and puts `..` in a
    # breadcrumb).
    try:
        target, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)

    if is_file:
        return await _render_add_form(request, target, rel_path)

    try:
        entries = await asyncio.to_thread(list_dir, target, root)
    except ShareUnavailable:
        return _unreachable_response(request, status_code=503)

    # One query for the whole folder (never one per row, D-48): which of
    # these files already have an edition, keyed by source path.
    file_paths = [entry.rel_path for entry in entries if not entry.is_dir]
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        already_added_rows = await queries.editions_by_source_paths(conn, file_paths)
    already_added_by_path = {row["source_path"]: row for row in already_added_rows}

    rows = [
        {
            "name": entry.name,
            "is_dir": entry.is_dir,
            "size": entry.size,
            "href": _href(entry.rel_path),
            "already_added": already_added_by_path.get(entry.rel_path),
        }
        for entry in entries
    ]

    return render(
        request,
        "share_listing.html",
        share_unreachable=False,
        entries=rows,
        breadcrumb=_breadcrumb(rel_path),
    )


@router.get("/sidecar-picker/{path:path}")
async def sidecar_picker_route(request: Request, path: str, kind: str = ""):
    """The candidate-list fragment for the add form's manual audio/
    subtitle field (T-05-14): reuses the exact same walk the form itself
    already ran to draw the track lists -- `external_tracks` or
    `external_audio_tracks` -- never a second scan of its own. `kind` is a
    closed vocabulary (`subtitle` or `audio`); anything else is refused
    outright rather than silently defaulting to one of the two searches.

    The source path resolves through `_resolve_share_path`, which is
    `safe_join` under the same containment discipline every other share
    route already applies, and every candidate this route lists is one
    the walker already contained against the share root through that same
    `safe_join` call -- this route can never disclose a folder the add
    form could not already open.
    """
    if kind not in _PICKER_KINDS:
        return Response(status_code=400)

    settings = get_settings()
    root = Path(settings.share_root)
    try:
        target, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return Response(status_code=400)
    if not is_file:
        return Response(status_code=400)

    candidates: list[dict] = []
    if kind == "subtitle":
        try:
            found = await asyncio.to_thread(external_tracks, target, root)
        except (OSError, TypeError):
            found = []
        for track in found:
            candidate_path = Path(track.path)
            try:
                rel = relative_to_root(root, candidate_path)
            except ValueError:
                continue
            folder_segments = _folder_segments_relative_to(target, candidate_path)
            candidates.append(
                {
                    "rel_path": rel,
                    "name": candidate_path.name,
                    "folder_label": "/".join(folder_segments),
                }
            )
    else:
        recognised = recognize_filename(target.name)
        if recognised.episode is not None:
            try:
                found_audio = await asyncio.to_thread(
                    external_audio_tracks, target, root, recognised.episode
                )
            except (OSError, TypeError):
                found_audio = []
            for candidate in found_audio:
                try:
                    rel = relative_to_root(root, candidate.path)
                except ValueError:
                    continue
                candidates.append(
                    {
                        "rel_path": rel,
                        "name": Path(candidate.path).name,
                        "folder_label": "/".join(candidate.folder_segments),
                    }
                )

    return render(request, "partials/sidecar_picker.html", candidates=candidates)


@router.get("/lang/{code}")
async def set_lang_route(code: str, request: Request):
    # The same off-site-redirect hole used to be guarded by its own
    # three-line check here; auth.safe_next is the one shared function now
    # (see auth.py's own docstring for the slash-backslash case this adds).
    next_url = auth.safe_next(request.query_params.get("next", "/"), fallback="/")

    response = RedirectResponse(next_url, status_code=302)
    if code in LOCALES:
        response.set_cookie("mytube_lang", code)
    return response
