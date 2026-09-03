"""The converter's only way to reach the queue.

The database is not reachable from where the converter runs (D-110): a
worker on `ml-server` talks to this process, over the interface's own
hostname, under this one prefix, and nothing else. The video bytes never
pass through here either -- the converter uploads them straight to storage
with its own scoped credential (D-115); only claims, heartbeats and
outcomes cross this boundary.

**The authorization rule, stated as a rule.** This prefix is opened by its
own credential (`require_worker_token`, applied to the whole router below),
and it is deliberately absent from every one of the three session-exemption
tuples in `orchestrator.auth`. Those tuples grant a pass with nothing behind
them -- exactly wrong for a caller that has no cookie and can follow no
redirect to a sign-in form it cannot read. This prefix is instead named in
`auth.TOKEN_GATED_PREFIXES`, which lets the request reach this router with
no session and authorizes nothing; the refusing is done here, by the
dependency below.
"""
import asyncio
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.storage import delete_object

logger = logging.getLogger(__name__)

WORKER_PREFIX = "/worker/"
WORKER_PROTOCOL_VERSION = 1

# The dotted catalog keys a converter is allowed to report on
# POST .../failed. Anything else is refused (T-04-07) -- this is what keeps
# a converter's own text out of the sentence an operator reads. The five
# reasons from gpu_out_of_memory through disk_full are this phase's own;
# crashed_converter is written by the reaper's stale-heartbeat sweep
# (orchestrator.reaper), not by the converter itself, but belongs in the
# same vocabulary; upload and unknown are the general-purpose fallbacks
# already used elsewhere in the catalog.
ALLOWED_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "failed.reason.crashed_converter",
        "failed.reason.gpu_out_of_memory",
        "failed.reason.probe_mismatch",
        "failed.reason.protocol_mismatch",
        "failed.reason.unreadable_source",
        "failed.reason.disk_full",
        "failed.reason.upload",
        "failed.reason.unknown",
    }
)

# Not a reason a converter may report (it carries no reason_key at all) --
# what a cancelled transcode's own title is marked failed with, so the
# library row leaves 'preparing' and the operator gets a Retry button
# (D-130) instead of a title stuck mid-flight forever.
_CANCELLED_TITLE_REASON = "failed.reason.cancelled"


async def require_worker_token(
    authorization: str | None = Header(default=None),
) -> None:
    """The one credential check for the whole router (D-111).

    Compares the presented value against the configured one with the same
    constant-time, byte-encoded comparison `routes_auth._setup_token_valid`
    already uses: both sides are encoded before comparing, so a value
    containing a non-ASCII character is refused rather than raising, and a
    missing header, a wrong scheme, a wrong value and a right-length wrong
    value are all refused identically, with no explanatory body. Nothing
    here ever logs the header, the presented value or the configured one.
    """
    ok = False
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme == "Bearer" and value:
            ok = secrets.compare_digest(
                value.encode("utf-8"), get_settings().worker_token.encode("utf-8")
            )
    if not ok:
        raise HTTPException(status_code=401)


router = APIRouter(prefix="/worker", dependencies=[Depends(require_worker_token)])


class _WorkerRequest(BaseModel):
    protocol: int
    worker: str


class ClaimBody(_WorkerRequest):
    # 'idle' (ready for work) or 'waiting_for_gpu' (the shared card is
    # below the free-memory floor, D-113) -- what worker_checkins.status
    # records on every claim, whether or not it finds work.
    status: str = "idle"


class HeartbeatBody(_WorkerRequest):
    attempt: int
    stage: str
    progress_pct: float | None = None
    eta_seconds: int | None = None
    realtime_factor: float | None = None


class DoneBody(_WorkerRequest):
    attempt: int
    object_key: str


class FailedBody(_WorkerRequest):
    attempt: int
    reason_key: str


class CancelledBody(_WorkerRequest):
    attempt: int


def _protocol_mismatch(protocol: int) -> JSONResponse | None:
    """None when `protocol` is the version this service speaks; otherwise
    a machine-readable refusal naming the mismatch, so a converter
    deployed ahead of (or behind) this service says so instead of failing
    strangely.
    """
    if protocol == WORKER_PROTOCOL_VERSION:
        return None
    return JSONResponse(
        status_code=400,
        content={
            "error": "protocol_mismatch",
            "expected_protocol": WORKER_PROTOCOL_VERSION,
            "got_protocol": protocol,
        },
    )


@router.post("/claim")
async def claim_route(request: Request, body: ClaimBody):
    """One claim, at most one job.

    The check-in is recorded first, unconditionally, before anything else
    -- the interface's "waiting for a converter" sentence is fed by
    check-ins and must not depend on there being work. A converter that
    reports `waiting_for_gpu` is recorded and answered no-content
    **without a claim being attempted**: it already said it cannot start
    one, and handing it work anyway would burn one of that job's three
    attempts on a condition that resolves itself.

    Carries only the operator's own choices (D-119): the job id, the
    attempt, the kind, the catalog identifiers, the destination object
    key, the source path, the chosen audio stream, the subtitle choice and
    the stereo layout. Nothing derived -- no encoder flags, no filter
    string, no built command. The converter re-reads the source file at
    the moment it starts and builds its own command with the same shared
    code the add form used; a command built here from an earlier read of
    the same file is a disagreement waiting to happen.

    A database hiccup during a claim is answered the same as an empty
    queue rather than as an error: a poller calling in every few seconds
    should never see a stack trace for a condition that resolves itself on
    the next poll.
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch

    pool = request.app.state.db_pool
    claimed = None
    object_key = None
    try:
        async with pool.acquire() as conn:
            await queries.record_worker_seen(conn, worker_name=body.worker, status=body.status)
            if body.status != "waiting_for_gpu":
                claimed = await queries.claim_job(
                    conn, kind="transcode", worker_name=body.worker
                )
                if claimed is not None:
                    object_key = await queries.edition_object_key(
                        conn, claimed["edition_id"]
                    )
    except Exception:
        logger.exception("claim: could not reach the queue for worker %r", body.worker)
        claimed = None

    if claimed is None:
        return Response(status_code=204)

    return {
        "job_id": str(claimed["id"]),
        "attempt": claimed["attempts"],
        "kind": claimed["kind"],
        "title_id": str(claimed["title_id"]) if claimed["title_id"] else None,
        "edition_id": str(claimed["edition_id"]) if claimed["edition_id"] else None,
        "object_key": object_key,
        "source_path": claimed["source_path"],
        "audio_stream_index": claimed["audio_stream_index"],
        "subtitle_kind": claimed["subtitle_kind"],
        "subtitle_stream_index": claimed["subtitle_stream_index"],
        "subtitle_path": claimed["subtitle_path"],
        "stereo_layout": claimed["stereo_layout"],
    }


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat_route(job_id: uuid.UUID, request: Request, body: HeartbeatBody):
    """Write progress, answer the cancellation flag.

    A mismatch -- an id that does not exist, or one that does but no
    longer belongs to this worker's attempt -- answers not-found with no
    traceback: a converter that lost the job it thinks it is still working
    has nothing to clean up yet (no report has been accepted, no object
    has necessarily finished uploading), so there is nothing here for it
    to act on beyond stopping.
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch

    pool = request.app.state.db_pool
    row = None
    try:
        async with pool.acquire() as conn:
            row = await queries.heartbeat_job(
                conn,
                job_id,
                worker_name=body.worker,
                attempt=body.attempt,
                stage=body.stage,
                progress_pct=body.progress_pct,
                eta_seconds=body.eta_seconds,
                realtime_factor=body.realtime_factor,
            )
    except Exception:
        logger.exception("heartbeat: could not write progress for job %s", job_id)
        row = None

    if row is None:
        return Response(status_code=404)
    return {"cancel_requested": row["cancel_requested"]}


async def _reject_superseded(pool, job_id: uuid.UUID, *, delete_key: bool) -> JSONResponse:
    """The shared conflict answer for done/failed/cancelled: a report from
    an attempt that no longer owns this job.

    **This service deletes the orphaned upload itself, not the converter.**
    It knows the object key from the job row -- looked up here, never
    trusted from the request that was just refused -- and it holds the
    bucket's full credential; the converter's own storage account is
    scoped to put and abort-multipart, with no delete beyond its own keys
    (D-115), so asking it to clean up would either fail against its own
    policy or force that policy open. The converter's only remaining duty
    is to abort any multipart upload of its own that is still dangling.
    """
    if delete_key:
        try:
            async with pool.acquire() as conn:
                job_row = await queries.job_by_id(conn, job_id)
                key = (
                    await queries.edition_object_key(conn, job_row["edition_id"])
                    if job_row is not None
                    else None
                )
            if key:
                await asyncio.to_thread(delete_object, key)
                logger.info("deleted orphaned object %r for superseded job %s", key, job_id)
        except Exception:
            logger.exception(
                "could not clean up the orphaned object for superseded job %s", job_id
            )
    return JSONResponse(status_code=409, content={"error": "superseded"})


@router.post("/jobs/{job_id}/done")
async def done_route(job_id: uuid.UUID, request: Request, body: DoneBody):
    """A finished transcode. Flips the title to playable and records what
    the conversion made it from, then answers no-content.

    A superseded report is refused with the orphan already deleted (see
    `_reject_superseded`) -- a late report from an attempt the reaper has
    already requeued must never mark a different attempt's job done.
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch

    pool = request.app.state.db_pool
    wrote = False
    try:
        async with pool.acquire() as conn:
            job_row = await queries.job_by_id(conn, job_id)
            wrote = await queries.finish_job(
                conn, job_id, worker_name=body.worker, attempt=body.attempt, outcome="done"
            )
            if wrote and job_row is not None and job_row["title_id"] is not None:
                await queries.mark_title_active(conn, job_row["title_id"])
                await queries.set_edition_conversion_detail(
                    conn,
                    job_row["edition_id"],
                    source_codec=None,
                    source_profile=None,
                    subtitle_burned_language=None,
                    subtitle_burned_title=None,
                    stereo_layout=job_row["stereo_layout"],
                )
    except Exception:
        logger.exception("done: could not finish job %s", job_id)
        wrote = False

    if not wrote:
        return await _reject_superseded(pool, job_id, delete_key=True)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/failed")
async def failed_route(job_id: uuid.UUID, request: Request, body: FailedBody):
    """A conversion the converter gave up on. `reason_key` must be one of
    `ALLOWED_FAILURE_REASONS` -- checked before the database is touched at
    all, so a converter's own text can never reach the sentence an
    operator reads (T-04-07).
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch
    if body.reason_key not in ALLOWED_FAILURE_REASONS:
        return JSONResponse(
            status_code=400,
            content={"error": "reason_not_allowed", "reason_key": body.reason_key},
        )

    pool = request.app.state.db_pool
    wrote = False
    try:
        async with pool.acquire() as conn:
            job_row = await queries.job_by_id(conn, job_id)
            wrote = await queries.finish_job(
                conn,
                job_id,
                worker_name=body.worker,
                attempt=body.attempt,
                outcome="failed",
                reason_key=body.reason_key,
            )
            if wrote and job_row is not None and job_row["title_id"] is not None:
                await queries.mark_title_failed(conn, job_row["title_id"], body.reason_key)
    except Exception:
        logger.exception("failed: could not finish job %s", job_id)
        wrote = False

    if not wrote:
        return await _reject_superseded(pool, job_id, delete_key=False)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/cancelled")
async def cancelled_route(job_id: uuid.UUID, request: Request, body: CancelledBody):
    """Confirms a cancellation the worker learned of from an earlier
    heartbeat's own answer.
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch

    pool = request.app.state.db_pool
    wrote = False
    try:
        async with pool.acquire() as conn:
            job_row = await queries.job_by_id(conn, job_id)
            wrote = await queries.finish_job(
                conn, job_id, worker_name=body.worker, attempt=body.attempt, outcome="cancelled"
            )
            if wrote and job_row is not None and job_row["title_id"] is not None:
                await queries.mark_title_failed(
                    conn, job_row["title_id"], _CANCELLED_TITLE_REASON
                )
    except Exception:
        logger.exception("cancelled: could not finish job %s", job_id)
        wrote = False

    if not wrote:
        return await _reject_superseded(pool, job_id, delete_key=False)
    return Response(status_code=204)
