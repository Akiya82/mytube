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

from mytube_media.compat import ProbeError, audio_tracks, probe
from mytube_media.share import ShareEscape, safe_join
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import SlugPlan, plan_slug
from orchestrator.reaper import reap_once
from orchestrator.slugs import SlugError
from orchestrator.storage import delete_object, get_object_bytes, reconcile, storage_usage
from orchestrator.web.routes_share import _resolve_share_path, _unreachable_response
from orchestrator.web.templates import humanize_seconds, render

router = APIRouter()

# The sort fallback library_route uses for a merged row missing
# created_at -- see that route's own docstring.
_EPOCH_FALLBACK = datetime.min.replace(tzinfo=timezone.utc)


@router.post("/add/{path:path}")
async def add_confirm_route(
    request: Request,
    path: str,
    name: str = Form(...),
    slug: str = Form(...),
    audio_track: str = Form(...),
    confirm_reuse: str = Form("false"),
):
    settings = get_settings()
    root = Path(settings.share_root)

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

    try:
        audio_index = int(audio_track)
    except ValueError:
        return _unreachable_response(request, status_code=400)

    # Re-probed here, not trusted from the form that opened the page (T-02-06):
    # `audio_index` ends up as an ffmpeg stream selector, so it is checked
    # against the file as it is right now, not as it was when the form drew.
    try:
        probe_json = await asyncio.to_thread(probe, target)
    except ProbeError:
        return _unreachable_response(request, status_code=400)

    tracks = audio_tracks(probe_json)
    selected = next((t for t in tracks if t.index == audio_index), None)
    if selected is None:
        return _unreachable_response(request, status_code=400)

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
                audio_channels=selected.channels,
            )

            # The row is written and the response returns before the
            # actual remux/upload work happens (D-49) -- the durable
            # `jobs` table row is what the addition consumer
            # (`orchestrator.media.queue.run_add_consumer`) claims, not an
            # in-memory handle. Inserted inside the same transaction as
            # the title/edition themselves, so a crash between the two
            # can never leave a 'preparing' title with nothing queued to
            # work it.
            await queries.enqueue_job(
                conn,
                kind="remux",
                title_id=title_id,
                edition_id=edition_id,
                source_path=rel_path,
                audio_stream_index=audio_index,
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


def _working_estimate_text(settings, source_path: str | None) -> str:
    """The Working sub-state's size-derived estimate (D-51): an edition's
    own `size_bytes` stays null until the remux finishes, so while a row
    is 'preparing' and started, the only size the row can show is the
    source file's, read straight off the share the same way the add form
    already does. Best-effort -- a share hiccup mid-remux degrades to an
    empty estimate rather than a broken row; the chip's own text still
    tells the operator work is under way.
    """
    if not source_path or settings.add_estimate_mb_per_second <= 0:
        return ""
    try:
        target = safe_join(Path(settings.share_root), source_path)
        size_bytes = target.stat().st_size
    except (ShareEscape, OSError):
        return ""
    seconds = size_bytes / (settings.add_estimate_mb_per_second * 1024 * 1024)
    return humanize_seconds(seconds)


async def _row_context(conn, settings, row) -> dict:
    """One `library_rows`/`library_row` record, enriched with the two
    facts the query alone cannot answer: how many 'preparing' titles sit
    ahead of a queued one (`count_preparing_ahead` -- a database count,
    never the in-memory queue, which is empty again the instant the
    process restarts) and the size-derived estimate for a started one.
    Both stay null for every other status.
    """
    data = dict(row)
    data["preparing_ahead"] = None
    data["estimate_text"] = None
    if data["title_status"] == "preparing":
        if data["started_at"] is None:
            data["preparing_ahead"] = await queries.count_preparing_ahead(
                conn, data["title_id"]
            )
        else:
            data["estimate_text"] = await asyncio.to_thread(
                _working_estimate_text, settings, data.get("source_path")
            )
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

    return render(
        request,
        "check.html",
        title_name=title_name,
        address=address,
        is_active=resolved["status"] == "active",
    )
