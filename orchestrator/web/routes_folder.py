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
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Request

from mytube_media.compat import (
    ProbeError,
    analyze,
    external_audio_track,
    moov_before_mdat,
    probe,
)
from mytube_media.share import ShareEscape, ShareUnavailable, list_dir
from mytube_media.subtitles import (
    SUBTITLE_EXTENSIONS,
    external_audio_tracks,
    match_by_episode_number,
    walk_sidecars,
)
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import plan_slug
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
        "title_name": title_name,
        "slug_plan": slug_plan,
        "title_match": title_match,
        "season": group_season,
        "season_provenance": season_provenance,
        "estimate_size_bytes": estimate_size,
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
        default_stereo_layout=default_stereo_layout,
        all_added=all_added,
        nothing_recognized=nothing_recognized,
        incompatible_count=len(incompatible_checked),
        all_probes_failed=all_probes_failed,
        estimate_size_bytes=built["estimate_size_bytes"],
        as_is_is_primary=not incompatible_checked,
        estimate_seconds=estimate_seconds,
        form_action=_folder_href(rel_path),
        default_preview_path=rows[0]["rel_path"] if rows else "",
    )
