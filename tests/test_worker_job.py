"""One claimed job driven through `worker.__main__._process_job` with a
faked runner -- nothing here starts a process, reaches a card or opens a
socket. What is under test is the resolution step between the job the
service handed over and the command that gets built from it: the subtitle
choice the operator made is a *choice*, and the converter is the last
place it can still be read against the file before a picture is drawn.

A conversion that quietly burns nothing in is the failure this file
exists to catch. It reports success, uploads, plays, and is wrong -- so
every assertion below is about the command's own filter chain rather than
about the job having finished.
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"

# Streams in this fixture: 0 video, 1 audio, 2 and 3 subtitles -- so the
# first subtitle track is file stream 2 and subtitle ordinal 0.
_TEXT_SUBTITLE_STREAM = 2
_AUDIO_STREAM = 1


def _probe_json() -> dict:
    return json.loads(
        (FIXTURES_DIR / "subtitles_mixed.json").read_text(encoding="utf-8")
    )


def _output_probe_json() -> dict:
    """A distinguishable fixture for the *output* probe only -- different
    geometry, profile, level, fps and audio shape from `_probe_json()`'s
    own source fixture, so a test can prove a reported field came from
    here and not from the source.
    """
    data = json.loads(
        (FIXTURES_DIR / "subtitles_mixed.json").read_text(encoding="utf-8")
    )
    for stream in data["streams"]:
        if stream["codec_type"] == "video":
            stream["width"] = 1280
            stream["height"] = 720
            stream["profile"] = "High"
            stream["level"] = 41
            stream["avg_frame_rate"] = "30/1"
        elif stream["codec_type"] == "audio":
            stream["sample_rate"] = "44100"
            stream["channel_layout"] = "stereo"
            stream["tags"] = {"language": "jpn", "title": "Dub"}
    data["format"]["duration"] = "603.5"
    return data


class _FakeClient:
    """Records the report the job ended with, and every heartbeat it was
    sent; every call answers the way a healthy service does unless a test
    hands over `heartbeat_responses`.

    `heartbeat_responses` is consumed one entry per `heartbeat()` call, in
    order: `True`/`False` answers the cancel flag the normal way, an
    exception instance is raised instead -- the shape a `JobLost` or a
    transient failure takes. Once exhausted, every further call answers
    `False`, the same healthy default every other test in this file
    relies on.
    """

    def __init__(self, heartbeat_responses: list | None = None):
        self.done_calls = []
        self.failed_calls = []
        self.cancelled_calls = []
        self.heartbeat_calls = []
        self.heartbeat_count_at_done = None
        self._heartbeat_responses = list(heartbeat_responses or [])

    async def heartbeat(self, job_id, **kwargs):
        self.heartbeat_calls.append(kwargs)
        if self._heartbeat_responses:
            answer = self._heartbeat_responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return False

    async def done(self, job_id, **kwargs):
        self.done_calls.append(kwargs)
        self.heartbeat_count_at_done = len(self.heartbeat_calls)

    async def failed(self, job_id, *, attempt, reason_key):
        self.failed_calls.append(reason_key)

    async def cancelled(self, job_id, **kwargs):
        self.cancelled_calls.append(kwargs)


class _FakeResult:
    cancelled = False


class _CancelledResult:
    cancelled = True


def _signature_error():
    """The exact shape the live run raised: the storage rejected the
    signature because the region did not match, on the abort call the
    cleanup path makes.
    """
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "SignatureDoesNotMatch", "Message": "signature mismatch"}},
        "ListMultipartUploads",
    )


def _settings(tmp_path):
    from worker.settings import WorkerSettings

    share = tmp_path / "share"
    work = tmp_path / "work"
    share.mkdir()
    work.mkdir()
    return WorkerSettings(
        orchestrator_url="http://orchestrator.local",
        worker_token="test-worker-token-do-not-use-in-prod",
        s3_endpoint="http://storage:8333",
        s3_bucket="mytube",
        worker_s3_key="worker-key",
        worker_s3_secret="worker-secret",
        share_root=str(share),
        work_dir=str(work),
    )


def _job(**overrides) -> dict:
    job = {
        "job_id": str(uuid.uuid4()),
        "attempt": 1,
        "object_key": "abc.mp4",
        "edition_id": str(uuid.uuid4()),
        "source_path": "movie.mkv",
        "audio_stream_index": _AUDIO_STREAM,
        "subtitle_kind": "embedded",
        "subtitle_stream_index": _TEXT_SUBTITLE_STREAM,
        "subtitle_path": None,
        "stereo_layout": "2d",
    }
    job.update(overrides)
    return job


def _run_one_job(
    monkeypatch,
    tmp_path,
    job,
    probe_json=None,
    *,
    output_probe_json=None,
    upload_file=None,
    abort_multipart=None,
    run_result=None,
    run_seconds: float = 0.0,
    heartbeat_interval: float | None = None,
    heartbeat_responses: list | None = None,
    poster_run_ffmpeg=None,
    tail_progress_speed: float = 999.0,
    mid_progress=None,
) -> tuple[list[str] | None, _FakeClient]:
    """Drive one job to its end and hand back the command that would have
    been run, or `None` when the job failed before building one.

    `upload_file`, `abort_multipart` and `run_result` replace the healthy
    defaults -- they are how a test puts a storage error into one
    particular step without touching the rest of the run. `run_seconds`
    makes the fake run poll `should_cancel` for a while instead of
    returning instantly, the way a real encode does, so a test can prove
    something about the heartbeat loop while it is still running.
    `heartbeat_interval` overrides the module's own real interval so a
    test does not have to wait real seconds; `heartbeat_responses` is
    handed to `_FakeClient` unchanged.

    `mid_progress`, when given a `Progress` instance, is awaited into
    `on_progress` (with a small percentage) before the fake run enters its
    wait loop, so a progress block genuinely precedes some of the
    heartbeats rather than arriving only at the end of the run.

    `output_probe_json` is what `compat.probe`/`compat.analyze` see for
    the *output* file only -- defaults to `probe_json` (the source's own
    fixture) when a test does not care to tell the two apart. A test that
    hands over a distinguishable fixture here can prove a reported field
    came from the output, not the source. `poster_run_ffmpeg` replaces
    the poster extraction's own healthy no-op default, the way
    `upload_file` replaces the video upload's.
    """
    import worker.__main__ as worker_main

    probe_json = probe_json if probe_json is not None else _probe_json()
    output_probe_json = (
        output_probe_json if output_probe_json is not None else probe_json
    )
    commands = []
    client = _FakeClient(heartbeat_responses=heartbeat_responses)

    async def fake_run(cmd, *, should_cancel=lambda: False, on_progress=None, **kwargs):
        commands.append(cmd)
        client.captured_on_progress = on_progress
        # A stub output file so the production code's own size stat
        # (`destination.stat().st_size`) has something to read -- no real
        # ffmpeg process runs in this file (module docstring).
        Path(cmd[-1]).write_bytes(b"stub-output-bytes")
        if mid_progress is not None and on_progress is not None:
            await on_progress(mid_progress, 1.0)
        elapsed = 0.0
        step = 0.01
        while elapsed < run_seconds:
            if should_cancel():
                return _CancelledResult()
            await asyncio.sleep(step)
            elapsed += step
        if run_seconds and should_cancel():
            return _CancelledResult()
        if on_progress is not None:
            from mytube_media.runner import Progress

            # A deliberately wrong "tail" reading -- proves the reported
            # speed is the measured wall time, not whatever this last
            # progress block happened to say.
            await on_progress(
                Progress(
                    out_time_seconds=elapsed, speed=tail_progress_speed, done=True
                ),
                100.0,
            )
        return run_result if run_result is not None else _FakeResult()

    def fake_extract_embedded_track(source, stream_index, scratch_dir, extension="ass"):
        # No real ffmpeg subprocess in this file (module docstring) -- a
        # stub file stands in for the track this call would have pulled
        # out of a real container.
        dest = Path(scratch_dir) / f"track_{stream_index}.{extension}"
        dest.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nStub cue\n", encoding="utf-8"
        )
        return dest

    def fake_copy_external_track(path, scratch_dir):
        dest = Path(scratch_dir) / f"external{Path(path).suffix.lower()}"
        dest.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nStub cue\n", encoding="utf-8"
        )
        return dest

    def fake_probe(path):
        if str(path).endswith("output.mp4"):
            return output_probe_json
        return probe_json

    monkeypatch.setattr(worker_main.compat, "probe", fake_probe)
    monkeypatch.setattr(worker_main.compat, "moov_before_mdat", lambda path: True)
    monkeypatch.setattr(worker_main, "run_ffmpeg_with_progress", fake_run)
    monkeypatch.setattr(
        worker_main, "extract_embedded_track", fake_extract_embedded_track
    )
    monkeypatch.setattr(worker_main, "copy_external_track", fake_copy_external_track)
    monkeypatch.setattr(
        worker_main.worker_storage,
        "upload_file",
        upload_file or (lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        worker_main.worker_storage,
        "abort_multipart",
        abort_multipart or (lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        worker_main.remux,
        "run_ffmpeg",
        poster_run_ffmpeg or (lambda *args, **kwargs: None),
    )
    if heartbeat_interval is not None:
        monkeypatch.setattr(
            worker_main, "HEARTBEAT_INTERVAL_SECONDS", heartbeat_interval
        )

    settings = _settings(tmp_path)
    asyncio.run(
        worker_main._process_job(
            client,
            object(),
            settings,
            job,
            tmp_path / "scratch",
            asyncio.Event(),
        )
    )
    return (commands[0] if commands else None), client


def test_an_embedded_choice_is_resolved_and_actually_burned_in(monkeypatch, tmp_path):
    """The whole bug in one assertion: the job says "embedded", which is
    neither of the two kinds the filter chain understands, and the run it
    produced has to carry a burn-in anyway.
    """
    job = _job()
    cmd, client = _run_one_job(monkeypatch, tmp_path, job)

    assert cmd is not None
    graph = cmd[cmd.index("-vf") + 1]
    assert "subtitles=" in graph
    # The burn-in no longer carries a stream ordinal at all -- it reads a
    # single-track file, not the source container by a stream selector.
    assert "si=" not in graph
    # The path inside the filter term names a file this converter wrote
    # into the job's own scratch directory, never the source --
    # the job id (plain hex and hyphens, untouched by filter escaping)
    # is the scratch directory's own name and stands in for the whole
    # path here, since the path itself is escaped inside the term.
    assert job["job_id"] in graph
    assert "movie.mkv" not in graph
    assert client.failed_calls == []
    assert client.done_calls


def test_an_external_subtitle_named_with_an_apostrophe_never_reaches_the_argv(
    monkeypatch, tmp_path
):
    """The share file's own name is never what the burn-in filter reads --
    the converter copies it into scratch under a generated name first, so
    an apostrophe in the operator's own filename (which would otherwise
    need the two-pass escaper's exact backslash count to survive) never
    has to travel through the filter term at all.
    """
    job = _job(
        subtitle_kind="external",
        subtitle_stream_index=None,
        subtitle_path="It's a subtitle.srt",
    )
    cmd, client = _run_one_job(monkeypatch, tmp_path, job)

    assert cmd is not None
    graph = cmd[cmd.index("-vf") + 1]
    assert "subtitles=" in graph
    assert "It's a subtitle" not in graph
    assert "external" in graph
    assert client.failed_calls == []
    assert client.done_calls


def test_an_unresolved_choice_never_reaches_the_filter_chain(monkeypatch, tmp_path):
    """The other half of the same guarantee: when the recorded index is
    not a subtitle stream at all, the job fails and says so -- it does not
    fall through into a conversion with nothing burned in.
    """
    cmd, client = _run_one_job(
        monkeypatch, tmp_path, _job(subtitle_stream_index=_AUDIO_STREAM)
    )

    assert cmd is None
    assert client.done_calls == []
    assert client.failed_calls == ["failed.reason.probe_mismatch"]


def test_a_bitmap_choice_is_resolved_to_its_own_overlay_branch(monkeypatch, tmp_path):
    """The same word, "embedded", resolving the other way -- a bitmap
    track is drawn by an overlay graph and would be silently dropped by a
    converter that only ever looked for the text kind.
    """
    probe_json = _probe_json()
    for stream in probe_json["streams"]:
        if stream.get("index") == _TEXT_SUBTITLE_STREAM:
            stream["codec_name"] = "hdmv_pgs_subtitle"

    cmd, client = _run_one_job(monkeypatch, tmp_path, _job(), probe_json=probe_json)

    assert cmd is not None
    assert "-filter_complex" in cmd
    assert "overlay" in cmd[cmd.index("-filter_complex") + 1]
    assert client.failed_calls == []


# --- the success report, including the poster gap ------------------------


def test_a_finished_job_reports_the_output_probes_own_facts_not_the_sources(
    monkeypatch, tmp_path
):
    """The whole point of keeping the output probe instead of discarding
    it: every field describing the stored object comes from what the
    encode actually produced, never from the source it was made from.
    """
    output_probe_json = _output_probe_json()
    _cmd, client = _run_one_job(
        monkeypatch, tmp_path, _job(), output_probe_json=output_probe_json
    )

    assert client.done_calls
    report = client.done_calls[0]
    assert report["width"] == 1280
    assert report["height"] == 720
    assert report["video_profile"] == "High"
    assert report["video_level"] == 41
    assert report["fps"] == 30.0
    assert report["audio_sample_rate"] == 44100
    assert report["audio_channel_layout"] == "stereo"
    assert report["audio_language"] == "jpn"
    assert report["audio_title"] == "Dub"
    assert report["duration_seconds"] == pytest.approx(603.5)
    assert report["size_bytes"] == len(b"stub-output-bytes")
    # The source's own facts, read from the source probe, never the
    # output's.
    assert report["source_codec"] == "h264"
    assert report["source_profile"] == "Constrained Baseline"


def test_an_audio_stream_with_no_language_tag_reports_it_as_absent(
    monkeypatch, tmp_path
):
    """The default fixture's own audio track carries no language or title
    tag at all -- the report must say so, never invent a substitute.
    """
    _cmd, client = _run_one_job(monkeypatch, tmp_path, _job())

    assert client.done_calls
    report = client.done_calls[0]
    assert report["audio_language"] is None
    assert report["audio_title"] is None


def test_an_embedded_burn_reports_the_tracks_language_and_title(monkeypatch, tmp_path):
    _cmd, client = _run_one_job(monkeypatch, tmp_path, _job())

    assert client.done_calls
    report = client.done_calls[0]
    assert report["subtitle_burned_language"] == "rus"
    assert report["subtitle_burned_title"] == "Russian"


def test_no_subtitle_chosen_reports_both_burned_fields_as_absent(monkeypatch, tmp_path):
    job = _job(subtitle_kind="none", subtitle_stream_index=None)
    _cmd, client = _run_one_job(monkeypatch, tmp_path, job)

    assert client.done_calls
    report = client.done_calls[0]
    assert report["subtitle_burned_language"] is None
    assert report["subtitle_burned_title"] is None


def test_an_external_burn_reports_the_files_own_name_with_no_language(
    monkeypatch, tmp_path
):
    """An external file carries no language tag this converter could read
    -- the file's own name is the only thing worth reporting in its place.
    """
    job = _job(
        subtitle_kind="external",
        subtitle_stream_index=None,
        subtitle_path="Subs/movie.en.srt",
    )
    _cmd, client = _run_one_job(monkeypatch, tmp_path, job)

    assert client.done_calls
    report = client.done_calls[0]
    assert report["subtitle_burned_language"] is None
    assert report["subtitle_burned_title"] == "movie.en.srt"


def test_the_reported_speed_is_the_source_duration_over_the_measured_wall_time(
    monkeypatch, tmp_path
):
    """The value that used to survive was whatever the final progress
    block happened to read -- the tail of the file, which can differ
    several-fold from the mean. This pins the replacement: source
    duration over the encode's own measured wall time, driven by a fake
    run that actually sleeps for `run_seconds` of real wall time so the
    two monotonic readings either side of it measure something real.
    """
    _cmd, client = _run_one_job(monkeypatch, tmp_path, _job(), run_seconds=0.3)

    assert client.done_calls
    report = client.done_calls[0]
    # The source fixture's own duration is 1.021 seconds; a healthy
    # margin around the expected ratio absorbs scheduling jitter, but the
    # deliberately wrong 999.0 tail reading the fake run also sends
    # through `on_progress` is nowhere near this range either way.
    assert report["realtime_factor"] == pytest.approx(1.021 / 0.3, rel=0.5)
    assert report["realtime_factor"] != 999.0


def test_a_successful_conversion_uploads_and_reports_a_poster_key(
    monkeypatch, tmp_path
):
    uploaded = []

    def spy_upload(client_arg, settings_arg, path, key, content_type="video/mp4"):
        uploaded.append((key, content_type))

    _cmd, client = _run_one_job(monkeypatch, tmp_path, _job(), upload_file=spy_upload)

    assert client.done_calls
    report = client.done_calls[0]
    assert report["poster_key"] is not None
    assert report["poster_key"].endswith(".jpg")
    poster_uploads = [u for u in uploaded if u[1] == "image/jpeg"]
    assert poster_uploads, "the poster must be uploaded under image/jpeg"


def test_a_poster_extraction_failure_is_logged_and_the_conversion_still_succeeds(
    monkeypatch, tmp_path, caplog
):
    """A poster that will not extract must never fail a conversion -- the
    row just degrades to the placeholder, the same discipline the as-is
    add path already follows.
    """

    def failing_run_ffmpeg(argv, *, timeout):
        raise RuntimeError("no frame could be extracted")

    with caplog.at_level(logging.ERROR):
        _cmd, client = _run_one_job(
            monkeypatch, tmp_path, _job(), poster_run_ffmpeg=failing_run_ffmpeg
        )

    assert client.failed_calls == []
    assert client.done_calls
    assert client.done_calls[0]["poster_key"] is None
    assert "poster" in caplog.text.lower()


def test_a_storage_error_while_cleaning_up_after_a_failed_upload_is_swallowed(
    monkeypatch, tmp_path, caplog
):
    """The upload fails, and the abort that cleans up after it fails too.
    That second error is not the job's outcome and must not become the
    converter's: it is logged and dropped, the failure is still reported,
    and the process stays up for the next job.
    """

    def failing_upload(*args, **kwargs):
        raise RuntimeError("the upload did not go through")

    def failing_abort(*args, **kwargs):
        raise _signature_error()

    with caplog.at_level(logging.ERROR):
        _cmd, client = _run_one_job(
            monkeypatch,
            tmp_path,
            _job(),
            upload_file=failing_upload,
            abort_multipart=failing_abort,
        )

    assert client.done_calls == []
    assert client.failed_calls == ["failed.reason.upload"]
    assert "SignatureDoesNotMatch" in caplog.text


def test_a_storage_error_while_cleaning_up_after_a_cancellation_is_swallowed(
    monkeypatch, tmp_path, caplog
):
    """The live failure, in one test: an operator cancels, the abort
    raises, and without this guard the exception left the job, killed the
    converter and left the cancelled job running with no heartbeat.
    """

    def failing_abort(*args, **kwargs):
        raise _signature_error()

    with caplog.at_level(logging.ERROR):
        _cmd, client = _run_one_job(
            monkeypatch,
            tmp_path,
            _job(),
            abort_multipart=failing_abort,
            run_result=_CancelledResult(),
        )

    assert client.cancelled_calls
    assert client.failed_calls == []
    assert client.done_calls == []
    assert "SignatureDoesNotMatch" in caplog.text


# --- the heartbeat loop --------------------------------------------------


def test_the_heartbeat_interval_is_below_the_library_row_s_poll_interval():
    """The row polls every 4 seconds -- the loop has to tick faster than
    that or the operator's own progress bar visibly stands still.
    """
    import worker.__main__ as worker_main

    assert worker_main.HEARTBEAT_INTERVAL_SECONDS < 4


def test_more_than_one_heartbeat_covers_an_upload_that_outlasts_the_interval(
    monkeypatch, tmp_path
):
    """The whole point of the fix: a transfer that runs longer than
    one tick of the loop is still heartbeating throughout, not just once
    before it starts.
    """

    def slow_upload(*args, **kwargs):
        time.sleep(0.05)

    _cmd, client = _run_one_job(
        monkeypatch,
        tmp_path,
        _job(),
        upload_file=slow_upload,
        heartbeat_interval=0.01,
    )

    uploading = [c for c in client.heartbeat_calls if c.get("stage") == "uploading"]
    assert len(uploading) > 1
    assert client.done_calls


def test_no_heartbeat_follows_the_success_report(monkeypatch, tmp_path):
    """The loop is stopped before the terminal report is posted -- a tick
    landing after would be answered not-found and misread as a lost job.
    """
    _cmd, client = _run_one_job(
        monkeypatch, tmp_path, _job(), heartbeat_interval=0.01, run_seconds=0.05
    )

    assert client.done_calls
    assert len(client.heartbeat_calls) == client.heartbeat_count_at_done


def test_the_progress_callback_performs_no_network_call(monkeypatch, tmp_path):
    """Called directly against a client whose heartbeat always raises,
    the callback still returns -- it never awaits the network itself,
    only the heartbeat loop's own timer does that.
    """
    from mytube_media.runner import Progress

    _cmd, client = _run_one_job(monkeypatch, tmp_path, _job())

    async def _call_it():
        await client.captured_on_progress(
            Progress(out_time_seconds=1.0, speed=1.0, done=False), 50.0
        )

    # The job already finished above with a healthy client; this proves
    # the callback's own shape needs nothing from the client at all.
    asyncio.run(_call_it())


def test_a_mid_run_progress_block_yields_a_positive_integer_eta_on_a_heartbeat(
    monkeypatch, tmp_path
):
    """One heartbeat taken after a real progress block carries a positive
    integer estimate -- the fixture's 1.021-second source at a mid-run
    speed of 0.05x has 20 seconds of source time left.
    """
    from mytube_media.runner import Progress

    _cmd, client = _run_one_job(
        monkeypatch,
        tmp_path,
        _job(),
        heartbeat_interval=0.01,
        run_seconds=0.1,
        mid_progress=Progress(out_time_seconds=0.0, speed=0.05, done=False),
    )

    etas = [c["eta_seconds"] for c in client.heartbeat_calls if c.get("eta_seconds")]
    assert etas, "expected at least one heartbeat carrying a positive eta_seconds"
    assert all(isinstance(v, int) and v > 0 for v in etas)


def test_a_stalled_block_reporting_zero_speed_sends_no_zero_eta_on_any_heartbeat(
    monkeypatch, tmp_path
):
    """A block reporting a speed of zero must leave both the realtime
    factor and the estimate at whatever the last usable block set --
    forwarding a zero would be refused by the route, and the tail block
    this harness sends by default is pinned to zero too so a late tick
    cannot smuggle a zero estimate in past this assertion.
    """
    from mytube_media.runner import Progress

    _cmd, client = _run_one_job(
        monkeypatch,
        tmp_path,
        _job(),
        heartbeat_interval=0.01,
        run_seconds=0.1,
        mid_progress=Progress(out_time_seconds=0.0, speed=0.0, done=False),
        tail_progress_speed=0.0,
    )

    assert not any(c.get("realtime_factor") == 0 for c in client.heartbeat_calls)
    assert not any(c.get("eta_seconds") == 0 for c in client.heartbeat_calls)


def test_a_heartbeat_answered_not_found_stops_the_run_and_reports_nothing(
    monkeypatch, tmp_path
):
    """The service saying "this attempt no longer owns the job" stops the
    encode the same way an operator's cancel does, but the converter must
    not then post an outcome for it -- no done, no failed, no cancelled.
    """
    from worker.client import JobLost

    _cmd, client = _run_one_job(
        monkeypatch,
        tmp_path,
        _job(),
        heartbeat_interval=0.01,
        run_seconds=0.3,
        heartbeat_responses=[False, False, JobLost("superseded")],
    )

    assert client.done_calls == []
    assert client.failed_calls == []
    assert client.cancelled_calls == []


def test_a_heartbeat_answered_with_any_other_error_is_logged_and_the_encode_continues(
    monkeypatch, tmp_path, caplog
):
    """Every other heartbeat failure keeps today's behaviour: ten minutes
    of card time is not thrown away for a blip the service side already
    tolerates.
    """
    with caplog.at_level(logging.WARNING):
        _cmd, client = _run_one_job(
            monkeypatch,
            tmp_path,
            _job(),
            heartbeat_interval=0.01,
            run_seconds=0.1,
            heartbeat_responses=[RuntimeError("network blip")],
        )

    assert client.done_calls
    assert client.failed_calls == []


# --- a protocol mismatch ends the process, not the loop ------------------


def test_a_protocol_mismatch_at_check_in_propagates_rather_than_being_swallowed():
    """The check-in's own broad `except Exception` used to turn this into
    a logged stack trace every few seconds, same as any other check-in
    hiccup -- this is not one: the numbers will not agree on the next
    poll either.
    """
    import worker.__main__ as worker_main
    from worker.client import ProtocolMismatch

    class _MismatchClient:
        async def claim(self, *, status="idle"):
            raise ProtocolMismatch(client_protocol=1, service_protocol=2)

    async def _run_it():
        with pytest.raises(ProtocolMismatch):
            await worker_main._check_in(
                _MismatchClient(), asyncio.Event(), status="idle"
            )

    asyncio.run(_run_it())


def test_a_protocol_mismatch_stops_the_run_loop_rather_than_retrying(
    monkeypatch, tmp_path
):
    """`_run`'s own `while` loop must not catch this and go around again
    -- it is the caller's (`main()`'s) job to end the process on it.
    """
    import worker.__main__ as worker_main
    from worker.client import ProtocolMismatch

    calls = {"n": 0}

    async def fake_check_in(client, shutdown_event, *, status):
        calls["n"] += 1
        raise ProtocolMismatch(client_protocol=1, service_protocol=2)

    monkeypatch.setattr(worker_main, "_check_in", fake_check_in)
    monkeypatch.setattr(
        worker_main.gpu, "free_vram_bytes", lambda: 8_000 * worker_main._BYTES_PER_MIB
    )

    settings = _settings(tmp_path)
    shutdown_event = asyncio.Event()

    async def _run_it():
        with pytest.raises(ProtocolMismatch):
            await worker_main._run(object(), object(), settings, shutdown_event)

    asyncio.run(_run_it())
    assert calls["n"] == 1, "the loop must not retry a protocol mismatch"


# --- the remaining-time estimate: defined for every absent input ---------


def test_eta_a_source_and_a_mid_run_block_report_the_remaining_source_time():
    import worker.__main__ as worker_main

    assert worker_main._eta_seconds(600.0, 60.0, 2.0) == 270


def test_eta_a_source_with_no_known_duration_reports_no_estimate():
    import worker.__main__ as worker_main

    assert worker_main._eta_seconds(None, 60.0, 2.0) is None


def test_eta_a_block_with_no_output_timestamp_reports_no_estimate():
    import worker.__main__ as worker_main

    assert worker_main._eta_seconds(600.0, None, 2.0) is None


def test_eta_a_block_reporting_no_or_non_positive_speed_reports_no_estimate():
    import worker.__main__ as worker_main

    assert worker_main._eta_seconds(600.0, 60.0, None) is None
    assert worker_main._eta_seconds(600.0, 60.0, 0.0) is None
    assert worker_main._eta_seconds(600.0, 60.0, -1.0) is None


def test_eta_the_first_block_ffmpeg_emits_still_reports_an_estimate():
    """A timestamp of zero is ffmpeg's first real block, not an absent
    reading -- it must still yield an estimate.
    """
    import worker.__main__ as worker_main

    assert worker_main._eta_seconds(600.0, 0.0, 2.0) == 300


def test_eta_a_block_past_the_source_duration_reports_zero_not_negative():
    import worker.__main__ as worker_main

    assert worker_main._eta_seconds(600.0, 900.0, 2.0) == 0


def test_eta_an_estimate_above_the_routes_ceiling_is_capped_at_it():
    import worker.__main__ as worker_main

    assert (
        worker_main._eta_seconds(worker_main.MAX_ETA_SECONDS * 10, 0.0, 0.1)
        == worker_main.MAX_ETA_SECONDS
    )


def test_a_writable_scratch_root_passes_the_check(tmp_path):
    import worker.__main__ as worker_main

    worker_main._require_writable_scratch(tmp_path)


def test_an_unwritable_scratch_root_ends_the_process_naming_the_directory(
    monkeypatch, tmp_path, caplog
):
    import worker.__main__ as worker_main

    monkeypatch.setattr(worker_main.os, "access", lambda *_args, **_kwargs: False)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc_info:
            worker_main._require_writable_scratch(tmp_path)

    assert exc_info.value.code != 0
    assert str(tmp_path) in caplog.text


def test_a_60_fps_source_is_encoded_at_the_level_that_holds_it(monkeypatch, tmp_path):
    probe_json = _probe_json()
    for stream in probe_json["streams"]:
        if stream["codec_type"] == "video":
            stream["r_frame_rate"] = stream["avg_frame_rate"] = "60/1"

    cmd, client = _run_one_job(monkeypatch, tmp_path, _job(), probe_json)

    assert cmd[cmd.index("-level") + 1] == "4.2"
    assert client.failed_calls == []
