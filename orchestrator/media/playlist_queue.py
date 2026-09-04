"""The assembly consumer: one claiming loop over the durable `jobs` table
(D-118), kind `playlist`, in the shape `orchestrator.media.queue`
establishes for the addition consumer -- a module logger, a frozen job
with one constructor built from a claimed row, one claiming task so
concurrency of one is a property of the loop rather than of a lock, and
per-job exception isolation so one failed build never stops the next one.

Before this module read `PlaylistBuildQueue`, an in-memory FIFO queue
object -- a route handed a job to it directly, and the whole thing forgot
every queued build the instant this process restarted (a cost this module's own
prior docstring named and accepted, since a build has no equivalent of the
addition's heartbeat-driven recovery until now). A row inserted by
`routes_playlists._enqueue_build` is now the only thing this loop ever
reads: it claims the oldest queued `playlist` row for itself, works it,
and reports progress and outcome back onto the same row through the job
lifecycle in `orchestrator.db.queries` -- the same functions the converter
reaches over the network for its own kind of work. This loop is entirely
independent of the addition consumer's own loop: neither shares a claim, a
heartbeat or a failure, so an assembly measured in minutes can never delay
an addition measured in under one (T-04-22).

**Nothing synchronous is called bare from this module.** This loop shares
its event loop with every HTTP request this process answers, including
`GET /p/{slug}`, `GET /v/{slug}` and `/healthz` -- and the compose health
check reads a `/healthz` that does not return as the container being
down, which takes the edge's `depends_on: service_healthy` condition with
it. Every blocking call here -- the disk check, each download, each
probe, each upload, each cleanup call -- goes through `asyncio.to_thread`.
Each segment's own packet copy runs through
`mytube_media.runner.run_ffmpeg_with_progress` instead of a blocking
call, so its progress reaches the operator without blocking this loop
while it runs. Each member's own download and each segment's own upload
run through `orchestrator.media.queue.heartbeat_while` instead of a bare
`asyncio.to_thread` -- a member can be an eight-gigabyte rip, and a
transfer that outlasts the reaper's own stale threshold must not get its
job requeued out from under it while it is still healthily in flight
(CR-01).

**What a build produces.** One segment object per member, MPEG-TS bytes
under a key shaped `{a fresh identifier}.mp4`, plus one artifact row each
in position order. The document listing them is *not* stored -- the
public address renders it fresh on every request with current signatures,
because a stored one carries signatures that age out partway through an
evening and there is no refresh path for a manifest the way there is for
a blob. `orchestrator.media.playlist_build`'s module docstring carries the
reasoning for the container and for the key shape.

**Fresh keys, never an overwrite.** Overwriting rewrites bytes a viewer
may be part-way through reading. New keys plus a pointer moved inside one
transaction make a rebuild atomic from every reader's point of view, and
the address the world holds does not change either way.

**A superseded generation is not deleted here.** It stays in the bucket,
marked superseded, and the reclamation sweep removes it once it is older
than the signing lifetime -- the longest an address handed out before the
rebuild can still be valid. Deleting it at swap time would contradict
this module's own promise: a viewer holding a signed address, past
whatever the edge still has cached, would get nothing and see the stub
clip, in the middle of exactly the evening this product exists for.

**A build interrupted by a restart is no longer silently lost.** The
durable row's heartbeat goes stale and the reaper's own sweep
(`orchestrator.reaper.sweep_stale_jobs`) requeues it, the same recovery
every other kind of work in this project now gets -- there is no second,
build-specific mechanism for it here. The objects a half-finished attempt
had already uploaded before the interruption are still orphaned, since no
artifact row names them until the whole build finishes and swaps; the
storage audit lists them and the operator removes them.
"""
import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from mytube_media import compat
from mytube_media.runner import run_ffmpeg_with_progress
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media import playlist_build
from orchestrator.media.queue import heartbeat_while
from orchestrator.storage import (
    abort_dangling_multipart,
    delete_object,
    download_object_to_file,
    put_object_from_file,
)

logger = logging.getLogger(__name__)

# A stream copy runs far faster than a re-encode, but a member can be an
# eight-gigabyte rip and the read comes off local scratch. Generous, and
# still bounded, so a genuinely hung ffmpeg cannot wedge the worker.
SEGMENT_TIMEOUT_SECONDS = 1800

# The bytes are MPEG-TS whatever the key is spelled like -- see
# `playlist_build`'s module docstring. Stating the real type on the upload
# is what keeps the response a player receives honest about what it holds.
SEGMENT_CONTENT_TYPE = "video/mp2t"

REASON_KEY_EMPTY = "playlist.refused.empty"
REASON_KEY_STORAGE_FULL = "playlist.refused.storage_full"
REASON_KEY_CHECKS = "playlist.refused.checks"
REASON_KEY_ASSEMBLY = "playlist.refused.assembly"

# The claim identity this in-process consumer reports itself under to
# `claim_job`/`heartbeat_job`/`finish_job` -- fixed, since exactly one task
# in this process ever claims a `playlist` row.
PLAYLIST_CONSUMER_WORKER_NAME = "orchestrator-playlist-consumer"


class _InsufficientSpaceError(Exception):
    """Internal signal only. Raised by the free-space precheck and mapped
    to its own catalogue key, the same way the add worker maps its own.
    """


class _SegmentCheckError(Exception):
    """The file ffmpeg produced did not pass this repository's own checks.
    Its own signal, and its own catalogue key, because the premise this
    whole mechanism rests on is that the assembly tool can exit
    successfully over a broken result -- so a zero exit code is not the
    check and must not be reported as one.
    """


class _JobCancelled(Exception):
    """Internal signal only. Raised when `run_ffmpeg_with_progress`
    reports that the operator's cancel request stopped a segment's own
    copy, so the build's single `except` chain can discard what it has
    already uploaded and finish the job `cancelled` rather than `failed`
    -- an operator's decision is not a fault.
    """


@dataclass(frozen=True)
class PlaylistBuildJob:
    """The playlist to assemble, its own job id and attempt, and nothing
    else about what it will build.

    Every other fact is read by the worker itself, in one query, from the
    catalog as it is when the build starts -- not as it was when save was
    pressed. Two saves in a row are ordinary, and a job carrying a
    membership snapshot from the first one would build the wrong list.
    `job_id`/`attempt` are what every heartbeat and the terminal outcome
    are written against -- the same id+worker+attempt match
    `heartbeat_job`/`finish_job` require of every caller, in-process or
    over the network.
    """

    job_id: object
    attempt: int
    playlist_id: object

    @classmethod
    def from_row(cls, job_row: asyncpg.Record) -> "PlaylistBuildJob":
        return cls(
            job_id=job_row["id"],
            attempt=job_row["attempts"],
            playlist_id=job_row["playlist_id"],
        )


async def run_playlist_consumer(pool: asyncpg.Pool, *, poll_interval: float) -> None:
    """Claim one `playlist` row at a time, forever, sleeping
    `poll_interval` whenever the queue is empty -- the assembly
    consumer's own loop, entirely independent of the addition consumer's
    (see the module docstring for why that independence is the point).
    """
    while True:
        try:
            async with pool.acquire() as conn:
                job_row = await queries.claim_job(
                    conn, kind="playlist", worker_name=PLAYLIST_CONSUMER_WORKER_NAME
                )
        except Exception:
            logger.exception("playlist consumer: could not claim from the queue")
            job_row = None

        if job_row is None:
            await asyncio.sleep(poll_interval)
            continue

        job = PlaylistBuildJob.from_row(job_row)
        try:
            await process_playlist_build(pool, job)
        except Exception:
            logger.exception("playlist build %s failed", job.playlist_id)


async def _record_failure(pool, playlist_id, reason_key: str, reason_params: dict) -> None:
    async with pool.acquire() as conn:
        await queries.set_playlist_build_failed(conn, playlist_id, reason_key, reason_params)


async def _finish(pool, job: PlaylistBuildJob, *, outcome: str, reason_key: str | None = None) -> None:
    async with pool.acquire() as conn:
        await queries.finish_job(
            conn,
            job.job_id,
            worker_name=PLAYLIST_CONSUMER_WORKER_NAME,
            attempt=job.attempt,
            outcome=outcome,
            reason_key=reason_key,
        )


async def _discard(keys) -> None:
    """The abort-then-delete pair the add worker already runs, against
    every key this build wrote. The abort covers an upload that died
    partway (an incomplete multipart upload); the delete covers one that
    completed before a later step failed. Both are safe no-ops against a
    key that was never touched, so both run unconditionally.
    """
    for key in keys:
        try:
            await asyncio.to_thread(abort_dangling_multipart, key)
            await asyncio.to_thread(delete_object, key)
        except Exception:
            logger.exception("could not remove the orphaned segment %s", key)


def _probe_duration(probe_json: dict) -> float | None:
    try:
        return float(probe_json.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


async def _check_segment(path) -> float:
    """This repository's own two checks over what ffmpeg produced, before
    anything points at it.

    The faststart check that every MP4 this project produces goes through
    does not apply: an MPEG-TS stream has no index atom to move to the
    front, it is readable from any point by construction. What remains is
    the decodability check -- the same one `tools/check_decodable.sh`
    runs, through the same probe -- and it does double duty, because the
    duration the document declares is read from the produced file rather
    than carried over from the member's catalog row. A declared duration
    that does not match the bytes is a malformed document, and a malformed
    document hangs every player in the instance.
    """
    probe_json = await asyncio.to_thread(compat.probe, path)
    if not any(s.get("codec_type") == "video" for s in probe_json.get("streams", [])):
        raise _SegmentCheckError(f"{path}: no decodable video stream")
    duration = _probe_duration(probe_json)
    if duration is None or duration <= 0:
        raise _SegmentCheckError(f"{path}: no usable duration ({duration!r})")
    return duration


async def process_playlist_build(pool: asyncpg.Pool, job: PlaylistBuildJob) -> None:
    """Assemble one playlist end to end.

    1. Read the members in one query. A member whose title has been
       deleted is skipped: it drops out and the rest play, which is the
       whole promise of a link that keeps working. Nothing left means
       there is nothing to build, and the address already answers
       not-ready for that.
    2. Run the joinability check on the recorded parameters, before
       anything is downloaded. Under this mechanism it is informational
       rather than blocking -- the boundary marker is what handles a
       parameter change, measured rather than assumed -- so the finding is
       logged and the build continues.
    3. Check free space against the directory ffmpeg actually writes into,
       using this build's own formula. The configured storage figure in
       this project is a display value that enforces nothing, and this
       stack has run out of space twice; a build that pulls every member
       onto local disk and writes a copy beside them is the obvious next
       occasion.
    4. Stream each member out of storage into scratch -- never into
       memory.
    5. Copy its packets into one deliverable segment, through the shared
       progress runner. The reported percentage combines how many
       segments are already done with how far through the *current*
       segment's own copy the tool is (`_overall_percent` below) --
       expressed as one figure, an approximation across segments rather
       than a single tool's own measure. A cancellation between blocks
       stops the copy, discards every segment already uploaded, and
       finishes the job `cancelled`.
    6. Check what came out before anything points at it (see
       `_check_segment`).
    7. Upload under a fresh key, never the playlist's own identifier,
       which is stable across rebuilds and would mean overwriting live
       objects. The `uploading` stage is set immediately before this call,
       the same as the addition consumer's own upload stage.
    8. Validate the document these segments render into, against the set
       of keys actually uploaded, and only then record the swap. On any
       validation failure nothing is pointed at anything: this is the one
       failure in this project that reaches every viewer in an instance at
       once.
    9. If the swap reports the playlist is no longer active, delete every
       object just uploaded -- the row is gone, nothing will ever point at
       them, and no artifact row exists for the deletion sweep to find
       them by. The job finishes `cancelled`, not `failed`: nothing went
       wrong, the operator deleted the playlist while this build ran.
    """
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    scratch: list[Path] = []
    uploaded: list[str] = []
    cancel_state = {"requested": False}

    async def _heartbeat(*, stage: str, progress_pct: float | None = None) -> None:
        async with pool.acquire() as conn:
            row = await queries.heartbeat_job(
                conn,
                job.job_id,
                worker_name=PLAYLIST_CONSUMER_WORKER_NAME,
                attempt=job.attempt,
                stage=stage,
                progress_pct=progress_pct,
            )
        if row is not None and row["cancel_requested"]:
            cancel_state["requested"] = True

    def _should_cancel() -> bool:
        return cancel_state["requested"]

    try:
        async with pool.acquire() as conn:
            members = await queries.playlist_build_inputs(conn, job.playlist_id)

        live = [m for m in members if not m["dropped"] and m["object_key"]]
        if not live:
            await _record_failure(pool, job.playlist_id, REASON_KEY_EMPTY, {})
            await _finish(pool, job, outcome="failed", reason_key=REASON_KEY_EMPTY)
            return

        parity = playlist_build.parity_verdict(live)
        if not parity.ok:
            # Recorded, not refused. The boundary between two clips whose
            # parameters differ was measured in a real player and carried,
            # so refusing here would refuse exactly the heterogeneous
            # library this feature exists for.
            logger.info(
                "playlist %s joins members that differ (%s): %s",
                job.playlist_id,
                parity.reason_key,
                parity.reason_params,
            )

        required = playlist_build.required_build_bytes(live)
        free = await asyncio.to_thread(lambda: shutil.disk_usage(settings.work_dir).free)
        if free < required:
            raise _InsufficientSpaceError(
                f"{free} free bytes < {required} required in {settings.work_dir}"
            )

        total = len(live)
        artifacts = []
        sections = []
        for index, member in enumerate(live):
            if cancel_state["requested"]:
                raise _JobCancelled()

            source = work_dir / f"{job.playlist_id}-{index}-source"
            segment = work_dir / f"{job.playlist_id}-{index}-segment"
            scratch.extend((source, segment))

            # Heartbeating while it runs (CR-01): a member can be an
            # eight-gigabyte rip, and a download that outlasts the reaper's
            # own stale threshold must not get its job requeued out from
            # under it while the transfer is still healthily in flight.
            await heartbeat_while(
                lambda: download_object_to_file(member["object_key"], str(source)),
                heartbeat=_heartbeat,
                stage="working",
            )

            cmd = playlist_build.build_segment_command(source, segment)
            cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]

            async def on_progress(_progress, percent):
                # Safe to close over the loop variable `index` directly:
                # this callback is only ever invoked while this
                # iteration's own `run_ffmpeg_with_progress` call is in
                # flight, entirely before the loop advances to the next
                # member.
                segment_fraction = (percent / 100.0) if percent is not None else 0.0
                overall = (index + segment_fraction) / total * 100.0
                await _heartbeat(stage="working", progress_pct=overall)

            result = await run_ffmpeg_with_progress(
                cmd,
                duration_seconds=member["duration_seconds"],
                on_progress=on_progress,
                should_cancel=_should_cancel,
                timeout=SEGMENT_TIMEOUT_SECONDS,
            )
            if result.cancelled:
                raise _JobCancelled()

            duration = await _check_segment(segment)

            key = f"{uuid.uuid4()}.mp4"
            await heartbeat_while(
                lambda: put_object_from_file(str(segment), key, SEGMENT_CONTENT_TYPE),
                heartbeat=_heartbeat,
                stage="uploading",
            )
            uploaded.append(key)

            artifacts.append(
                {
                    "object_key": key,
                    "position": member["position"],
                    "duration_seconds": duration,
                }
            )
            sections.append(
                playlist_build.Section(
                    title_name=member["title_name"] or "",
                    segments=(
                        playlist_build.Segment(
                            object_key=key, duration_seconds=duration
                        ),
                    ),
                )
            )

        check = playlist_build.validate_manifest(sections, set(uploaded))
        if not check.ok:
            raise _SegmentCheckError(f"{check.reason_key}: {check.reason_params}")

        async with pool.acquire() as conn:
            swapped = await queries.set_playlist_built(
                conn, job.playlist_id, "hls", artifacts
            )

        if not swapped:
            logger.info(
                "playlist %s was deleted while its build ran -- removing the "
                "%d object(s) it uploaded, since no row will ever name them",
                job.playlist_id,
                len(uploaded),
            )
            await _discard(uploaded)
            await _finish(pool, job, outcome="cancelled")
            return

        async with pool.acquire() as conn:
            await queries.finish_job(
                conn,
                job.job_id,
                worker_name=PLAYLIST_CONSUMER_WORKER_NAME,
                attempt=job.attempt,
                outcome="done",
            )

        logger.info(
            "playlist %s built: %d segment(s)", job.playlist_id, len(artifacts)
        )
    except _JobCancelled:
        logger.info("playlist build %s cancelled", job.playlist_id)
        await _discard(uploaded)
        await _finish(pool, job, outcome="cancelled")
    except Exception as exc:
        logger.exception("playlist build %s failed", job.playlist_id)
        await _discard(uploaded)
        reason_key = _reason_key_for(exc)
        await _record_failure(pool, job.playlist_id, reason_key, _reason_params_for(exc))
        await _finish(pool, job, outcome="failed", reason_key=reason_key)
    finally:
        for path in scratch:
            try:
                if await asyncio.to_thread(path.exists):
                    await asyncio.to_thread(path.unlink)
            except OSError:
                logger.exception("could not remove the scratch file %s", path)


def _reason_key_for(exc: Exception) -> str:
    """Map a failure to a translation catalogue key, never an English
    sentence and never raw tool output.

    A produced file that failed its own checks gets a different key from
    an assembly that failed outright, because they send the operator to
    different places: the first means the clip that came out is wrong, the
    second means it never came out. `FfmpegFailed` (the shared progress
    runner's own exception, raised for a non-zero exit or a timeout) falls
    into the same assembly-failure bucket the previous `RemuxError` did --
    both mean the segment never came out.
    """
    if isinstance(exc, _InsufficientSpaceError):
        return REASON_KEY_STORAGE_FULL
    if isinstance(exc, _SegmentCheckError):
        return REASON_KEY_CHECKS
    return REASON_KEY_ASSEMBLY


def _reason_params_for(exc: Exception) -> dict:
    """A refusal carries values, not a message. The only value any of
    these sentences substitutes today is nothing at all -- the underlying
    detail goes to the log, where it belongs, rather than in front of an
    operator who cannot act on ffmpeg's stderr.
    """
    return {}
