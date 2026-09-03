"""The claim-probe-build-run-upload-report loop -- one job at a time, on
the converter's own event loop, forever until asked to stop.

Every turn: read the card's free memory, check in (recording that the
converter is alive whether or not it claims anything), claim at most one
job, re-read the source file from the moment it starts, build the
command from that fresh reading with the same shared code the add form
used, run it, verify what it produced, upload with the converter's own
credential, and report the outcome. Nothing here trusts what the service
sent as a command fragment -- only the operator's own choices (which
audio track, which subtitle, which layout) cross the network; everything
else is rebuilt from the file as it is right now.
"""
import asyncio
import logging
import shutil
import signal
from pathlib import Path

from mytube_media import compat, transcode
from mytube_media.compat import ProbeError
from mytube_media.runner import FfmpegFailed, run_ffmpeg_with_progress
from mytube_media.share import ShareEscape, safe_join
from mytube_media.subtitles import (
    collect_fonts,
    embedded_subtitle_kind,
    read_text,
    subtitle_ordinal,
)
from mytube_media.transcode import (
    SubtitleChoice,
    TranscodePlan,
    TranscodeRefused,
    bitrate_for_output,
    build_transcode_command,
    gop_for,
    output_dimensions,
    required_free_bytes_for_transcode,
)

from worker import gpu
from worker import storage as worker_storage
from worker.client import AuthenticationRejected, ReportRejected, WorkerClient
from worker.settings import WorkerSettings

logger = logging.getLogger(__name__)

_BYTES_PER_MIB = 1024 * 1024

# Generous enough for a long film, finite so a genuinely wedged encode
# cannot occupy the card forever -- the same "bounded, not unbounded"
# discipline mytube_media.runner's own timeout argument already carries.
TRANSCODE_TIMEOUT_SECONDS = 10800

# The dotted catalog keys this program is allowed to report on a
# failure -- the service's own allow-list
# (orchestrator/web/routes_worker.py's ALLOWED_FAILURE_REASONS), repeated
# here rather than imported, since this package imports nothing from
# orchestrator. "failed.reason.crashed_converter" is deliberately absent:
# it is written by the service's own stale-heartbeat sweep, never by this
# program.
_ALLOWED_FAILURE_REASONS = frozenset(
    {
        "failed.reason.gpu_out_of_memory",
        "failed.reason.probe_mismatch",
        "failed.reason.protocol_mismatch",
        "failed.reason.unreadable_source",
        "failed.reason.disk_full",
        "failed.reason.upload",
        "failed.reason.unknown",
    }
)


class _InsufficientSpaceError(Exception):
    """Internal signal only. The free-space precheck failed against the
    scratch directory before anything was started -- not the source's
    size (the source is read over the share and never copied), the
    output's.
    """


class _ProbeMismatchError(Exception):
    """Internal signal only. What the job claimed (an audio track, a
    subtitle choice, a layout) does not match a fresh reading of the file
    right now -- the file changed, or the choice never existed on it.
    """


class _OutputVerificationError(Exception):
    """Internal signal only. The file this converter just produced failed
    one of the two checks every file this project publishes has to pass:
    it must decode, and it must be laid out so a player can start before
    the download finishes.
    """


class _UploadError(Exception):
    """Internal signal only. Wraps whatever the storage client raised at
    the one upload call site.
    """


class _JobCancelled(Exception):
    """Internal signal only. Raised once `run_ffmpeg_with_progress`
    reports that either the operator's own cancel request or this
    program's own shutdown stopped the run.
    """


def _reason_key_for(exc: Exception) -> str:
    """Map a failure to one of the service's allowed catalog keys -- a
    raw tool message must never reach a report.
    """
    if isinstance(exc, _InsufficientSpaceError):
        return "failed.reason.disk_full"
    if isinstance(exc, (ShareEscape, ProbeError, OSError)):
        return "failed.reason.unreadable_source"
    if isinstance(exc, (_ProbeMismatchError, TranscodeRefused)):
        return "failed.reason.probe_mismatch"
    if isinstance(exc, _OutputVerificationError):
        return "failed.reason.unknown"
    if isinstance(exc, FfmpegFailed):
        # The card's own error text is otherwise opaque -- "conversion
        # failed" tells an operator nothing about whether waiting would
        # help. Matched against ffmpeg's own NVENC error strings
        # ("out of memory", "encoder busy") rather than a captured real
        # log line from this project's own card -- no GPU is reachable
        # from where this module is written, so this pairing is a
        # documented best effort, not a measurement. See this plan's own
        # Summary for the note to revisit once a real failure of this
        # kind has actually been seen and logged.
        text = exc.stderr_tail.lower()
        if "out of memory" in text or "encoder busy" in text:
            return "failed.reason.gpu_out_of_memory"
        return "failed.reason.unknown"
    if isinstance(exc, _UploadError):
        return "failed.reason.upload"
    return "failed.reason.unknown"


async def _abort_upload_quietly(s3_client, settings: WorkerSettings, key: str, job_id) -> None:
    """The one way this program aborts a dangling upload -- every cleanup
    path goes through here rather than calling `abort_multipart` directly.

    Cleanup runs after the outcome is already decided, so a storage error
    in it must not become the outcome. Left to propagate it does real
    damage: a wrong storage region made the abort raise
    SignatureDoesNotMatch, that escaped the job, killed the process and
    got the container restarted -- and the job it was cleaning up after
    stayed `running` with nobody left to report it, until the service's
    stale sweep noticed minutes later. Logged with the job id, swallowed,
    and the report the caller was about to send still goes out.
    """
    try:
        await asyncio.to_thread(worker_storage.abort_multipart, s3_client, settings, key)
    except Exception:
        logger.exception("job %s: could not abort the dangling upload of %s", job_id, key)


async def _process_job(
    client: WorkerClient,
    s3_client,
    settings: WorkerSettings,
    job: dict,
    scratch_root: Path,
    shutdown_event: asyncio.Event,
) -> None:
    """Resolve, build, run, verify, upload and report one claimed job --
    the whole shape mirrors `orchestrator.media.queue.process_add`'s own
    try/except/finally: a single failure handler mapping every step's
    exception to a reason key, an unconditional cleanup before a failure
    is reported, and scratch removed on every path out.
    """
    job_id = job["job_id"]
    attempt = job["attempt"]
    object_key = job["object_key"]
    job_scratch = scratch_root / str(job_id)

    cancel_state = {"requested": False}

    def _should_stop() -> bool:
        return cancel_state["requested"] or shutdown_event.is_set()

    async def _heartbeat(
        *, stage: str, progress_pct: float | None = None, realtime_factor: float | None = None
    ) -> None:
        # A heartbeat that cannot reach the service does not stop the
        # encode -- ten minutes of card time is not thrown away for a
        # blip the service side is already built to tolerate for a
        # bounded while (the reaper's own stale-job threshold).
        try:
            cancel_requested = await client.heartbeat(
                job_id,
                attempt=attempt,
                stage=stage,
                progress_pct=progress_pct,
                realtime_factor=realtime_factor,
            )
        except Exception:
            logger.warning("job %s: heartbeat did not reach the service", job_id, exc_info=True)
            return
        if cancel_requested:
            cancel_state["requested"] = True

    try:
        job_scratch.mkdir(parents=True, exist_ok=True)

        root = Path(settings.share_root)
        source = safe_join(root, job["source_path"])

        subtitle_kind = job.get("subtitle_kind") or "none"
        subtitle_stream_index = job.get("subtitle_stream_index")
        subtitle_rel_path = job.get("subtitle_path")
        subtitle_abs_path = None
        if subtitle_kind == "external":
            if not subtitle_rel_path:
                raise _ProbeMismatchError(
                    "an external subtitle was chosen but no path was recorded"
                )
            subtitle_abs_path = safe_join(root, subtitle_rel_path)

        probe_json = await asyncio.to_thread(compat.probe, source)

        if subtitle_kind == "embedded":
            # "embedded" is the operator's choice, not a kind this
            # converter can burn in: a text track and a bitmap track take
            # two different filter branches. Resolved here, against the
            # file as it is right now, because leaving it unresolved is
            # silent -- the job reports a burned-in subtitle and the file
            # it produces carries none.
            resolved_kind = embedded_subtitle_kind(probe_json, subtitle_stream_index)
            if resolved_kind is None:
                raise _ProbeMismatchError(
                    f"subtitle stream {subtitle_stream_index} is no longer present"
                )
            subtitle_kind = resolved_kind

        subtitle_si = None
        if subtitle_kind == "embedded_text":
            # The subtitles= filter always needs a filename to read from,
            # even for a track embedded in the file being decoded --
            # mytube_media.transcode.SubtitleChoice's own docstring. The
            # ordinal beside it comes from this fresh probe, never from the
            # job: it is what the filter's si option counts, and it is only
            # knowable from the file's current stream list.
            subtitle_si = subtitle_ordinal(probe_json, subtitle_stream_index)
            if subtitle_si is None:
                raise _ProbeMismatchError(
                    f"subtitle stream {subtitle_stream_index} is no longer present"
                )
            subtitle_abs_path = source
        verdict = compat.analyze(
            probe_json,
            selected_audio_index=job["audio_stream_index"],
            filename=Path(job["source_path"]).name,
        )

        selected_track = next(
            (t for t in verdict.tracks if t.index == job["audio_stream_index"]), None
        )
        if selected_track is None:
            raise _ProbeMismatchError(
                f"audio stream {job['audio_stream_index']} is no longer present"
            )

        # The layout is the operator's own choice, carried in the job --
        # not re-detected here. It was already confirmed against the file
        # once, at add time (D-139's selector); re-guessing it now would
        # let a second, independent detection silently disagree with what
        # the operator actually picked.
        stereo_layout = job.get("stereo_layout") or "2d"

        out_width, out_height = output_dimensions(
            verdict.width or 0, verdict.height or 0, stereo_layout
        )
        video_bitrate_bps = bitrate_for_output(out_width, out_height, stereo_layout)
        gop = gop_for(verdict.fps)

        video_stream = next(
            (s for s in probe_json["streams"] if s.get("codec_type") == "video"), None
        )
        source_codec = video_stream.get("codec_name") if video_stream else None
        sample_aspect_ratio = video_stream.get("sample_aspect_ratio") if video_stream else None

        plan = TranscodePlan(
            width=out_width,
            height=out_height,
            sample_aspect_ratio=sample_aspect_ratio,
            video_bitrate_bps=video_bitrate_bps,
            maxrate_bps=round(video_bitrate_bps * transcode.MAXRATE_RATIO),
            bufsize_bps=round(video_bitrate_bps * transcode.BUFSIZE_RATIO),
            gop=gop,
            hwaccel="cuda" if source_codec in transcode.NVDEC_SUPPORTED_CODECS else None,
            audio_stream_index=job["audio_stream_index"],
            audio_channels=selected_track.channels,
            reencode_audio=verdict.audio_needs_reencode,
            stereo_layout=stereo_layout,
        )

        font_dir = None
        charenc = None
        if subtitle_kind in ("embedded_text", "external"):
            font_dir = await asyncio.to_thread(
                collect_fonts,
                source,
                subtitle_abs_path if subtitle_kind == "external" else None,
                job_scratch,
            )
            if subtitle_kind == "external":
                _, charenc = await asyncio.to_thread(read_text, subtitle_abs_path)

        subtitle = SubtitleChoice(
            kind=subtitle_kind,
            stream_index=subtitle_stream_index,
            subtitle_ordinal=subtitle_si,
            path=str(subtitle_abs_path) if subtitle_abs_path else None,
            font_dir=font_dir,
            charenc=charenc,
        )

        destination = job_scratch / "output.mp4"
        cmd = build_transcode_command(source, destination, plan=plan, subtitle=subtitle)

        # Not the source's size -- the source is read over the share and
        # never copied. What needs room is the encoder's own output plus
        # the +faststart rewrite that follows it, after the expensive
        # part (the encode itself) has already finished.
        required = required_free_bytes_for_transcode(
            video_bitrate_bps, verdict.duration_seconds or 0.0
        )
        free_bytes = await asyncio.to_thread(lambda: shutil.disk_usage(settings.work_dir).free)
        if free_bytes < required:
            raise _InsufficientSpaceError(
                f"{free_bytes} free bytes < {required} required in {settings.work_dir}"
            )

        async def on_progress(progress, percent) -> None:
            await _heartbeat(stage="working", progress_pct=percent, realtime_factor=progress.speed)

        result = await run_ffmpeg_with_progress(
            cmd,
            duration_seconds=verdict.duration_seconds,
            on_progress=on_progress,
            should_cancel=_should_stop,
            timeout=TRANSCODE_TIMEOUT_SECONDS,
        )
        if result.cancelled:
            raise _JobCancelled()

        # The same two checks this project already runs on every file it
        # produces before publishing it: it must decode, and its moov
        # atom must precede mdat so a player can start before the
        # download finishes.
        try:
            await asyncio.to_thread(compat.probe, destination)
            moov_first = await asyncio.to_thread(compat.moov_before_mdat, destination)
        except ProbeError as exc:
            raise _OutputVerificationError(str(exc)) from exc
        if not moov_first:
            raise _OutputVerificationError(f"{destination}: the moov atom is not first")

        await _heartbeat(stage="uploading")
        try:
            await asyncio.to_thread(
                worker_storage.upload_file, s3_client, settings, str(destination), object_key
            )
        except Exception as exc:
            raise _UploadError(str(exc)) from exc

        try:
            await client.done(job_id, attempt=attempt, object_key=object_key)
        except ReportRejected:
            # The job moved on to a different attempt. This converter has
            # no delete beyond its own keys (D-115): only abort a dangling
            # multipart upload of its own -- the service removes the
            # completed object itself, on this same refusal, since only it
            # holds the credential wide enough to do that.
            await _abort_upload_quietly(s3_client, settings, object_key, job_id)
            logger.info(
                "job %s: the done report was refused (superseded) -- our own dangling "
                "upload was aborted; the service removes the completed object",
                job_id,
            )

    except _JobCancelled:
        await _abort_upload_quietly(s3_client, settings, object_key, job_id)
        if shutdown_event.is_set():
            # Shutting down: the heartbeat going stale is what the
            # service is already built to handle, and attempting a report
            # here only delays the exit.
            logger.info("job %s: stopped for shutdown, not reported", job_id)
        else:
            logger.info("job %s: cancelled", job_id)
            try:
                await client.cancelled(job_id, attempt=attempt)
            except ReportRejected:
                logger.info("job %s: cancellation report refused (already superseded)", job_id)
            except Exception:
                logger.exception("job %s: could not report the cancellation", job_id)
    except Exception as exc:
        logger.exception("job %s: failed", job_id)
        # Unconditional, whether or not an upload was ever attempted --
        # abort_multipart on a key with nothing dangling is a safe no-op.
        await _abort_upload_quietly(s3_client, settings, object_key, job_id)
        reason_key = _reason_key_for(exc)
        try:
            await client.failed(job_id, attempt=attempt, reason_key=reason_key)
        except ReportRejected:
            logger.info("job %s: failure report refused (already superseded)", job_id)
        except Exception:
            logger.exception("job %s: could not report the failure (reason=%s)", job_id, reason_key)
    finally:
        await asyncio.to_thread(shutil.rmtree, job_scratch, ignore_errors=True)


async def _check_in(
    client: WorkerClient, shutdown_event: asyncio.Event, *, status: str
) -> dict | None:
    """One claim call, tolerant of the service being unreachable -- a
    poller calling in every few seconds should never stop the loop for a
    condition that resolves itself on the next poll. A refused credential
    is the one exception that is not tolerated: it will not resolve
    itself, and the operator needs to see it, not have it retried away.

    Raced against `shutdown_event`: the client's own bounded retry can
    run for minutes against an address that never answers, and a polite
    shutdown must not sit through the rest of that window -- the claim
    call is cancelled outright the moment shutdown is requested, and this
    returns `None` (the same "nothing to do" answer an empty queue gives)
    so the caller's own loop condition is what actually ends the turn.
    """
    claim_task = asyncio.ensure_future(client.claim(status=status))
    shutdown_task = asyncio.ensure_future(shutdown_event.wait())
    done, _pending = await asyncio.wait(
        {claim_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if claim_task not in done:
        claim_task.cancel()
        shutdown_task.cancel()
        return None
    shutdown_task.cancel()
    try:
        return claim_task.result()
    except AuthenticationRejected:
        raise
    except Exception:
        logger.exception("check-in failed")
        return None


async def _sleep_or_wake(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep for the poll interval, but wake immediately if shutdown is
    requested mid-sleep -- a plain sleep would make a polite exit wait
    out the full interval even when nothing is running.
    """
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _run(
    client: WorkerClient, s3_client, settings: WorkerSettings, shutdown_event: asyncio.Event
) -> None:
    """One turn at a time, forever, until `shutdown_event` is set. One
    job at a time, by construction: one loop, one claim, nothing here
    starts a second job before this one finishes, and there is no setting
    for it.
    """
    scratch_root = Path(settings.work_dir)
    while not shutdown_event.is_set():
        try:
            free_bytes = await asyncio.to_thread(gpu.free_vram_bytes)
        except gpu.GpuQueryError as exc:
            # A card that cannot be queried at all is a misconfiguration,
            # not a wait: logged loudly, and checked in as idle rather
            # than as waiting_for_gpu, since "waiting for room" would be a
            # claim this converter cannot back up with any actual card
            # state.
            logger.error("nvidia-smi could not be read: %s", exc)
            job = await _check_in(client, shutdown_event, status="idle")
            if job is not None:
                await _process_job(client, s3_client, settings, job, scratch_root, shutdown_event)
                continue
            await _sleep_or_wake(settings.worker_poll_seconds, shutdown_event)
            continue

        floor_bytes = settings.worker_vram_min_mib * _BYTES_PER_MIB
        if free_bytes < floor_bytes:
            # The card is shared with other services -- claiming a job
            # that will then fail for memory burns one of its three
            # attempts on a condition that resolves itself. Still calling
            # in: silence here reads as a converter that is switched off,
            # not as a card that is busy.
            logger.info(
                "waiting_for_gpu: %d MiB free, floor is %d MiB",
                free_bytes // _BYTES_PER_MIB,
                settings.worker_vram_min_mib,
            )
            await _check_in(client, shutdown_event, status="waiting_for_gpu")
            await _sleep_or_wake(settings.worker_poll_seconds, shutdown_event)
            continue

        job = await _check_in(client, shutdown_event, status="idle")
        if job is None:
            await _sleep_or_wake(settings.worker_poll_seconds, shutdown_event)
            continue

        await _process_job(client, s3_client, settings, job, scratch_root, shutdown_event)


def _clean_scratch(scratch_root: Path) -> None:
    """Remove anything a previous run left behind under `scratch_root` --
    a crash mid-job can leave a job-id-named directory with a partial
    output file in it.
    """
    for child in scratch_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = WorkerSettings()

    # A lever, not a resolution setting (see worker/settings.py's own
    # comment on this field): mytube_media.transcode reads this as a
    # plain module constant, so this is the one place the converter's own
    # override takes effect.
    transcode.MAX_OUTPUT_WIDTH = settings.worker_max_output_width

    scratch_root = Path(settings.work_dir)
    scratch_root.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_clean_scratch, scratch_root)

    try:
        free_bytes = await asyncio.to_thread(gpu.free_vram_bytes)
        card_state = f"{free_bytes // _BYTES_PER_MIB} MiB free"
    except gpu.GpuQueryError as exc:
        card_state = f"could not be read ({exc})"

    logger.info(
        "worker %r starting: service %s, card %s",
        settings.worker_name,
        settings.orchestrator_url,
        card_state,
    )

    client = WorkerClient(
        base_url=settings.orchestrator_url,
        token=settings.worker_token,
        worker_name=settings.worker_name,
    )
    s3_client = worker_storage.build_client(settings)

    shutdown_event = asyncio.Event()

    def _handle_sigterm() -> None:
        logger.info("SIGTERM received -- stopping the current step, then exiting; not reporting")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_sigterm)
        except NotImplementedError:
            # Not available on every platform this module might be
            # imported on for a quick check -- the real deployment target
            # is Linux, where this is always available.
            pass

    try:
        await _run(client, s3_client, settings, shutdown_event)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
