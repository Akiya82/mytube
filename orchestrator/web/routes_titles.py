"""POST of the add form and the Library screen (D-45..D-53, D-58..D-61,
D-67, D-72).

`POST /add/{path:path}` is the confirm action Screen 2's form submits to
(`orchestrator/web/templates/add_form.html`, built in plan 03): it writes
the 'preparing' row in one transaction and returns immediately (D-49) --
the actual remux/upload work happens in the background, on
`request.app.state.add_queue` (`orchestrator.media.queue.AddQueue`,
started in `orchestrator.main`'s lifespan).

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
"""
import uuid
from pathlib import Path

from botocore.exceptions import ClientError
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import SlugPlan, plan_slug
from orchestrator.media.compat import ProbeError, audio_tracks, probe
from orchestrator.media.queue import AddJob
from orchestrator.media.share import ShareEscape, safe_join
from orchestrator.slugs import SlugError
from orchestrator.storage import delete_object, get_object_bytes, storage_usage
from orchestrator.web.routes_share import _unreachable_response
from orchestrator.web.templates import humanize_seconds, render

router = APIRouter()


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

    try:
        target = safe_join(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)

    if not target.is_file():
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
        probe_json = probe(target)
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
                source_path=path,
                audio_stream_index=audio_index,
                audio_channels=selected.channels,
            )

        job_row = await queries.job_row(conn, title_id)

    job = AddJob.from_row(title_id, job_row)
    await request.app.state.add_queue.enqueue(job)

    return RedirectResponse("/library", status_code=302)


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
            data["estimate_text"] = _working_estimate_text(settings, data.get("source_path"))
    return data


@router.get("/library")
async def library_route(request: Request):
    settings = get_settings()
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        raw_rows = await queries.library_rows(conn)
        rows = [await _row_context(conn, settings, row) for row in raw_rows]

    return render(request, "library.html", rows=rows, storage_usage=storage_usage())


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
        data = get_object_bytes(f"{parsed}.jpg")
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
            delete_object(key)

        await queries.delete_failed_title(conn, parsed)

    return RedirectResponse("/library", status_code=302)
