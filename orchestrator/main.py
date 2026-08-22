"""FastAPI application entrypoint.

/healthz, plus /v/{slug} and /refresh/{edition_id} (plan 04, D-04/D-06/D-08/
D-09), plus the delayed-delete reaper (D-13) running as a background task
for the lifetime of the process. Also the share-browsing interface (D-57
onward): /static/ assets and the share router. The add worker (D-49/D-50)
is a second lifespan-scoped background task, started and cancelled exactly
the way the reaper task already is -- see orchestrator.media.queue.
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from orchestrator.config import get_settings
from orchestrator.db import create_pool, queries
from orchestrator.media.queue import AddJob, AddQueue
from orchestrator.reaper import run_reaper
from orchestrator.resolve import router as resolve_router
from orchestrator.storage import abort_dangling_multipart, delete_object
from orchestrator.web.routes_share import router as share_router
from orchestrator.web.routes_titles import router as titles_router

STATIC_DIR = Path(__file__).parents[1] / "static"
logger = logging.getLogger(__name__)


async def _sweep_preparing_titles(pool: asyncpg.Pool, add_queue: AddQueue) -> None:
    """Startup reconciliation for interrupted adds (D-53) -- run once,
    before the worker task starts pulling, so a stale 'preparing' row
    never sits waiting for a worker that will never see it again. The
    in-process queue holds nothing across a restart, so this is the only
    remaining place D-53's "leaves nothing behind" promise can be kept
    for a row that was mid-flight when the process died.

    A row with `started_at` set was genuinely interrupted: its object
    (and its poster, if one was uploaded before the crash) may be fully
    present in storage with nothing pointing at it once this sweep marks
    the row failed, so the same abort-then-delete cleanup pair
    `process_add`'s own failure path uses runs here too, before the row
    is marked failed -- a crash between the cleanup and the mark leaves
    the row still saying "preparing", and this sweep runs again on the
    next restart.

    A row with `started_at` still null never began any work at all;
    marking it "interrupted" would be a lie about a row that was only
    ever sitting in the queue, so it goes back on the queue instead,
    rebuilt through `AddJob.from_row` -- the one shape a job can have.

    Every blocking storage call here goes through `asyncio.to_thread`,
    exactly like every other blocking call in `orchestrator.media.queue`:
    an unbounded number of mid-flight rows after a crash would otherwise
    mean an unbounded serial run of storage calls before this process
    ever answers `/healthz`, against the five-second health check `edge`'s
    `depends_on: service_healthy` condition is waiting on.
    """
    settings = get_settings()

    async with pool.acquire() as conn:
        queued, started = await queries.preparing_rows_at_startup(conn)

        for row in started:
            title_id = row["title_id"]
            try:
                job_row = await queries.job_row(conn, title_id)
                if job_row is not None:
                    await asyncio.to_thread(abort_dangling_multipart, job_row["object_key"])
                    await asyncio.to_thread(delete_object, job_row["object_key"])
                    poster_key = job_row["poster_key"]
                    if poster_key:
                        await asyncio.to_thread(abort_dangling_multipart, poster_key)
                        await asyncio.to_thread(delete_object, poster_key)
                await queries.mark_title_failed(conn, title_id, "failed.reason.interrupted")
            except Exception:
                logger.exception(
                    "startup sweep: failed to reconcile interrupted title %s", title_id
                )

        for row in queued:
            title_id = row["title_id"]
            try:
                job_row = await queries.job_row(conn, title_id)
                if job_row is not None:
                    await add_queue.enqueue(AddJob.from_row(title_id, job_row))
            except Exception:
                logger.exception(
                    "startup sweep: failed to re-enqueue queued title %s", title_id
                )

    # A process that was killed never ran its own scratch cleanup -- this
    # is the only place that promise can be kept. /work is a plain
    # container-layer directory (never a named volume), so on a genuine
    # recreate it is already empty; this also covers a plain restart,
    # which does not clear it on its own.
    work_dir = Path(settings.work_dir)
    if work_dir.is_dir():
        for leftover in work_dir.iterdir():
            if leftover.is_file():
                leftover.unlink()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await create_pool()
    app.state.add_queue = AddQueue()

    await _sweep_preparing_titles(app.state.db_pool, app.state.add_queue)

    add_worker_task = asyncio.create_task(app.state.add_queue.run(app.state.db_pool))
    reaper_task = asyncio.create_task(run_reaper(app.state.db_pool))
    try:
        yield
    finally:
        add_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await add_worker_task
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
        await app.state.db_pool.close()


app = FastAPI(title="mytube-orchestrator", lifespan=lifespan)
app.include_router(resolve_router)
app.include_router(share_router)
app.include_router(titles_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.db_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
