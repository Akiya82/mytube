"""FastAPI application entrypoint.

/healthz, plus /v/{slug} and /refresh/{edition_id} (plan 04, D-04/D-06/D-08/
D-09), plus the delayed-delete reaper (D-13) running as a background task
for the lifetime of the process.
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.db import create_pool
from orchestrator.reaper import run_reaper
from orchestrator.resolve import router as resolve_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await create_pool()
    reaper_task = asyncio.create_task(run_reaper(app.state.db_pool))
    try:
        yield
    finally:
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
        await app.state.db_pool.close()


app = FastAPI(title="mytube-orchestrator", lifespan=lifespan)
app.include_router(resolve_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.db_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
