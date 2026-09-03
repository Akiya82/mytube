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
    """Records the report the job ended with; every call answers the way
    a healthy service does.
    """

    def __init__(self):
        self.done_calls = []
        self.failed_calls = []
        self.cancelled_calls = []

    async def heartbeat(self, job_id, **kwargs):
        return False

    async def done(self, job_id, **kwargs):
        self.done_calls.append(kwargs)

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
) -> tuple[list[str] | None, _FakeClient]:
    """Drive one job to its end and hand back the command that would have
    been run, or `None` when the job failed before building one.

    `upload_file`, `abort_multipart` and `run_result` replace the healthy
    defaults -- they are how a test puts a storage error into one
    particular step without touching the rest of the run.
    """
    import worker.__main__ as worker_main

    probe_json = probe_json if probe_json is not None else _probe_json()
    commands = []

    async def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return run_result if run_result is not None else _FakeResult()

    monkeypatch.setattr(worker_main.compat, "probe", lambda path: probe_json)
    monkeypatch.setattr(worker_main.compat, "moov_before_mdat", lambda path: True)
    monkeypatch.setattr(worker_main, "run_ffmpeg_with_progress", fake_run)
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

    client = _FakeClient()
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
    cmd, client = _run_one_job(monkeypatch, tmp_path, _job())

    assert cmd is not None
    graph = cmd[cmd.index("-vf") + 1]
    assert "subtitles=" in graph
    # The filter counts subtitle streams, not file streams: stream 2 is
    # the first subtitle in this file.
    assert "si=0" in graph
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
