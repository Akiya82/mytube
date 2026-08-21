"""FastAPI application entrypoint.

/healthz, plus /v/{slug} and /refresh/{edition_id} (plan 04, D-04/D-06/D-08/
D-09). The delayed-delete reaper (D-13) is wired in by plan 04's Task 3.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.db import create_pool
from orchestrator.resolve import router as resolve_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await create_pool()
    try:
        yield
    finally:
        await app.state.db_pool.close()


app = FastAPI(title="mytube-orchestrator", lifespan=lifespan)
app.include_router(resolve_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    async with app.state.db_pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
