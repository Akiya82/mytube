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
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.storage import delete_object, invalidate_usage_cache

logger = logging.getLogger(__name__)

WORKER_PREFIX = "/worker/"
# Raised to 3 in this change: the claim response grew `external_audio_path`
# (D-180), a field an older converter does not read and a newer one
# requires. The two images are non-interoperable across this bump -- the
# converter is built from source on the GPU host by
# `docker-compose.worker.yml`, not pulled from the registry alongside the
# interface, so until it is rebuilt from the same commit it exits at its
# first claim rather than silently ignoring the new field.
WORKER_PROTOCOL_VERSION = 3

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
    # records on every claim, whether or not it finds work. Enumerated
    # (WR-05/IN-13) rather than a free string: any other value would
    # otherwise reach the column's own CHECK constraint and come back as
    # an unrelated database error instead of a 422 naming the field.
    status: Literal["idle", "waiting_for_gpu"] = "idle"


class HeartbeatBody(_WorkerRequest):
    # Bounded rather than a bare int (WR-05): the second and third
    # attempts at the same job are real, but nothing legitimate ever
    # claims a job for the zeroth or a negative time.
    attempt: int = Field(ge=1)
    # Enumerated against `jobs.stage`'s own CHECK constraint (WR-05/
    # IN-13): 'queued' and 'cancelling' are never valid heartbeat stages
    # (a heartbeat only ever reports the two states a worker itself
    # observes itself in), so a converter that sends either -- or a typo
    # -- is refused by name here rather than reaching the constraint and
    # coming back as a 404 with a stack trace logged on every request.
    stage: Literal["working", "uploading"]
    # A credential holder can reach this route with numbers alone
    # (T-04-57): every bound below exists because the value is stored
    # unguarded and then rendered unguarded, on the library page every
    # operator watches. `allow_inf_nan=False` refuses NaN/inf, which
    # `derive_job_ui`'s own `int(round(progress))` cannot survive.
    progress_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    eta_seconds: int | None = Field(default=None, ge=0, le=7 * 24 * 3600)
    realtime_factor: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)


class DoneBody(_WorkerRequest):
    """The success report. Every field from `source_codec` onward is
    explicit and carries no default -- a converter that omits a key is
    refused rather than answered as if it had sent a plausible value
    (T-04-58's own prohibition against a fabricated fact).

    Which fields may still carry an explicit `null` is a decision, stated
    here rather than left to be inferred from the type alone (recorded
    for the next plan's own converter-side model in this plan's own
    Summary): `size_bytes`, `duration_seconds`, `width`, `height` and
    `audio_reencoded` are facts every conversion always knows, so they
    have no `| None` at all -- a report missing one of them is refused,
    never silently written as absent. `source_codec`, `fps`,
    `video_profile`, `pix_fmt`, `audio_codec`, `audio_sample_rate` and
    `realtime_factor` describe the stored object this same conversion
    just produced and measured, so they are equally always known and
    equally carry no `| None`. The remaining eight -- `source_profile`
    (a codec that carries no profile), `video_level` (a container that
    carries no level tag), `audio_channel_layout` (ffprobe names the
    channel count of a multichannel AAC track but not always its order:
    the converter's own ffmpeg 8.1 build reports every 5.1 AAC output it
    makes as "unknown"), `audio_language`/`audio_title` (a source track
    with no language or title tag), `subtitle_burned_language`/
    `subtitle_burned_title` (a conversion with no subtitle burned) and
    `poster_key` (a poster whose extraction failed) -- are genuinely
    sometimes absent, so each is typed `| None` with no default: still a
    required key, but the value itself may be sent as `null`.
    """

    attempt: int = Field(ge=1)
    object_key: str

    # --- what the source was, and what was done to it (D-146) ---------
    source_codec: str
    source_profile: str | None
    subtitle_burned_language: str | None
    subtitle_burned_title: str | None

    # --- what the stored object is (D-60, WR-08) -----------------------
    size_bytes: int = Field(gt=0, le=10_000_000_000_000)
    duration_seconds: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)
    width: int = Field(gt=0, le=100_000)
    height: int = Field(gt=0, le=100_000)
    fps: float = Field(gt=0, le=1_000, allow_inf_nan=False)
    video_profile: str
    video_level: int | None = Field(ge=0, le=1_000)
    pix_fmt: str
    audio_codec: str
    audio_sample_rate: int = Field(gt=0, le=1_000_000)
    audio_channel_layout: str | None
    audio_language: str | None
    audio_title: str | None
    audio_reencoded: bool

    # --- the rest of what `done_route` needs to finish the row ---------
    poster_key: str | None
    realtime_factor: float = Field(gt=0, le=1_000, allow_inf_nan=False)


class FailedBody(_WorkerRequest):
    attempt: int = Field(ge=1)
    reason_key: str


class CancelledBody(_WorkerRequest):
    attempt: int = Field(ge=1)


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
    key, the source path, the chosen audio stream, the external audio
    choice (D-180), the subtitle choice and the stereo layout. Nothing
    derived -- no encoder flags, no filter string, no built command. The
    converter re-reads the source file at
    the moment it starts and builds its own command with the same shared
    code the add form used; a command built here from an earlier read of
    the same file is a disagreement waiting to happen.

    A database hiccup during a claim is answered the same as an empty
    queue rather than as an error: a poller calling in every few seconds
    should never see a stack trace for a condition that resolves itself on
    the next poll.

    The claim and the object-key lookup run inside one transaction
    (IN-10): a failure between them used to leave the row `running` with
    no worker, burning one of the job's three attempts for nothing until
    the stale sweep resolved it 360 seconds later. Wrapping them means
    that failure rolls the claim back too, and the tolerance above still
    holds -- the broad `except` below still turns the rollback into the
    same empty-queue answer, never a stack trace. The check-in write
    itself stays outside the transaction and unconditional, per this
    docstring's own opening paragraph: it must survive even when the
    claim that follows it fails.
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
                async with conn.transaction():
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
        "external_audio_path": claimed["external_audio_path"],
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
            if row is not None:
                # This is the fix for the stale-while-busy defect: while
                # a converter holds a job it stops polling /worker/claim,
                # so claim_route's own check-in call freezes at the
                # moment of the claim and the interface reads a busy
                # converter as one that vanished about a minute in. The
                # guard above is the whole superseded-worker protection
                # -- heartbeat_job's own WHERE clause returns a row only
                # when the job id, the worker name, the attempt and the
                # running state all still match, so an attempt that lost
                # its job never reaches here to refresh a check-in its
                # successor now owns. The status is a literal this route
                # chooses, never the request body's own status field --
                # a crafted body can carry a value the check-in's own
                # CHECK constraint would reject, and this route accepts
                # no status from the converter at all.
                await queries.record_worker_seen(conn, worker_name=body.worker, status="working")
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

    **Never deletes when the job is already `done` (CR-03(b)).** Both
    attempts of a job write the same object key, so a late report from an
    earlier attempt against a job a different attempt already finished
    points at the object an active title is serving right now, not at an
    orphan -- the delete is skipped and the skip is logged, so the
    decision is visible without guessing from the object's mere absence.
    """
    if delete_key:
        try:
            async with pool.acquire() as conn:
                job_row = await queries.job_by_id(conn, job_id)
                already_done = job_row is not None and job_row["status"] == "done"
                key = (
                    await queries.edition_object_key(conn, job_row["edition_id"])
                    if job_row is not None and not already_done
                    else None
                )
            if key:
                await asyncio.to_thread(delete_object, key)
                logger.info("deleted orphaned object %r for superseded job %s", key, job_id)
            elif already_done:
                logger.info(
                    "job %s is already done -- its object is live, not deleting it",
                    job_id,
                )
        except Exception:
            logger.exception(
                "could not clean up the orphaned object for superseded job %s", job_id
            )
    return JSONResponse(status_code=409, content={"error": "superseded"})


@router.post("/jobs/{job_id}/done")
async def done_route(job_id: uuid.UUID, request: Request, body: DoneBody):
    """A finished transcode. Flips the title to playable and records what
    the conversion made it from, then answers no-content.

    The read and every write below -- the job row, the terminal write,
    and, only when it wrote and the job carries a title, the title's own
    flip to active plus the conversion detail, the technical detail and
    the poster key -- run inside one transaction on one connection
    (CR-03(a)): all six commit together or none does. A superseded report
    (the terminal write's own clean `False`, no exception) is refused with
    the orphan already deleted (see `_reject_superseded`) -- a late report
    from an attempt the reaper has already requeued must never mark a
    different attempt's job done. A raised exception is a different
    condition and gets a different answer: a server error, never a
    conflict, and no delete -- a conflict is the converter's signal to
    clean up and stop, and it must never be the answer to a transient
    database error that would have resolved on a retry (the converter's
    own retry of a 5xx is the next plan's own change).
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch

    pool = request.app.state.db_pool
    wrote = False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                job_row = await queries.job_by_id(conn, job_id)
                wrote = await queries.finish_job(
                    conn,
                    job_id,
                    worker_name=body.worker,
                    attempt=body.attempt,
                    outcome="done",
                    realtime_factor=body.realtime_factor,
                )
                if wrote:
                    # Alongside the terminal write, inside this same
                    # transaction, not after it: the write is what
                    # proves this report legitimate, and a rolled-back
                    # report must leave no trace of a check-in it never
                    # earned. Closes the window between the converter's
                    # last heartbeat and its next claim poll -- a
                    # converter that just finished and is about to ask
                    # for work again is idle, not still "working".
                    await queries.record_worker_seen(conn, worker_name=body.worker, status="idle")
                if wrote and job_row is not None and job_row["title_id"] is not None:
                    await queries.set_edition_conversion_detail(
                        conn,
                        job_row["edition_id"],
                        source_codec=body.source_codec,
                        source_profile=body.source_profile,
                        subtitle_burned_language=body.subtitle_burned_language,
                        subtitle_burned_title=body.subtitle_burned_title,
                        stereo_layout=job_row["stereo_layout"],
                    )
                    await queries.set_edition_technical(
                        conn,
                        job_row["edition_id"],
                        size_bytes=body.size_bytes,
                        duration_seconds=body.duration_seconds,
                        width=body.width,
                        height=body.height,
                        audio_language=body.audio_language,
                        audio_title=body.audio_title,
                        audio_reencoded=body.audio_reencoded,
                        fps=body.fps,
                        video_profile=body.video_profile,
                        video_level=body.video_level,
                        pix_fmt=body.pix_fmt,
                        audio_codec=body.audio_codec,
                        audio_sample_rate=body.audio_sample_rate,
                        audio_channel_layout=body.audio_channel_layout,
                    )
                    if body.poster_key is not None:
                        await queries.set_edition_poster(
                            conn, job_row["edition_id"], body.poster_key
                        )
                    # D-157/D-178: the edition this job produced lands on
                    # 'active' (not the title directly), the default
                    # pointer moves only when this job's own creation
                    # asked for it, and the title's own aggregate status
                    # is recomputed from the whole season -- see
                    # orchestrator.media.queue's identical done-path
                    # sequence and queries.sync_title_status's own
                    # docstring for the rule.
                    await queries.mark_edition_active(conn, job_row["edition_id"])
                    if job_row["make_default_when_ready"]:
                        await queries.set_default_edition(conn, job_row["edition_id"])
                    await queries.sync_title_status(conn, job_row["title_id"])
    except Exception:
        logger.exception("done: could not finish job %s", job_id)
        return JSONResponse(status_code=503, content={"error": "try_again"})

    if not wrote:
        return await _reject_superseded(pool, job_id, delete_key=True)
    # The converter uploads the object with its own client in its own
    # pod (D-115) -- this process's cached bucket listing has never seen
    # that key, and this report is the first moment this process can know
    # the bucket changed. Called only on this accepted branch, after the
    # write committed: a rolled-back write above must not throw away a
    # cache that is still correct, and the superseded branch above needs
    # nothing of its own -- its own delete_object call clears the cache
    # again by itself.
    invalidate_usage_cache()
    return Response(status_code=204)


@router.post("/jobs/{job_id}/failed")
async def failed_route(job_id: uuid.UUID, request: Request, body: FailedBody):
    """A conversion the converter gave up on. `reason_key` must be one of
    `ALLOWED_FAILURE_REASONS` -- checked before the database is touched at
    all, so a converter's own text can never reach the sentence an
    operator reads (T-04-07).

    Shares `done_route`'s own shape (CR-03): the terminal write and the
    title's own flip to failed run inside one transaction, and a raised
    exception answers a server error rather than a conflict -- the same
    reasoning, since this route answered a conflict for a write that had
    actually happened just as often as the success route did.
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
            async with conn.transaction():
                job_row = await queries.job_by_id(conn, job_id)
                wrote = await queries.finish_job(
                    conn,
                    job_id,
                    worker_name=body.worker,
                    attempt=body.attempt,
                    outcome="failed",
                    reason_key=body.reason_key,
                )
                if wrote:
                    # Same placement and reasoning as done_route's own
                    # call above.
                    await queries.record_worker_seen(conn, worker_name=body.worker, status="idle")
                if wrote and job_row is not None and job_row["title_id"] is not None:
                    # D-157: the edition this job would have produced is
                    # what actually fails; the title's own aggregate
                    # status is recomputed from the whole season, exactly
                    # as the done path recomputes it on success.
                    await queries.mark_edition_failed(
                        conn, job_row["edition_id"], body.reason_key
                    )
                    await queries.sync_title_status(
                        conn, job_row["title_id"], reason_key=body.reason_key
                    )
    except Exception:
        logger.exception("failed: could not finish job %s", job_id)
        return JSONResponse(status_code=503, content={"error": "try_again"})

    if not wrote:
        return await _reject_superseded(pool, job_id, delete_key=False)
    # Same reasoning as done_route's own call above -- and this branch
    # matters in particular: the poster is uploaded before the video
    # (D-115), so a conversion that dies during the video transfer has
    # already put an object in the bucket, and this failure report is
    # the only announcement of it. This direction only ever under-reports
    # for up to a minute, never claims a delete failed, which is why it
    # was found late.
    invalidate_usage_cache()
    return Response(status_code=204)


@router.post("/jobs/{job_id}/cancelled")
async def cancelled_route(job_id: uuid.UUID, request: Request, body: CancelledBody):
    """Confirms a cancellation the worker learned of from an earlier
    heartbeat's own answer.

    A cancelled transcode's title never went live (T-04-32), so the row
    is hard-deleted here with no tombstone, the exact same
    `queries.delete_never_active_title` call
    `orchestrator.media.queue`'s own cancellation cleanup and
    `routes_titles.cancel_route`'s immediate (queued-job) case both use --
    one function, three call sites, one outcome regardless of which of
    them actually finishes the job. This is what makes the row disappear
    on the poller's own existing 404-means-remove branch instead of
    leaving a 'failed' row behind with nothing to dismiss.
    """
    mismatch = _protocol_mismatch(body.protocol)
    if mismatch is not None:
        return mismatch

    pool = request.app.state.db_pool
    wrote = False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                job_row = await queries.job_by_id(conn, job_id)
                wrote = await queries.finish_job(
                    conn,
                    job_id,
                    worker_name=body.worker,
                    attempt=body.attempt,
                    outcome="cancelled",
                )
                if wrote:
                    # Same placement and reasoning as done_route's own
                    # call above.
                    await queries.record_worker_seen(conn, worker_name=body.worker, status="idle")
                if wrote and job_row is not None and job_row["title_id"] is not None:
                    try:
                        await queries.delete_never_active_title(conn, job_row["title_id"])
                    except (LookupError, ValueError):
                        pass
    except Exception:
        logger.exception("cancelled: could not finish job %s", job_id)
        return JSONResponse(status_code=503, content={"error": "try_again"})

    if not wrote:
        return await _reject_superseded(pool, job_id, delete_key=False)
    # Same reasoning as failed_route's own call above -- a cancelled
    # conversion can just as easily have already uploaded a poster before
    # the cancellation was noticed.
    invalidate_usage_cache()
    return Response(status_code=204)
