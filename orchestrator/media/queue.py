"""The addition consumer: one claiming loop over the durable `jobs` table
(D-118), kind `remux`.

Before this module read `AddQueue`, an in-memory FIFO queue object -- a
route handed a job to it directly, and the whole thing forgot every
in-flight and every queued job the instant this process restarted. That
mechanism is gone. A row inserted by `routes_titles.add_confirm_route` is
now the only thing this loop ever reads: it claims the oldest queued
`remux` row for itself, works it, and reports progress and outcome back
onto the same row through the job lifecycle in `orchestrator.db.queries`
-- the same functions the converter reaches over the network for its own
kind of work (`transcode`). A restart loses nothing: the claimed row's
heartbeat simply goes stale and the reaper's sweep (`orchestrator.reaper`)
puts it back in the queue.

**Nothing synchronous is called bare from this module.** `run_add_consumer`
awaits `process_add` on the same event loop that also serves every HTTP
request in this process. `run_ffmpeg` (still used for the poster) is
`subprocess.run`; `put_object_from_file` is boto3's managed `upload_file`.
Both block for as long as the work takes -- called directly, an
eight-gigabyte source stalls the whole service for minutes: the library
poll never returns, `GET /v/{slug}` never returns, and `/healthz` never
returns, which the compose health check (five-second interval,
three-second timeout) reads as the container being down, taking `edge`'s
`depends_on: service_healthy` condition down with it. Every blocking call
in this module -- ffmpeg, every storage operation, the disk check, the
source stat -- goes through `asyncio.to_thread`. The remux itself runs
through `mytube_media.runner.run_ffmpeg_with_progress` instead, which
drains ffmpeg's own pipes on this event loop rather than blocking it, and
reports a percentage on every closed progress block.

**Failure leaves nothing behind (D-53).** Any failure past the point the
title flips to 'preparing'/started can leave an object in the bucket two
different ways: the upload died partway (an *incomplete* multipart upload,
cleared by `abort_dangling_multipart`) or the upload **completed** and a
later step failed (a fully-present object nothing will ever point at
again, cleared by `delete_object`). Both calls are safe to run
unconditionally on every failure -- `delete_object` on a key that was
never uploaded is a no-op against S3 -- so this module always runs both,
for the video key and, when one was uploaded, the poster key, before the
row is marked failed. A cancellation runs the identical pair: an
operator's stop request leaves the same kind of orphan a crash would.
"""
import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from mytube_media import compat, remux
from mytube_media.compat import ProbeError
from mytube_media.runner import FfmpegFailed, run_ffmpeg_with_progress
from mytube_media.share import ShareEscape, safe_join
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import required_free_bytes
from orchestrator.storage import abort_dangling_multipart, delete_object, put_object_from_file

logger = logging.getLogger(__name__)

# ffmpeg on an eight-gigabyte rip is measured in minutes, not seconds --
# generous but not unbounded, so a genuinely hung ffmpeg process cannot
# wedge the worker forever.
REMUX_TIMEOUT_SECONDS = 3600
POSTER_TIMEOUT_SECONDS = 60

# The claim identity this in-process consumer reports itself under to
# `claim_job`/`heartbeat_job`/`finish_job` -- a fixed, single value is
# correct because exactly one task in this process ever claims a `remux`
# row (see `run_add_consumer`'s own docstring for why concurrency of one
# is a property of the loop rather than of a lock).
ADD_CONSUMER_WORKER_NAME = "orchestrator-remux-consumer"


class _InsufficientSpaceError(Exception):
    """Internal signal only -- never escapes `process_add`. Raised by the
    free-space precheck and mapped to the `reason.storage_full` catalog
    key by `_reason_key_for`, the same way `ProbeError` maps to
    `reason.unreadable`.
    """


class _UploadError(Exception):
    """Internal signal only -- never escapes `process_add`. Wraps whatever
    the storage client raised at an upload call site, so "the upload did
    not finish" is told to the operator when the upload is what failed and
    not, as it used to be, for every unexpected exception in the pipeline.
    """


class _JobCancelled(Exception):
    """Internal signal only. Raised when `run_ffmpeg_with_progress`
    reports that the operator's cancel request stopped the remux, so the
    single `except` block in `process_add` can run the same orphan cleanup
    a failure runs and then choose the `cancelled` outcome instead of the
    `failed` one -- the two are different outcomes and the interface says
    different things about them.
    """


# Deliberately short, the same reasoning `worker.__main__`'s own
# heartbeat interval already carries: a transfer that outlasts the
# reaper's stale threshold must not go unheartbeated for any real
# stretch of it, and a heartbeat every few seconds costs this database
# nothing. Not imported from `worker` -- this process shares nothing
# with the converter's own package (orchestrator never imports worker).
HEARTBEAT_WHILE_INTERVAL_SECONDS = 3.0


async def heartbeat_while(work, *, heartbeat, stage: str, interval: float | None = None):
    """Run the blocking, zero-argument callable `work` in a thread while
    sending `heartbeat(stage=stage)` on `interval`'s own timer until it
    finishes -- closes CR-01 for every blocking transfer in this project:
    without this, a transfer that outlasts the reaper's own stale
    threshold gets its job requeued out from under it, the object it just
    finished writing gets deleted, and the same work runs again from
    scratch.

    `interval` defaults to the module constant, read at call time rather
    than bound as a parameter default, so a test can lower it with a
    plain `monkeypatch.setattr` the same way it patches everything else
    this module reads off itself.

    One heartbeat is always sent before the first wait, whether or not
    `work` is already finished by then -- a transfer that finishes inside
    one interval still produces exactly the heartbeat it always did. Not
    a reporter object and not a context manager: a function that awaits a
    thread and sends a heartbeat on a timer is the whole mechanism: a
    second one to keep in step with the converter's own would be more
    than this needs.

    `work`'s own exception, if any, propagates unchanged once the task is
    done -- this adds reporting, never error handling.
    """
    if interval is None:
        interval = HEARTBEAT_WHILE_INTERVAL_SECONDS
    task = asyncio.ensure_future(asyncio.to_thread(work))
    while True:
        await heartbeat(stage=stage)
        done, _pending = await asyncio.wait({task}, timeout=interval)
        if task in done:
            return task.result()


async def _upload(path, key: str, content_type: str = "video/mp4", *, heartbeat) -> None:
    """One upload, heartbeating on its own timer while it runs, with its
    failures labelled. The only place `put_object_from_file` is called
    from `process_add`.
    """
    try:
        await heartbeat_while(
            lambda: put_object_from_file(str(path), key, content_type),
            heartbeat=heartbeat,
            stage="uploading",
        )
    except Exception as exc:
        raise _UploadError(f"{key}: {exc}") from exc


def _reason_key_for(exc: Exception) -> str:
    """Map a failure to a translation catalog key, never an English
    sentence (D-66).

    The residual case gets its own neutral key rather than being folded
    into the upload one. A wrong reason costs more than a vague one: the
    most likely real failure in this pipeline is ffmpeg exiting non-zero
    or hitting its timeout, and telling the operator the upload to storage
    did not finish sends them to look at storage for a file that never
    reached it.
    """
    if isinstance(exc, _InsufficientSpaceError):
        return "reason.storage_full"
    if isinstance(exc, (ProbeError, OSError, ShareEscape)):
        return "reason.unreadable"
    if isinstance(exc, (remux.RemuxError, FfmpegFailed)):
        # RemuxError is the poster extraction's own blocking call
        # (isolated in its own try/except and never reaches this
        # mapping); FfmpegFailed is what the shared progress runner
        # raises for the same condition (a non-zero exit or a timeout) on
        # the main remux invocation. Both describe ffmpeg failing, not the
        # upload, and are told to the operator the same way.
        return "failed.reason.remux"
    if isinstance(exc, _UploadError):
        return "failed.reason.upload"
    return "failed.reason.unknown"


@dataclass(frozen=True)
class AddJob:
    """Everything the worker needs to remux/upload one edition -- nothing
    else. Every remaining fact (`reencode_audio`, `already_correct`,
    duration, dimensions, size) is derived from a probe the worker
    performs itself against the resolved source, never threaded in from
    whichever route enqueued this job.

    `from_row` is the single constructor: it is built from the row
    `claim_job` returns (kind `remux`) plus the edition-side facts that
    row cannot carry (`queries.edition_for_remux`) -- one shape, always
    read fresh off the durable table a claim just locked, never carried
    from an earlier read of the same file. One extra `ffprobe` costs a
    fraction of a second against an upload measured in minutes.
    """

    job_id: object
    attempt: int
    title_id: object
    edition_id: object
    object_key: str
    source_rel_path: str
    audio_stream_index: int
    channels: int

    @classmethod
    def from_row(cls, job_row: asyncpg.Record, edition_row: asyncpg.Record) -> "AddJob":
        return cls(
            job_id=job_row["id"],
            attempt=job_row["attempts"],
            title_id=job_row["title_id"],
            edition_id=job_row["edition_id"],
            object_key=edition_row["object_key"],
            source_rel_path=job_row["source_path"],
            audio_stream_index=job_row["audio_stream_index"],
            channels=edition_row["audio_channels"],
        )


async def run_add_consumer(pool: asyncpg.Pool, *, poll_interval: float) -> None:
    """Claim one `remux` row at a time, forever, sleeping `poll_interval`
    whenever the queue is empty.

    Concurrency of one is a property of this loop's own shape -- exactly
    one task ever calls this function for the whole process (wired once in
    `orchestrator.main`'s lifespan) -- rather than of a lock somebody has
    to remember, the same guarantee the old `AddQueue` gave for a
    different reason (its own single `.get()` caller). Independent of the
    playlist consumer's own loop by construction: the two never share a
    claim, a heartbeat or a failure, so a long assembly can never delay a
    short addition (T-04-22).

    Per-item exception isolation, unchanged from the mechanism this
    replaces: a bad claimed job can raise past `process_add`'s own
    handling (a database hiccup while writing the outcome, say) without
    ever stopping this loop from claiming the next one.
    """
    while True:
        try:
            async with pool.acquire() as conn:
                job_row = await queries.claim_job(
                    conn, kind="remux", worker_name=ADD_CONSUMER_WORKER_NAME
                )
                edition_row = (
                    await queries.edition_for_remux(conn, job_row["edition_id"])
                    if job_row is not None
                    else None
                )
        except Exception:
            logger.exception("add consumer: could not claim from the queue")
            job_row = None
            edition_row = None

        if job_row is None or edition_row is None:
            await asyncio.sleep(poll_interval)
            continue

        job = AddJob.from_row(job_row, edition_row)
        try:
            await process_add(pool, job)
        except Exception:
            logger.exception("add %s failed", job.title_id)


async def _cleanup_orphans(object_key: str, poster_key: str | None) -> None:
    """The abort-then-delete pair the module docstring describes, run
    unconditionally by both `process_add`'s failure branch and its
    cancellation branch -- a cancelled remux leaves the same kind of
    orphan a crash would.
    """
    await asyncio.to_thread(abort_dangling_multipart, object_key)
    await asyncio.to_thread(delete_object, object_key)
    if poster_key is not None:
        await asyncio.to_thread(abort_dangling_multipart, poster_key)
        await asyncio.to_thread(delete_object, poster_key)


async def process_add(pool: asyncpg.Pool, job: AddJob) -> None:
    """Remux (or upload as-is) one edition end to end, starting from a
    title already sitting in the catalog as 'preparing'.

    1. `mark_title_started` first, before any work -- this is what
       separates "waiting" from "working" for the interface, and what
       lets the stale-heartbeat sweep tell a row that was mid-remux from
       one that had not begun.
    2. Resolve the source through `safe_join` against the share root
       again -- a path validated on a previous request is not
       automatically the path arriving on this one. Inside the failure
       handler's `try`, so a rejected path fails the row rather than
       stranding it.
    3. Probe and analyze the resolved source, using the job's own
       `audio_stream_index` as the selected track, from the file as it is
       right now rather than as it was when the form was drawn.
    4. Free-space precheck against the work directory ffmpeg actually
       writes into, before any ffmpeg process starts -- branched on
       `already_correct` through the one shared `required_free_bytes`
       formula (D-52), never a restated threshold.
    5. If already correct (D-44), upload the source file straight from
       the share -- ffmpeg never touches the video at all. Otherwise
       build the remux command, run it through the shared progress
       runner, upload the result, and remove the scratch file whether the
       upload succeeded or not. Either way the size recorded on the
       edition is stat'ed from the artifact that was actually uploaded,
       which in the remux branch is not the source. Every heartbeat
       written while ffmpeg runs carries the running job's cancellation
       answer; a `True` answer stops the tool through the runner's own
       cancellation predicate rather than waiting for it to finish. Both
       upload paths -- the as-is path and the remux path -- run through
       `heartbeat_while`, so an upload that outlasts a heartbeat interval
       is still reporting throughout the transfer and cannot be requeued
       out from under itself (CR-01).
    6. Extract the poster in its own try/except: a poster that will not
       extract must not fail an add that otherwise worked.
    7. Record the technical detail on the edition, flip the title to
       active, and finish the job `done`.

    Any failure past this point runs the abort-then-delete cleanup pair
    (see the module docstring) for the video key and, when one was
    uploaded, the poster key, before the row is marked failed with a
    translation catalog key rather than an English sentence, and the job
    is finished `failed` with the same key. A cancellation (raised as
    `_JobCancelled` once the runner reports it) runs the identical cleanup
    and finishes the job `cancelled` instead.
    """
    settings = get_settings()
    async with pool.acquire() as conn:
        await queries.mark_title_started(conn, job.title_id)

    poster_key: str | None = None
    cancel_state = {"requested": False}

    async def _heartbeat(*, stage: str, progress_pct: float | None = None) -> None:
        async with pool.acquire() as conn:
            row = await queries.heartbeat_job(
                conn,
                job.job_id,
                worker_name=ADD_CONSUMER_WORKER_NAME,
                attempt=job.attempt,
                stage=stage,
                progress_pct=progress_pct,
            )
        if row is not None and row["cancel_requested"]:
            cancel_state["requested"] = True

    def _should_cancel() -> bool:
        return cancel_state["requested"]

    try:
        # Resolved inside the try, not above it: the row is already
        # 'preparing' with started_at set by this point, so a ShareEscape
        # (or a TypeError from a null source_path on a job rebuilt from an
        # older edition row) raised out here would escape past the failure
        # handler and strand the row on "Working..." until a process
        # restart, while count_preparing_ahead kept counting it against
        # every later add.
        root = Path(settings.share_root)
        source = safe_join(root, job.source_rel_path)

        work_dir = Path(settings.work_dir)
        scratch_video = work_dir / f"{job.edition_id}.mp4"
        scratch_poster = work_dir / f"{job.edition_id}.jpg"

        probe_json = await asyncio.to_thread(compat.probe, source)
        moov_first = await asyncio.to_thread(compat.moov_before_mdat, source)
        verdict = compat.analyze(
            probe_json,
            selected_audio_index=job.audio_stream_index,
            moov_first=moov_first,
        )

        source_size = verdict.size_bytes
        if not source_size:
            source_size = await asyncio.to_thread(lambda: os.stat(source).st_size)

        free_bytes = await asyncio.to_thread(
            lambda: shutil.disk_usage(settings.work_dir).free
        )
        required = required_free_bytes(source_size, already_correct=verdict.already_correct)
        if free_bytes < required:
            raise _InsufficientSpaceError(
                f"{free_bytes} free bytes < {required} required in {settings.work_dir}"
            )

        started_at = time.monotonic()
        try:
            if verdict.already_correct:
                uploaded_path = source
                await _upload(source, job.object_key, heartbeat=_heartbeat)
            else:
                cmd = remux.build_remux_command(
                    source,
                    scratch_video,
                    audio_stream_index=job.audio_stream_index,
                    reencode_audio=verdict.audio_needs_reencode,
                    channels=job.channels,
                )
                # `-progress pipe:1` is what the shared runner reads its
                # blocks from -- `build_remux_command` stays a pure
                # function with no knowledge of the runner, so this is
                # appended here, the same place transcode.py's own command
                # builder puts it, right before the destination argument.
                cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]

                async def on_progress(_progress, percent):
                    await _heartbeat(stage="working", progress_pct=percent)

                result = await run_ffmpeg_with_progress(
                    cmd,
                    duration_seconds=verdict.duration_seconds,
                    on_progress=on_progress,
                    should_cancel=_should_cancel,
                    timeout=REMUX_TIMEOUT_SECONDS,
                )
                if result.cancelled:
                    raise _JobCancelled()

                uploaded_path = scratch_video
                await _upload(scratch_video, job.object_key, heartbeat=_heartbeat)

            # `editions.size_bytes` is a fact about the stored object, and
            # in the remux branch that is ffmpeg's output, not the source:
            # a different container layout, and materially different
            # whenever the audio was re-encoded. Taken here, before the
            # finally below removes the scratch file.
            uploaded_size = await asyncio.to_thread(
                lambda: os.stat(uploaded_path).st_size
            )
        finally:
            if await asyncio.to_thread(scratch_video.exists):
                await asyncio.to_thread(scratch_video.unlink)

        try:
            at_seconds = remux.poster_timestamp(verdict.duration_seconds)
            poster_cmd = remux.build_poster_command(
                source, scratch_poster, at_seconds=at_seconds
            )
            await asyncio.to_thread(
                remux.run_ffmpeg, poster_cmd, timeout=POSTER_TIMEOUT_SECONDS
            )
            candidate_poster_key = f"{job.edition_id}.jpg"
            await _upload(scratch_poster, candidate_poster_key, "image/jpeg", heartbeat=_heartbeat)
            poster_key = candidate_poster_key
        except Exception:
            # A poster that will not extract must not fail an add that
            # otherwise worked -- the row just degrades to the missing-
            # thumbnail placeholder (D-72).
            logger.exception(
                "poster extraction failed for edition %s", job.edition_id
            )
            poster_key = None
        finally:
            if await asyncio.to_thread(scratch_poster.exists):
                await asyncio.to_thread(scratch_poster.unlink)

        selected_track = next(
            (t for t in verdict.tracks if t.index == job.audio_stream_index), None
        )
        async with pool.acquire() as conn:
            await queries.set_edition_technical(
                conn,
                job.edition_id,
                size_bytes=uploaded_size,
                duration_seconds=verdict.duration_seconds or 0.0,
                width=verdict.width or 0,
                height=verdict.height or 0,
                audio_language=selected_track.language if selected_track else None,
                audio_title=selected_track.title if selected_track else None,
                audio_reencoded=verdict.audio_needs_reencode,
                # The four video parameters come straight from the verdict:
                # the video stream is always copied, so its parameters
                # already describe the stored object. The audio sample rate
                # and channel layout come straight from the selected track
                # for the same reason -- no downmix and no resample argument
                # is ever emitted. The audio codec is reconciled to what the
                # remux actually stores (compat.effective_audio_codec), since
                # the probed source codec is not what ends up in the bucket
                # whenever the track is re-encoded.
                fps=verdict.fps,
                video_profile=verdict.video_profile,
                video_level=verdict.video_level,
                pix_fmt=verdict.pix_fmt,
                audio_codec=(
                    compat.effective_audio_codec(
                        selected_track.codec, verdict.audio_needs_reencode
                    )
                    if selected_track
                    else None
                ),
                audio_sample_rate=selected_track.sample_rate if selected_track else None,
                audio_channel_layout=selected_track.channel_layout if selected_track else None,
            )
            if poster_key is not None:
                await queries.set_edition_poster(conn, job.edition_id, poster_key)
            await queries.mark_title_active(conn, job.title_id)
            await queries.finish_job(
                conn,
                job.job_id,
                worker_name=ADD_CONSUMER_WORKER_NAME,
                attempt=job.attempt,
                outcome="done",
            )

        elapsed = time.monotonic() - started_at
        logger.info("add %s finished in %.1fs", job.title_id, elapsed)
    except _JobCancelled:
        logger.info("add %s cancelled", job.title_id)
        await _cleanup_orphans(job.object_key, poster_key)
        async with pool.acquire() as conn:
            # A cancelled title's address never went live (T-04-32) -- it
            # is hard-deleted here rather than marked 'failed', with no
            # tombstone, the same guard and the same ordering (objects
            # already cleaned up above, rows dropped only now)
            # routes_titles.cancel_route uses for a queued cancellation.
            # Best-effort: a title that has somehow moved on since (or
            # was already removed by a concurrent purge) is not this
            # cleanup's problem to report.
            try:
                await queries.delete_never_active_title(conn, job.title_id)
            except (LookupError, ValueError):
                pass
            await queries.finish_job(
                conn,
                job.job_id,
                worker_name=ADD_CONSUMER_WORKER_NAME,
                attempt=job.attempt,
                outcome="cancelled",
            )
    except Exception as exc:
        logger.exception("add %s failed", job.title_id)

        # Unconditional on every failure, whether or not the upload ever
        # started: abort_dangling_multipart covers the upload-died-partway
        # case (an incomplete multipart upload), delete_object covers the
        # upload-completed-then-a-later-step-failed case, and both are
        # safe no-ops against a key that was never touched (T-02-08).
        await _cleanup_orphans(job.object_key, poster_key)

        reason_key = _reason_key_for(exc)
        async with pool.acquire() as conn:
            await queries.mark_title_failed(conn, job.title_id, reason_key)
            await queries.finish_job(
                conn,
                job.job_id,
                worker_name=ADD_CONSUMER_WORKER_NAME,
                attempt=job.attempt,
                outcome="failed",
                reason_key=reason_key,
            )
