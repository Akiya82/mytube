"""POST of the add form and the Library screen (D-45..D-53, D-58..D-61,
D-67, D-72).

`POST /add/{path:path}` is the confirm action Screen 2's form submits to
(`orchestrator/web/templates/add_form.html`, built in plan 03): it writes
the 'preparing' row and a queued `remux` job row in one transaction and
returns immediately (D-49) -- the actual remux/upload work happens in the
background, claimed off the durable `jobs` table by
`orchestrator.media.queue.run_add_consumer`, started in
`orchestrator.main`'s lifespan.

There is deliberately no `confirm_duplicate` form field: the design
contract settles that a duplicate source path is disclosed by the share
listing's chip and the add form's banner and is not blocked (D-48), so
this route never looks for one -- a route that required a field no
template produces would make adding a duplicate unreachable through the
interface while a test that posts the field by hand would pass for
nothing.

The rest of this module is Screen 3 (02-UI-SPEC.md): `GET /library` (the
full page), `GET /library/{id}/row` (the same row partial, polled every 4
seconds by `static/app.js` for an in-progress title), `GET
/poster/{edition_id}` (the poster frame, served through the orchestrator
rather than edge -- see that route's own docstring for why), and `POST
/library/{id}/dismiss` (hard-deletes a failed title and frees its slug).

This plan adds the three actions the row partial's states were already
built for: `GET`/`POST /library/{id}/rename` (drives the same
`plan_slug`/`rename_slug` pair the CLI's own `_rename` uses, so the old
address stays in slug_history exactly as it always has), `POST
/library/{id}/delete`, `/undo` and `/purge` (the grace window `reaper.py`
already runs, finally reachable from both ends), and `GET /check/{slug}`
(Screen 4 -- a video element pointed at the public address, not a direct
storage URL, so this page proves the whole delivery chain rather than just
that a file exists somewhere).

Every blocking call here -- the share probe, the source stat behind the
Working estimate, the bucket listing behind the usage figure, the poster
GET, and the object deletes -- goes through `asyncio.to_thread`, for the
reason `orchestrator.media.queue`'s docstring states in full: they share
an event loop with `GET /v/{slug}` and `/healthz`, and a bucket
enumeration on a cold cache run bare stops both for its whole duration.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from mytube_media.compat import ProbeError, audio_tracks, external_audio_track, probe
from mytube_media.share import ShareEscape, relative_to_root, safe_join
from mytube_media.stereo3d import LAYOUTS as STEREO_LAYOUTS
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.language import resolve_track_kind
from orchestrator.media.add import (
    AudioChoiceRefused,
    SlugPlan,
    SubtitleChoiceRefused,
    embedded_subtitle_kind,
    parse_audio_choice,
    parse_subtitle_choice,
    plan_slug,
)
from orchestrator.reaper import reap_once
from orchestrator.slugs import SlugError
from orchestrator.storage import delete_object, get_object_bytes, reconcile, storage_usage
from orchestrator.web.routes_share import (
    _folder_segments_relative_to,
    _resolve_share_path,
    _unreachable_response,
)
from orchestrator.web.templates import humanize_seconds, render


def _resolve_signs_only(source: Path, subtitle_target: Path) -> bool | None:
    """D-186 at add time: the first folder token between the source and
    the chosen external subtitle file that resolves to signs-only or
    full, or `None` when nothing in the path resolves either word -- the
    exact same resolution `routes_share._render_add_form` already applies
    when it draws the caption for this same file, re-run here because the
    route validates the choice fresh rather than trusting what the form
    last rendered (T-02-06's own discipline, applied to this new field).
    """
    folder_segments = _folder_segments_relative_to(source, subtitle_target)
    for token in folder_segments:
        kind = resolve_track_kind(token)
        if kind in ("signs_only", "full"):
            return kind == "signs_only"
    return None


router = APIRouter()

# The sort fallback library_route uses for a merged row missing
# created_at -- see that route's own docstring.
_EPOCH_FALLBACK = datetime.min.replace(tzinfo=timezone.utc)

async def convert_estimate_seconds(conn, duration_seconds, settings) -> float | None:
    """D-148's pre-start time estimate: source duration divided by the
    median realtime factor of recent finished conversions, falling back
    to the configured constant when no conversion has finished yet. A
    median, not a mean -- one pathological source must not poison the
    estimate for weeks. Reads through `queries.median_realtime_factor`
    rather than any SQL of its own, since the query module is what owns
    this table (D-148); this function exists so both the GET-rendered
    form (`routes_share._render_add_form`) and this route's own
    free-space precheck compute the estimate identically instead of two
    slightly different formulas drifting apart.
    """
    if not duration_seconds:
        return None
    factor = await queries.median_realtime_factor(conn, kind="transcode")
    if not factor:
        factor = settings.transcode_estimate_realtime_factor
    if not factor:
        return None
    return duration_seconds / factor


@router.post("/add/{path:path}")
async def add_confirm_route(
    request: Request,
    path: str,
    name: str = Form(...),
    slug: str = Form(...),
    audio_choice: str = Form(...),
    stereo_layout: str = Form("2d"),
    subtitle_choice: str = Form("none"),
    subtitle_manual_path: str = Form(""),
    confirm_reuse: str = Form("false"),
    confirm_add_anyway: str = Form("false"),
    add: str | None = Form(None),
    convert: str | None = Form(None),
):
    """One address, two branches (D-145) -- which submit control the
    browser sent (`add` or `convert`, the two buttons' own `name`) decides
    which runs; a POST naming neither (every pre-existing caller of this
    route, before this plan) still takes the as-is branch, unchanged.
    """
    settings = get_settings()
    root = Path(settings.share_root)
    is_convert = convert is not None

    # `rel_path` is the identity the "already added" chip and the
    # duplicate-detection query both key on -- derived from the validated
    # target, never the raw request segment (see share.relative_to_root).
    try:
        target, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)

    if not is_file:
        return _unreachable_response(request, status_code=400)

    clean_name = name.strip()
    if not clean_name:
        return _unreachable_response(request, status_code=400)

    # T-05-13: the compound audio choice is re-derived from the form on
    # every submission, never trusted from an earlier page render -- a
    # submission bypassing the page's own controls gets exactly the same
    # scrutiny a normal one does.
    try:
        audio_kind, audio_index, audio_rel_path = parse_audio_choice(audio_choice)
    except AudioChoiceRefused:
        return _unreachable_response(request, status_code=400)

    if stereo_layout not in STEREO_LAYOUTS:
        return _unreachable_response(request, status_code=400)

    # Re-probed here, not trusted from the form that opened the page
    # (T-02-06): the embedded audio index and the subtitle choice both end
    # up as ffmpeg stream selectors, so both are checked against the file
    # as it is right now, not as it was when the form drew.
    try:
        probe_json = await asyncio.to_thread(probe, target)
    except ProbeError:
        return _unreachable_response(request, status_code=400)

    external_audio_rel_path: str | None = None
    if audio_kind == "embedded":
        tracks = audio_tracks(probe_json)
        selected = next((t for t in tracks if t.index == audio_index), None)
        if selected is None:
            return _unreachable_response(request, status_code=400)
        audio_channels = selected.channels
    else:
        # An external audio choice is contained against the share root the
        # same way an external subtitle choice already is, and must exist
        # right now -- a missing or out-of-bounds choice fails the request
        # with its own refusal (T-05-16), never a silent fall back to the
        # embedded track, since the operator specifically asked for the
        # dub (add.audio_choice.missing / add.audio_choice.outside_share).
        try:
            audio_target = safe_join(root, audio_rel_path)
        except ShareEscape:
            return _unreachable_response(request, status_code=400)
        exists = await asyncio.to_thread(audio_target.is_file)
        if not exists:
            return _unreachable_response(request, status_code=400)
        try:
            probed_external = await asyncio.to_thread(external_audio_track, audio_target)
        except ProbeError:
            return _unreachable_response(request, status_code=400)
        external_audio_rel_path = relative_to_root(root, audio_target)
        audio_index = None
        audio_channels = probed_external.channels

    clean_subtitle_kind = "none"
    clean_subtitle_stream_index: int | None = None
    clean_subtitle_path: str | None = None
    subtitle_signs_only: bool | None = None

    if is_convert:
        try:
            raw_kind, raw_stream_index, raw_path = parse_subtitle_choice(
                subtitle_choice,
                manual_path=subtitle_manual_path.strip(),
                rel_path=rel_path,
            )
        except SubtitleChoiceRefused:
            return _unreachable_response(request, status_code=400)

        # D-140: a stereo source refuses a burn-in outright -- a job that
        # will refuse itself minutes later on a shared card is a waste of
        # the operator's time and the card's, so this is checked before
        # anything is queued, not left to the converter to discover.
        if stereo_layout != "2d" and raw_kind != "none":
            return _unreachable_response(request, status_code=400)

        if raw_kind == "embedded":
            embedded_kind = embedded_subtitle_kind(probe_json, raw_stream_index)
            if embedded_kind is None:
                return _unreachable_response(request, status_code=400)
            clean_subtitle_kind = embedded_kind
            clean_subtitle_stream_index = raw_stream_index
        elif raw_kind == "external":
            try:
                subtitle_target = safe_join(root, raw_path)
            except ShareEscape:
                return _unreachable_response(request, status_code=400)
            exists = await asyncio.to_thread(subtitle_target.is_file)
            if not exists:
                return _unreachable_response(request, status_code=400)
            clean_subtitle_kind = "external"
            clean_subtitle_path = relative_to_root(root, subtitle_target)
            # D-186: the editions.subtitle_burned_signs_only column is
            # written below, once the edition exists, only when a folder
            # token between the source and this file actually resolved
            # one of the two words -- left unset (NULL) otherwise, since
            # unknown and known-false are different facts.
            subtitle_signs_only = _resolve_signs_only(target, subtitle_target)
        else:
            clean_subtitle_kind = "none"

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The advisory lock has to be taken before taken_slugs() is
            # read -- see create_title_with_edition's own docstring for
            # the race this closes and why the lock alone, taken inside
            # that function, would not be enough.
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
            )
            try:
                slug_plan: SlugPlan = await plan_slug(conn, clean_name, explicit_slug=slug)
            except SlugError:
                return _unreachable_response(request, status_code=400)

            if slug_plan.is_tombstone and confirm_reuse != "true":
                # The form's primary button is disabled until "Reuse this
                # address" is clicked (a GET reload, see routes_share.py);
                # a POST that arrives without that confirmation is either a
                # stale form or a direct request bypassing the UI, and is
                # refused the same way the CLI's --reuse-slug refuses it.
                return _unreachable_response(request, status_code=400)

            # D-147's second-edition gate, enforced here too -- a form can
            # be submitted with no page's own control at all, and this is
            # the check that actually decides. Only fires when the
            # existing edition's own kind is known and differs from the
            # action about to be taken; a title this phase never queued a
            # job for (edition_kind None) is not treated as a mismatch.
            existing_rows = await queries.editions_by_source_paths(conn, [rel_path])
            existing = existing_rows[0] if existing_rows else None
            actioned_kind = "transcode" if is_convert else "remux"
            if (
                existing is not None
                and existing["edition_kind"] is not None
                and existing["edition_kind"] != actioned_kind
                and confirm_add_anyway != "true"
            ):
                return _unreachable_response(request, status_code=400)

            edition_id = uuid.uuid4()
            object_key = f"{edition_id}.mp4"
            title_id = await queries.create_title_with_edition(
                conn,
                clean_name,
                slug_plan.chosen,
                edition_id,
                object_key,
                status="preparing",
                source_path=rel_path,
                audio_stream_index=audio_index,
                audio_channels=audio_channels,
                # D-139: both actions carry the layout selector and both
                # store what it says -- a file that arrives already
                # stereo-compatible takes this as-is path, and it is the
                # one that most needs the badge. The convert branch stores
                # its own layout later, once the conversion reports done
                # (see routes_worker.done_route's own
                # set_edition_conversion_detail call) -- not here, so the
                # edition is never written twice for the same fact.
                stereo_layout=None if is_convert else stereo_layout,
            )

            if subtitle_signs_only is not None:
                await queries.set_edition_subtitle_signs_only(
                    conn, edition_id, subtitle_signs_only
                )

            # The row is written and the response returns before the
            # actual work happens (D-49) -- the durable `jobs` table row
            # is what the matching consumer claims, not an in-memory
            # handle. Inserted inside the same transaction as the
            # title/edition themselves, so a crash between the two can
            # never leave a 'preparing' title with nothing queued to work
            # it.
            if is_convert:
                await queries.enqueue_job(
                    conn,
                    kind="transcode",
                    title_id=title_id,
                    edition_id=edition_id,
                    source_path=rel_path,
                    audio_stream_index=audio_index,
                    subtitle_kind=clean_subtitle_kind,
                    subtitle_stream_index=clean_subtitle_stream_index,
                    subtitle_path=clean_subtitle_path,
                    stereo_layout=stereo_layout,
                    external_audio_path=external_audio_rel_path,
                )
            else:
                await queries.enqueue_job(
                    conn,
                    kind="remux",
                    title_id=title_id,
                    edition_id=edition_id,
                    source_path=rel_path,
                    audio_stream_index=audio_index,
                    external_audio_path=external_audio_rel_path,
                )

    return RedirectResponse("/library", status_code=302)


async def _rename_target_row(conn, title_id):
    """The one Library row a rename can act on -- reusing `queries.library_row`
    rather than a new query, since it already has everything the rename
    form needs (title name, current slug). Only an 'active' title may be
    renamed (T-02-10): a title mid-flight, already failed, or already
    counting down to deletion has no address worth correcting.
    """
    row = await queries.library_row(conn, title_id)
    if row is None or row["title_status"] != "active":
        return None
    return row


@router.get("/library/{title_id}/rename")
async def rename_form_route(title_id: str, request: Request):
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await _rename_target_row(conn, parsed)
    if row is None:
        return Response(status_code=404)

    return render(
        request,
        "rename_form.html",
        done=False,
        title_id=parsed,
        slug=row["slug"],
        name_value=row["title_name"],
        slug_plan=None,
    )


@router.post("/library/{title_id}/rename")
async def rename_confirm_route(
    title_id: str,
    request: Request,
    name: str = Form(...),
    confirm_reuse: str = Form("false"),
):
    """Wraps the exact same `plan_slug` / `rename_slug` pair
    `cli/__main__.py`'s `_rename` already calls (D-63) -- nothing about
    slug_history changes here, `rename_slug` alone still owns that. The one
    behavior `plan_slug` adds beyond the CLI's own rename (which never
    checked for a tombstone at all, see `_rename`) is D-64: a rename onto a
    tombstoned slug now gets the same explicit-confirmation gate the add
    form's `confirm_reuse` already gives the CLI's `--reuse-slug` flag,
    rather than silently getting suffixed away by collision handling.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    clean_name = name.strip()
    if not clean_name:
        return Response(status_code=400)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await _rename_target_row(conn, parsed)
        if row is None:
            return Response(status_code=404)
        old_slug = row["slug"]

        async with conn.transaction():
            # Same advisory lock as the add route, taken before the slug
            # plan is read -- see create_title_with_edition's docstring
            # for the race this closes.
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
            )
            try:
                slug_plan: SlugPlan = await plan_slug(conn, clean_name)
            except SlugError:
                return Response(status_code=400)

            if slug_plan.is_tombstone and confirm_reuse != "true":
                # Mirrors the add form's own gate: show the warning and a
                # "Reuse this address" confirm control, write nothing yet.
                return render(
                    request,
                    "rename_form.html",
                    done=False,
                    title_id=parsed,
                    slug=old_slug,
                    name_value=clean_name,
                    slug_plan=slug_plan,
                )

            await queries.rename_slug(conn, old_slug, slug_plan.chosen)

    return render(
        request,
        "rename_form.html",
        done=True,
        title_id=parsed,
        old_slug=old_slug,
        new_slug=slug_plan.chosen,
        similar=slug_plan.similar,
    )


def derive_job_ui(data: dict, settings) -> None:
    """Turn the raw job/converter columns `queries._LIBRARY_ROW_FROM` and
    `queries._PLAYLIST_ROW_FROM` already joined in into the one value the
    template dispatches on (`job_stage`) plus the small set of pre-
    formatted strings it renders (04-UI-SPEC.md States -> Job progress).
    Mutates `data` in place; pure and synchronous, since every fact it
    needs already rode in on the one row query -- this only ever
    recombines them, never queries again (the same "one query per screen"
    reasoning that put the job/converter joins in the row query at all).

    Shared by both row kinds (`_row_context` below and
    `routes_playlists._playlist_row_context`) -- one status model, one
    place that reads it (D-132).
    """
    data["job_stage"] = None
    data["job_percent"] = 0
    data["job_remaining_text"] = ""
    data["worker_ago_text"] = ""

    if data.get("job_id") is None:
        return

    progress = data.get("job_progress_pct")
    data["job_percent"] = int(round(progress)) if progress is not None else 0
    eta = data.get("job_eta_seconds")
    data["job_remaining_text"] = humanize_seconds(eta) if eta is not None else ""

    # A running job with the flag set is shown cancelling regardless of
    # its own `stage` column -- nothing in this codebase writes
    # stage='cancelling' today (the consumers heartbeat 'working'/
    # 'uploading' throughout), so the flag itself is the signal.
    if data.get("job_cancel_requested") and data.get("job_status") == "running":
        data["job_stage"] = "cancelling"
        return

    db_stage = data.get("job_db_stage")
    if db_stage in ("working", "uploading", "cancelling"):
        data["job_stage"] = db_stage
        return

    # db_stage == "queued" (or an unrecognised value -- falls back the
    # same way, never a raised error over a row the operator is looking
    # at). Only a transcode job has a converter to wait on at all (D-113);
    # remux and playlist both run in-process the instant this service
    # itself claims them.
    if data.get("job_kind") != "transcode":
        data["job_stage"] = "queued"
        return

    worker_name = data.get("worker_name")
    last_seen = data.get("worker_last_seen_at")
    stale = True
    if last_seen is not None:
        elapsed = (datetime.now(timezone.utc) - last_seen).total_seconds()
        stale = elapsed > settings.converter_offline_seconds
        data["worker_ago_text"] = humanize_seconds(elapsed)

    if not stale and data.get("worker_status") == "waiting_for_gpu":
        data["job_stage"] = "waiting_for_gpu"
    elif stale:
        data["job_stage"] = "waiting_for_converter"
    else:
        data["job_stage"] = "queued"
    data["worker_name"] = worker_name


async def _row_context(conn, settings, row) -> dict:
    """One `library_rows`/`library_row` record, enriched with the one
    fact the row query cannot answer synchronously: the job/converter
    columns it already joined in, recombined into `job_stage` by
    `derive_job_ui`. No further database call -- see that function's own
    docstring.
    """
    data = dict(row)
    derive_job_ui(data, settings)
    # The converted-detail line's own bitrate (D-146), formatted the same
    # way every other size on this row already is (`filesizeformat_binary`,
    # applied in the template) -- reusing that filter on bytes-per-second
    # rather than a new bits-per-second formatter. Only meaningful once a
    # conversion has actually recorded what it made (`source_codec` is set
    # only by `set_edition_conversion_detail`), and only computable when
    # both the size and the duration are known.
    data["output_bytes_per_second"] = None
    if data.get("source_codec") and data.get("size_bytes") and data.get("duration_seconds"):
        data["output_bytes_per_second"] = data["size_bytes"] / data["duration_seconds"]
    return data


@router.get("/library")
async def library_route(request: Request):
    """The library merges two sources into one newest-first list (D-108):
    every video row from `library_rows` and every playlist row from
    `playlist_rows`, each tagged with a `kind` the row partial dispatches
    on. Sorting falls back to the earliest possible timestamp for a row
    missing `created_at` (never observed against a real database, where
    both queries always select it -- only a hand-built test stub can omit
    it) rather than raising, so one malformed row cannot take the whole
    screen down, the same tolerance this route already extends to a row's
    optional technical fields.
    """
    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        raw_rows = await queries.library_rows(conn)
        video_rows = [await _row_context(conn, settings, row) for row in raw_rows]
        for row in video_rows:
            row["kind"] = "video"

        raw_playlist_rows = await queries.playlist_rows(conn)
        playlist_rows = [dict(row) for row in raw_playlist_rows]
        for row in playlist_rows:
            row["kind"] = "playlist"
            derive_job_ui(row, settings)

        rows = sorted(
            [*video_rows, *playlist_rows],
            key=lambda row: row.get("created_at") or _EPOCH_FALLBACK,
            reverse=True,
        )

        referenced = await queries.referenced_objects(conn)

    # The reconciliation costs one catalog query and no extra bucket
    # enumeration -- usage.listing is the same sixty-second-cached listing
    # the used/free figure already reads (storage.storage_usage()).
    usage = await asyncio.to_thread(storage_usage)
    reconciliation = reconcile(usage.listing, referenced)

    return render(
        request,
        "library.html",
        rows=rows,
        storage_usage=usage,
        reconciliation=reconciliation,
    )


@router.get("/library/{title_id}/row")
async def library_row_fragment_route(title_id: str, request: Request):
    """The exact row partial `library.html` includes, rendered standalone
    -- what `static/app.js` polls every 4 seconds for an in-progress row
    (D-49). Never anything but the one `<li>` element: the poller swaps
    only the row it fetched, so a page's scroll position and any other
    row's focus survive every poll.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.library_row(conn, parsed)
        if row is None:
            return Response(status_code=404)
        data = await _row_context(conn, settings, row)

    return render(request, "partials/library_row.html", row=data)


@router.get("/poster/{edition_id}")
async def poster_route(edition_id: str):
    """The poster frame extracted at add time (D-72), served through the
    orchestrator rather than edge: the edge blob path is bound to the
    UUID-and-`.mp4` pattern the previous phase spent seven plans proving,
    and widening that component to serve a thumbnail on a LAN-only page is
    not a trade worth making for a few tens of KB.

    The identifier is parsed as a UUID before any storage call (T-02-09),
    so this route can never be turned into an arbitrary-key reader -- a
    non-UUID is a 404. The object key itself is never looked up in the
    catalog: it is derived deterministically from the UUID exactly as
    `orchestrator.media.queue.process_add` names it
    (`f"{edition_id}.jpg"`), so a missing poster is simply a missing S3
    key, answered with the same 404 rather than a second existence check.
    """
    try:
        parsed = uuid.UUID(edition_id)
    except ValueError:
        return Response(status_code=404)

    try:
        data = await asyncio.to_thread(get_object_bytes, f"{parsed}.jpg")
    except ClientError:
        return Response(status_code=404)

    return Response(
        content=data,
        media_type="image/jpeg",
        # A poster is immutable once extracted (a new add gets a new
        # edition id, never the same key rewritten) -- safe to cache for a
        # year rather than re-fetched on every library render.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/library/{title_id}/dismiss")
async def dismiss_route(title_id: str, request: Request):
    """Hard-delete a failed title and free its slug immediately (D-61):
    nothing was ever published under it, so there is no old link to
    protect and no confirmation to ask for.

    **Objects before rows.** `queries.failed_title_object_keys` only
    reads; the objects it names are deleted here first, and only then
    does `queries.delete_failed_title` destroy the rows that were the
    only record of those keys (T-02-08) -- a crash between the two steps
    leaves the row there to retry dismiss against, rather than an object
    nothing can ever find again. `delete_object` on an already-absent key
    is a no-op, so a retry after a partial failure costs one extra S3
    round trip and needs no bookkeeping about which case this was.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            keys = await queries.failed_title_object_keys(conn, parsed)
        except LookupError:
            return Response(status_code=404)
        except ValueError:
            # Refused: this title is not 'failed' -- nothing was deleted,
            # on either side (T-02-10).
            return Response(status_code=400)

        for key in keys:
            await asyncio.to_thread(delete_object, key)

        await queries.delete_failed_title(conn, parsed)

    return RedirectResponse("/library", status_code=302)


async def _cancel_never_active_title(conn, title_id) -> None:
    """The immediate half of a cancellation's hard delete (T-04-32):
    objects before rows, the same ordering `dismiss_route` already uses
    for the same reason (T-02-08). Swallows the guard's own refusal --
    called from more than one place (a queued cancel finishing on the
    spot here, a running job's cancel completing later through the
    worker/consumer/reaper paths this same function does not cover) and
    a title that has moved on (or was already removed) between the check
    and this call is not this route's problem to report.
    """
    try:
        keys = await queries.never_active_title_object_keys(conn, title_id)
    except (LookupError, ValueError):
        return
    for key in keys:
        await asyncio.to_thread(delete_object, key)
    try:
        await queries.delete_never_active_title(conn, title_id)
    except (LookupError, ValueError):
        pass


@router.post("/library/{title_id}/cancel")
async def cancel_route(title_id: str, request: Request):
    """Stop an in-flight job for this title, for every kind of work
    (D-129, D-132).

    A job still queued has no converter or consumer to tell, so
    `queries.request_job_cancel` finishes it `cancelled` on the spot --
    this route notices that (the returned row's own `status`) and hard-
    deletes the title immediately, with no tombstone (T-04-32): its
    address never went live. A running job only has its cancel flag set
    here; the row keeps polling and shows the cancelling stage until the
    consumer that owns it (`orchestrator.media.queue`,
    `orchestrator.web.routes_worker.cancelled_route`) or the reaper's
    stale sweep (`orchestrator.reaper.sweep_stale_jobs`) finishes the
    same hard delete once the worker actually confirms or gives up --
    all three call the identical `queries.delete_never_active_title`
    this route calls for the immediate case, so a cancelled title
    disappears the same way regardless of which path finishes it.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        job = await queries.job_for_title(conn, parsed)
        if job is None:
            return Response(status_code=404)

        result = await queries.request_job_cancel(conn, job["id"])
        if result is not None and result["status"] == "cancelled":
            await _cancel_never_active_title(conn, parsed)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/{title_id}/retry")
async def retry_route(title_id: str, request: Request):
    """Requeue a failed title with the same choices and the same address
    (D-130): reads the failed row's own last job -- whichever kind it
    was, remux or transcode -- and enqueues a fresh one carrying exactly
    the same payload, then puts the title back to 'preparing' so the row
    renders queued, visually indistinguishable from a first attempt.

    Only a 'failed' title has a Retry control to reach this from (the
    same T-02-10 shape every other action route in this module already
    follows) -- a title in any other status is refused.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.library_row(conn, parsed)
        if row is None or row["title_status"] != "failed":
            return Response(status_code=404)

        last_job = await queries.last_job_for_title(conn, parsed)
        if last_job is None:
            return Response(status_code=404)

        async with conn.transaction():
            await queries.enqueue_job(
                conn,
                kind=last_job["kind"],
                title_id=parsed,
                edition_id=last_job["edition_id"],
                source_path=last_job["source_path"],
                audio_stream_index=last_job["audio_stream_index"],
                subtitle_kind=last_job["subtitle_kind"],
                subtitle_stream_index=last_job["subtitle_stream_index"],
                subtitle_path=last_job["subtitle_path"],
                stereo_layout=last_job["stereo_layout"],
            )
            await queries.requeue_title(conn, parsed)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/{title_id}/delete")
async def delete_route(title_id: str, request: Request):
    """Start the grace window (D-13, D-62): `GET /v/{slug}` answers 410
    immediately, unchanged from phase 1. Only an 'active' title can be
    marked for deletion -- the same guard rename uses, for the same
    reason (T-02-10): a title that is not active has no live address to
    protect a mouse slip against.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.library_row(conn, parsed)
        if row is None or row["title_status"] != "active":
            return Response(status_code=404)
        await queries.mark_for_delete(conn, parsed, settings.delete_grace_seconds)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/{title_id}/undo")
async def undo_route(title_id: str, request: Request):
    """D-62's cancel: only a title currently pending deletion may be
    undone (T-02-10) -- a stale page cannot revive a title that was
    already purged or was never marked for deletion in the first place.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.library_row(conn, parsed)
        if row is None:
            return Response(status_code=404)
        if row["title_status"] != "pending_delete":
            return Response(status_code=400)
        await queries.cancel_delete(conn, parsed)

    return RedirectResponse("/library", status_code=302)


@router.post("/library/{title_id}/purge")
async def purge_route(title_id: str, request: Request):
    """D-62's skip-the-wait: reuses `mark_for_delete` with a zero-second
    window rather than a second deletion path (so it can never drift from
    the one the reaper already runs), then awaits one `reap_once` cycle
    inline so the object is actually gone -- and the title actually
    'deleted' -- before this response returns, rather than sometime within
    the next 30-second poll. Only a title already pending deletion may be
    purged (T-02-10): purge is reached from the countdown UI, which only
    ever shows for a title already in that state.
    """
    try:
        parsed = uuid.UUID(title_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.library_row(conn, parsed)
        if row is None:
            return Response(status_code=404)
        if row["title_status"] != "pending_delete":
            return Response(status_code=400)
        await queries.mark_for_delete(conn, parsed, 0)

    await reap_once(pool)

    return RedirectResponse("/library", status_code=302)


@router.get("/check/{slug}")
async def check_page_route(slug: str, request: Request):
    """Screen 4 (D-65): a `<video>` pointed at the public address for this
    exact slug, never a direct storage URL (T-02-12) -- the point is to
    prove the whole delivery chain (resolve, sign, redirect, range-serve),
    not that an object exists somewhere. A slug that does not resolve at
    all is a 404; a slug that resolves to a non-active title renders the
    same page with the video element omitted, since the address is real
    but does not currently play.
    """
    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        resolved = await queries.resolve_slug(conn, slug)
        if resolved is None:
            return Response(status_code=404)
        row = await queries.library_row(conn, resolved["title_id"])

    title_name = row["title_name"] if row is not None else slug
    address = f"{settings.public_base_url}/v/{slug}"

    # The stereo-mode sentence (04-UI-SPEC.md Screen 4 addendum): read
    # straight off the stored edition, never re-derived here -- the
    # layout was decided once, when the file was added or converted, and
    # a page that re-derived it from the file itself could disagree with
    # the badge the library row already shows for the same edition.
    stereo_layout = row.get("stereo_layout") if row is not None else None
    layout_hint_key = None
    if stereo_layout and stereo_layout.startswith("sbs"):
        layout_hint_key = "check.layout_hint.sbs"
    elif stereo_layout and stereo_layout.startswith("ou"):
        layout_hint_key = "check.layout_hint.ou"

    return render(
        request,
        "check.html",
        title_name=title_name,
        address=address,
        is_active=resolved["status"] == "active",
        layout_hint_key=layout_hint_key,
    )
