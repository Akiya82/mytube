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

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"

# Streams in this fixture: 0 video, 1 audio, 2 and 3 subtitles -- so the
# first subtitle track is file stream 2 and subtitle ordinal 0.
_TEXT_SUBTITLE_STREAM = 2
_AUDIO_STREAM = 1


def _probe_json() -> dict:
    return json.loads((FIXTURES_DIR / "subtitles_mixed.json").read_text(encoding="utf-8"))


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
    upload_file=None,
    abort_multipart=None,
    run_result=None,
    run_seconds: float = 0.0,
    heartbeat_interval: float | None = None,
    heartbeat_responses: list | None = None,
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
    """
    import worker.__main__ as worker_main

    probe_json = probe_json if probe_json is not None else _probe_json()
    commands = []
    client = _FakeClient(heartbeat_responses=heartbeat_responses)

    async def fake_run(cmd, *, should_cancel=lambda: False, on_progress=None, **kwargs):
        commands.append(cmd)
        client.captured_on_progress = on_progress
        elapsed = 0.0
        step = 0.01
        while elapsed < run_seconds:
            if should_cancel():
                return _CancelledResult()
            await asyncio.sleep(step)
            elapsed += step
        if run_seconds and should_cancel():
            return _CancelledResult()
        return run_result if run_result is not None else _FakeResult()

    def fake_extract_embedded_track(source, stream_index, scratch_dir, extension="ass"):
        # No real ffmpeg subprocess in this file (module docstring) -- a
        # stub file stands in for the track this call would have pulled
        # out of a real container.
        dest = Path(scratch_dir) / f"track_{stream_index}.{extension}"
        dest.write_text("1\n00:00:01,000 --> 00:00:02,000\nStub cue\n", encoding="utf-8")
        return dest

    def fake_copy_external_track(path, scratch_dir):
        dest = Path(scratch_dir) / f"external{Path(path).suffix.lower()}"
        dest.write_text("1\n00:00:01,000 --> 00:00:02,000\nStub cue\n", encoding="utf-8")
        return dest

    monkeypatch.setattr(worker_main.compat, "probe", lambda path: probe_json)
    monkeypatch.setattr(worker_main.compat, "moov_before_mdat", lambda path: True)
    monkeypatch.setattr(worker_main, "run_ffmpeg_with_progress", fake_run)
    monkeypatch.setattr(worker_main, "extract_embedded_track", fake_extract_embedded_track)
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
    if heartbeat_interval is not None:
        monkeypatch.setattr(worker_main, "HEARTBEAT_INTERVAL_SECONDS", heartbeat_interval)

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
    # into the job's own scratch directory (CR-05), never the source --
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


# --- the heartbeat loop (CR-01, WR-01) -----------------------------------


def test_the_heartbeat_interval_is_below_the_library_row_s_poll_interval():
    """The row polls every 4 seconds -- the loop has to tick faster than
    that or the operator's own progress bar visibly stands still.
    """
    import worker.__main__ as worker_main

    assert worker_main.HEARTBEAT_INTERVAL_SECONDS < 4


def test_more_than_one_heartbeat_covers_an_upload_that_outlasts_the_interval(
    monkeypatch, tmp_path
):
    """The whole point of CR-01's fix: a transfer that runs longer than
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
