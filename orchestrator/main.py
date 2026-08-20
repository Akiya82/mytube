"""FastAPI application entrypoint.

Only /healthz exists here — enough for compose's healthcheck to wait on.
/v/{slug} and /refresh/{edition_id} land in plan 04.
"""
from fastapi import FastAPI

app = FastAPI(title="mytube-orchestrator")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
