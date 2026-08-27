"""One-at-a-time in-process background worker for the add pipeline
(D-49/D-50) -- mirrors `orchestrator.reaper`'s shape: a module logger,
per-item exception isolation so one bad add can never stop future adds
from draining, and every synchronous call offloaded to a thread.

**Nothing synchronous is called bare from this module.** `AddQueue.run`
awaits `process_add` on the same event loop that also serves every HTTP
request in this process. `run_ffmpeg` is `subprocess.run`;
`put_object_from_file` is boto3's managed `upload_file`. Both block for as
long as the work takes -- called directly, an eight-gigabyte source stalls
the whole service for minutes: the library poll never returns, `GET
/v/{slug}` never returns, and `/healthz` never returns, which the compose
health check (five-second interval, three-second timeout) reads as the
container being down, taking `edge`'s `depends_on: service_healthy`
condition down with it. Every blocking call in this module -- ffmpeg,
every storage operation, the disk check, the source stat -- goes through
`asyncio.to_thread`. State the rule here, once, so the next person adding
a step follows it instead of rediscovering it from a support ticket.

**Failure leaves nothing behind (D-53).** Any failure past the point the
title flips to 'preparing'/started can leave an object in the bucket two
different ways: the upload died partway (an *incomplete* multipart upload,
cleared by `abort_dangling_multipart`) or the upload **completed** and a
later step failed (a fully-present object nothing will ever point at
again, cleared by `delete_object`). Both calls are safe to run
unconditionally on every failure -- `delete_object` on a key that was
never uploaded is a no-op against S3 -- so this module always runs both,
for the video key and, when one was uploaded, the poster key, before the
row is marked failed. Doing the cleanup before the mark means a crash
between the two leaves the row still saying "preparing", and the startup
sweep (`orchestrator.main`) re-runs the same cleanup rather than losing
the key.
"""
import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media import compat, remux
from orchestrator.media.add import required_free_bytes
from orchestrator.media.compat import ProbeError
from orchestrator.media.share import ShareEscape, safe_join
from orchestrator.storage import abort_dangling_multipart, delete_object, put_object_from_file

logger = logging.getLogger(__name__)

# ffmpeg on an eight-gigabyte rip is measured in minutes, not seconds --
# generous but not unbounded, so a genuinely hung ffmpeg process cannot
# wedge the worker forever.
REMUX_TIMEOUT_SECONDS = 3600
POSTER_TIMEOUT_SECONDS = 60


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


async def _upload(path, key: str, content_type: str = "video/mp4") -> None:
    """One upload, with its failures labelled. The only place
    `put_object_from_file` is called from `process_add`.
    """
    try:
        await asyncio.to_thread(put_object_from_file, str(path), key, content_type)
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
    if isinstance(exc, remux.RemuxError):
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

    `from_row` is the single constructor: the add route and the startup
    re-enqueue both build a job the same way, from the same
    `queries.job_row` shape, so there is exactly one shape a job can have.
    The alternative -- carrying analyzer-derived fields from the route
    into this dataclass -- needs a second construction path for rows
    recovered after a restart, and two paths that must agree eventually
    do not. One extra `ffprobe` costs a fraction of a second against an
    upload measured in minutes.
    """

    title_id: object
    edition_id: object
    object_key: str
    source_rel_path: str
    audio_stream_index: int
    channels: int

    @classmethod
    def from_row(cls, title_id, row: asyncpg.Record) -> "AddJob":
        return cls(
            title_id=title_id,
            edition_id=row["edition_id"],
            object_key=row["object_key"],
            source_rel_path=row["source_path"],
            audio_stream_index=row["audio_stream_index"],
            channels=row["audio_channels"],
        )


class AddQueue:
    """Wraps an unbounded `asyncio.Queue`. Concurrency of one by
    construction -- exactly one worker task ever calls `.get()` on it, so
    there is no semaphore to get wrong.
    """

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[AddJob]" = asyncio.Queue()

    async def enqueue(self, job: AddJob) -> None:
        await self._queue.put(job)

    async def run(self, pool: asyncpg.Pool) -> None:
        while True:
            job = await self._queue.get()
            try:
                await process_add(pool, job)
            except Exception:
                logger.exception("add %s failed", job.title_id)
            finally:
                self._queue.task_done()


async def process_add(pool: asyncpg.Pool, job: AddJob) -> None:
    """Remux (or upload as-is) one edition end to end, starting from a
    title already sitting in the catalog as 'preparing'.

    1. `mark_title_started` first, before any work -- this is what
       separates "waiting" from "working" for the interface, and what
       lets restart reconciliation tell a row that was mid-remux from one
       that had not begun.
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
       build the remux command, run it, upload the result, and remove the
       scratch file whether the upload succeeded or not. Either way the
       size recorded on the edition is stat'ed from the artifact that was
       actually uploaded, which in the remux branch is not the source.
    6. Extract the poster in its own try/except: a poster that will not
       extract must not fail an add that otherwise worked.
    7. Record the technical detail on the edition and flip the title to
       active.

    Any failure past this point runs the abort-then-delete cleanup pair
    (see the module docstring) for the video key and, when one was
    uploaded, the poster key, before the row is marked failed with a
    translation catalog key rather than an English sentence.
    """
    settings = get_settings()
    async with pool.acquire() as conn:
        await queries.mark_title_started(conn, job.title_id)

    poster_key: str | None = None

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
                await _upload(source, job.object_key)
            else:
                cmd = remux.build_remux_command(
                    source,
                    scratch_video,
                    audio_stream_index=job.audio_stream_index,
                    reencode_audio=verdict.audio_needs_reencode,
                    channels=job.channels,
                )
                await asyncio.to_thread(
                    remux.run_ffmpeg, cmd, timeout=REMUX_TIMEOUT_SECONDS
                )
                uploaded_path = scratch_video
                await _upload(scratch_video, job.object_key)

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
            await _upload(scratch_poster, candidate_poster_key, "image/jpeg")
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

        elapsed = time.monotonic() - started_at
        logger.info("add %s finished in %.1fs", job.title_id, elapsed)
    except Exception as exc:
        logger.exception("add %s failed", job.title_id)

        # Unconditional on every failure, whether or not the upload ever
        # started: abort_dangling_multipart covers the upload-died-partway
        # case (an incomplete multipart upload), delete_object covers the
        # upload-completed-then-a-later-step-failed case, and both are
        # safe no-ops against a key that was never touched (T-02-08).
        await asyncio.to_thread(abort_dangling_multipart, job.object_key)
        await asyncio.to_thread(delete_object, job.object_key)
        if poster_key is not None:
            await asyncio.to_thread(abort_dangling_multipart, poster_key)
            await asyncio.to_thread(delete_object, poster_key)

        async with pool.acquire() as conn:
            await queries.mark_title_failed(conn, job.title_id, _reason_key_for(exc))
