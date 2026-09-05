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
import contextlib
import logging
import shutil
import signal
import time
from pathlib import Path

from mytube_media import compat, remux, transcode
from mytube_media.compat import ProbeError
from mytube_media.runner import FfmpegFailed, run_ffmpeg_with_progress
from mytube_media.share import ShareEscape, safe_join
from mytube_media.subtitles import (
    SubtitleExtractError,
    SubtitleReadError,
    collect_fonts,
    copy_external_track,
    embedded_subtitle_kind,
    embedded_tracks,
    extract_embedded_track,
    extraction_extension,
    read_text,
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
from worker.client import AuthenticationRejected, JobLost, ReportRejected, WorkerClient
from worker.settings import WorkerSettings

logger = logging.getLogger(__name__)

_BYTES_PER_MIB = 1024 * 1024

# Generous enough for a long film, finite so a genuinely wedged encode
# cannot occupy the card forever -- the same "bounded, not unbounded"
# discipline mytube_media.runner's own timeout argument already carries.
TRANSCODE_TIMEOUT_SECONDS = 10800

# Deliberately below the library row's own 4-second poll interval, not a
# round "every 30 seconds" figure: the phase measured a cancel reaching
# the card in 4.6 seconds end to end, and the row's own progress must
# never visibly stand still for longer than one poll. A request every
# three seconds from one converter costs the service nothing -- what it
# buys is a heartbeat that covers the upload too, closing the gap where a
# transfer running longer than the reaper's stale threshold used to get
# its own job requeued out from under it.
HEARTBEAT_INTERVAL_SECONDS = 3.0

# Same figure the add-consumer's own poster extraction uses
# (orchestrator/media/queue.py) -- one frame, nothing that should ever
# take this long against a file already sitting on local disk.
POSTER_TIMEOUT_SECONDS = 60

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
    if isinstance(
        exc, (ShareEscape, ProbeError, OSError, SubtitleExtractError, SubtitleReadError)
    ):
        # A subtitle that could not be extracted or copied is, in effect,
        # a source this converter could not read for what it needed --
        # the same reason an unreachable or unreadable video file gets.
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
        # log line from this project's own card: no GPU is reachable from
        # where this module is written, so this pairing is a best effort
        # to be revisited once a real out-of-memory failure has actually
        # been seen and logged.
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

    # "requested" is what `_should_stop` polls -- set by an operator's own
    # cancel (through a heartbeat's answer) or by a not-found answer.
    # "job_lost" distinguishes the second case from the first once the
    # run has actually stopped: an operator's cancel still reports
    # `cancelled`, a not-found reports nothing at all, because the
    # service has already told this attempt it does not own the job.
    cancel_state = {"requested": False, "job_lost": False}

    # Written by the progress callback, read by the heartbeat loop --
    # nothing else passes progress between the two. The callback never
    # awaits the network itself; the loop is the only thing that ever
    # calls `client.heartbeat` for this job.
    progress_state: dict = {"stage": "working", "progress_pct": None, "realtime_factor": None}

    def _should_stop() -> bool:
        return cancel_state["requested"] or shutdown_event.is_set()

    async def _heartbeat_loop() -> None:
        """One heartbeat every `HEARTBEAT_INTERVAL_SECONDS`, reporting
        whatever `progress_state` currently holds -- covering every stage
        this job passes through, the upload included, since this loop
        does not care what the main path is doing. Cancelled deliberately
        by `_process_job` before its own terminal report is posted: a
        tick landing after that report has already committed would be
        answered not-found and misread as a lost job.
        """
        while True:
            try:
                cancel_requested = await client.heartbeat(
                    job_id,
                    attempt=attempt,
                    stage=progress_state["stage"],
                    progress_pct=progress_state["progress_pct"],
                    realtime_factor=progress_state["realtime_factor"],
                )
            except JobLost:
                # The service says this attempt no longer owns the job --
                # stop the run the same way an operator's cancel stops
                # it, but distinctly, so the caller reports nothing for
                # it afterward.
                cancel_state["requested"] = True
                cancel_state["job_lost"] = True
                return
            except Exception:
                # Every other heartbeat failure keeps today's behaviour:
                # logged, encode continues. Ten minutes of card time is
                # not thrown away for a blip the service side is already
                # built to tolerate for a bounded while (the reaper's own
                # stale-job threshold).
                logger.warning(
                    "job %s: heartbeat did not reach the service", job_id, exc_info=True
                )
            else:
                if cancel_requested:
                    cancel_state["requested"] = True
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    heartbeat_task = asyncio.ensure_future(_heartbeat_loop())

    async def _stop_heartbeat_loop() -> None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    try:
        job_scratch.mkdir(parents=True, exist_ok=True)

        root = Path(settings.share_root)
        source = safe_join(root, job["source_path"])

        subtitle_kind = job.get("subtitle_kind") or "none"
        subtitle_stream_index = job.get("subtitle_stream_index")
        subtitle_rel_path = job.get("subtitle_path")
        subtitle_share_path = None
        if subtitle_kind == "external":
            if not subtitle_rel_path:
                raise _ProbeMismatchError(
                    "an external subtitle was chosen but no path was recorded"
                )
            subtitle_share_path = safe_join(root, subtitle_rel_path)

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
        source_profile = video_stream.get("profile") if video_stream else None
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

        # Resolved to a scratch-local file for both text kinds -- the
        # burn-in filter reads a file this converter just wrote, never the
        # source container by a stream selector and never the operator's
        # own path on the share (CR-04/CR-05).
        subtitle_burn_path = None
        font_dir = None
        charenc = None
        if subtitle_kind == "embedded_text":
            extension = extraction_extension(probe_json, subtitle_stream_index)
            subtitle_burn_path = await asyncio.to_thread(
                extract_embedded_track,
                source,
                subtitle_stream_index,
                job_scratch,
                extension=extension,
            )
            font_dir = await asyncio.to_thread(collect_fonts, source, None, job_scratch)
            # Read from the same file the burn-in reads, the same as the
            # preview route already does for this kind -- without this the
            # conversion of an older Cyrillic-encoded embedded track would
            # burn in without a charenc term while its own preview showed
            # it correctly.
            _, charenc = await asyncio.to_thread(read_text, subtitle_burn_path)
        elif subtitle_kind == "external":
            subtitle_burn_path = await asyncio.to_thread(
                copy_external_track, subtitle_share_path, job_scratch
            )
            font_dir = await asyncio.to_thread(
                collect_fonts, source, subtitle_share_path, job_scratch
            )
            # The encoding is read from the copy, not the share original --
            # both carry the same bytes, but the copy is what actually gets
            # burned, and that is the file whose encoding matters here.
            _, charenc = await asyncio.to_thread(read_text, subtitle_burn_path)

        # What gets reported as burned, resolved from the same source
        # probe and the same enumeration `embedded_subtitle_kind` already
        # walked above -- never a second, independent read of the stream
        # list. An external file carries no language tag at all; its own
        # name is the only thing worth reporting in its place.
        subtitle_burned_language = None
        subtitle_burned_title = None
        if subtitle_kind in ("embedded_text", "embedded_bitmap"):
            burned_track = next(
                (
                    t
                    for t in embedded_tracks(probe_json)
                    if t.stream_index == subtitle_stream_index
                ),
                None,
            )
            if burned_track is not None:
                subtitle_burned_language = burned_track.language
                subtitle_burned_title = burned_track.title
        elif subtitle_kind == "external":
            subtitle_burned_title = Path(subtitle_share_path).name

        subtitle = SubtitleChoice(
            kind=subtitle_kind,
            stream_index=subtitle_stream_index,
            path=str(subtitle_burn_path) if subtitle_burn_path else None,
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
            # No network call here on purpose -- a service that never
            # answers must not delay the encode. The heartbeat loop reads
            # this same dictionary on its own timer and is the only thing
            # that ever posts a heartbeat for this job.
            progress_state["progress_pct"] = percent
            progress_state["realtime_factor"] = progress.speed

        # A monotonic reading either side of the run, not the last
        # progress block's own speed -- that reading is the tail of the
        # file and can differ several-fold from the mean, and twenty such
        # samples make a median of noise, which is exactly the number the
        # next add form's own estimate reads.
        encode_started_at = time.monotonic()
        result = await run_ffmpeg_with_progress(
            cmd,
            duration_seconds=verdict.duration_seconds,
            on_progress=on_progress,
            should_cancel=_should_stop,
            timeout=TRANSCODE_TIMEOUT_SECONDS,
        )
        encode_elapsed_seconds = time.monotonic() - encode_started_at
        if result.cancelled:
            raise _JobCancelled()

        # The same two checks this project already runs on every file it
        # produces before publishing it: it must decode, and its moov
        # atom must precede mdat so a player can start before the
        # download finishes. Kept this time, rather than discarded: this
        # probe is the one honest description of what the stored object
        # actually is -- reading the source a second time would describe
        # a file this converter did not produce.
        try:
            output_probe_json = await asyncio.to_thread(compat.probe, destination)
            moov_first = await asyncio.to_thread(compat.moov_before_mdat, destination)
        except ProbeError as exc:
            raise _OutputVerificationError(str(exc)) from exc
        if not moov_first:
            raise _OutputVerificationError(f"{destination}: the moov atom is not first")

        output_verdict = compat.analyze(output_probe_json)
        # The stored object's own audio track -- exactly one always
        # survives into the output (D-42), so the first (and only) entry
        # `audio_tracks` finds is it; `Verdict` itself carries the
        # reconciled codec/rate/layout but not the language/title tags,
        # which live on the track, not the format block.
        output_audio_track = next(iter(output_verdict.tracks), None)
        # Stat'ed here, before the scratch directory is removed in the
        # `finally` below -- this is the size of what was actually
        # uploaded, not a guess from the source.
        output_size_bytes = await asyncio.to_thread(lambda: destination.stat().st_size)

        # A poster extracted from the output, not the source: the output
        # is what the edition actually is, on local disk, carrying the
        # burned subtitles and the final geometry. Same timestamp helper
        # and command builder the as-is path already uses (D-72); a
        # poster that will not extract must never fail a conversion,
        # exactly as it never fails an add.
        scratch_poster = job_scratch / "poster.jpg"
        poster_key: str | None = None
        try:
            at_seconds = remux.poster_timestamp(output_verdict.duration_seconds)
            poster_cmd = remux.build_poster_command(
                destination, scratch_poster, at_seconds=at_seconds
            )
            await asyncio.to_thread(remux.run_ffmpeg, poster_cmd, timeout=POSTER_TIMEOUT_SECONDS)
            candidate_poster_key = f"{job['edition_id']}.jpg"
            await asyncio.to_thread(
                worker_storage.upload_file,
                s3_client,
                settings,
                str(scratch_poster),
                candidate_poster_key,
                "image/jpeg",
            )
            poster_key = candidate_poster_key
        except Exception:
            logger.exception("job %s: poster extraction failed", job_id)
            poster_key = None

        # Writing the dictionary is the whole of setting this stage --
        # the loop picks it up on its next tick and keeps reporting
        # throughout the transfer, which is the whole point (CR-01):
        # a multi-gigabyte upload is now covered the same as the encode.
        progress_state["stage"] = "uploading"
        try:
            await asyncio.to_thread(
                worker_storage.upload_file, s3_client, settings, str(destination), object_key
            )
        except Exception as exc:
            raise _UploadError(str(exc)) from exc

        # The job's own average speed -- the source duration over the
        # wall time the encode actually took. Falls back to the last
        # progress reading only when the source's own duration could not
        # be read at all, which never happens for a file that already
        # made it through the free-space estimate above.
        if verdict.duration_seconds and encode_elapsed_seconds > 0:
            realtime_factor = verdict.duration_seconds / encode_elapsed_seconds
        else:
            realtime_factor = progress_state["realtime_factor"] or 1.0

        # Stopped deliberately here, before the terminal report: a tick
        # landing after this report has already committed would be
        # answered not-found and misread as a lost job.
        await _stop_heartbeat_loop()
        if cancel_state["job_lost"]:
            # The service told us during the upload that this attempt no
            # longer owns the job -- a converter that has been told that
            # must not then post an outcome for it.
            logger.info(
                "job %s: the service said this attempt no longer owns the job -- "
                "the done report was not sent",
                job_id,
            )
            await _abort_upload_quietly(s3_client, settings, object_key, job_id)
        else:
            try:
                await client.done(
                    job_id,
                    attempt=attempt,
                    object_key=object_key,
                    source_codec=source_codec,
                    source_profile=source_profile,
                    subtitle_burned_language=subtitle_burned_language,
                    subtitle_burned_title=subtitle_burned_title,
                    size_bytes=output_size_bytes,
                    duration_seconds=output_verdict.duration_seconds,
                    width=output_verdict.width,
                    height=output_verdict.height,
                    fps=output_verdict.fps,
                    video_profile=output_verdict.video_profile,
                    video_level=output_verdict.video_level,
                    pix_fmt=output_verdict.pix_fmt,
                    audio_codec=output_verdict.audio_codec,
                    audio_sample_rate=output_verdict.audio_sample_rate,
                    audio_channel_layout=output_verdict.audio_channel_layout,
                    audio_language=(
                        output_audio_track.language if output_audio_track else None
                    ),
                    audio_title=output_audio_track.title if output_audio_track else None,
                    audio_reencoded=verdict.audio_needs_reencode,
                    poster_key=poster_key,
                    realtime_factor=realtime_factor,
                )
            except ReportRejected:
                # The job moved on to a different attempt. This converter
                # has no delete beyond its own keys (D-115): only abort a
                # dangling multipart upload of its own -- the service
                # removes the completed object itself, on this same
                # refusal, since only it holds the credential wide enough
                # to do that.
                await _abort_upload_quietly(s3_client, settings, object_key, job_id)
                logger.info(
                    "job %s: the done report was refused (superseded) -- our own "
                    "dangling upload was aborted; the service removes the completed "
                    "object",
                    job_id,
                )

    except _JobCancelled:
        await _stop_heartbeat_loop()
        await _abort_upload_quietly(s3_client, settings, object_key, job_id)
        if cancel_state["job_lost"]:
            # A converter that has been told the job is not its own must
            # not then post an outcome for it -- no done, no failed, no
            # cancelled.
            logger.info(
                "job %s: the service said this attempt no longer owns the job -- "
                "stopped, nothing reported",
                job_id,
            )
        elif shutdown_event.is_set():
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
        await _stop_heartbeat_loop()
        # Unconditional, whether or not an upload was ever attempted --
        # abort_multipart on a key with nothing dangling is a safe no-op.
        await _abort_upload_quietly(s3_client, settings, object_key, job_id)
        if cancel_state["job_lost"]:
            logger.info(
                "job %s: the service said this attempt no longer owns the job -- "
                "the failure was not reported",
                job_id,
            )
        else:
            reason_key = _reason_key_for(exc)
            try:
                await client.failed(job_id, attempt=attempt, reason_key=reason_key)
            except ReportRejected:
                logger.info("job %s: failure report refused (already superseded)", job_id)
            except Exception:
                logger.exception(
                    "job %s: could not report the failure (reason=%s)", job_id, reason_key
                )
    finally:
        # A backstop only -- every path above already stops the loop
        # before its own report, this just guards against a path that
        # never reached one. Cancelling an already-stopped task is a
        # no-op.
        await _stop_heartbeat_loop()
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
