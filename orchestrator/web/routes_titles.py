"""POST of the add form and the first library page (D-45..D-53).

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
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import SlugPlan, plan_slug
from orchestrator.media.compat import ProbeError, audio_tracks, probe
from orchestrator.media.queue import AddJob
from orchestrator.media.share import ShareEscape, safe_join
from orchestrator.slugs import SlugError
from orchestrator.web.routes_share import _unreachable_response
from orchestrator.web.templates import render

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


@router.get("/library")
async def library_route(request: Request):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await queries.library_rows(conn)

    return render(request, "library.html", rows=rows)
