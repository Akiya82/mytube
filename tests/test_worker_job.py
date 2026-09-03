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

    async def heartbeat(self, job_id, **kwargs):
        return False

    async def done(self, job_id, **kwargs):
        self.done_calls.append(kwargs)

    async def failed(self, job_id, *, attempt, reason_key):
        self.failed_calls.append(reason_key)

    async def cancelled(self, job_id, **kwargs):
        pass


class _FakeResult:
    cancelled = False


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
    monkeypatch, tmp_path, job, probe_json=None
) -> tuple[list[str] | None, _FakeClient]:
    """Drive one job to its end and hand back the command that would have
    been run, or `None` when the job failed before building one.
    """
    import worker.__main__ as worker_main

    probe_json = probe_json if probe_json is not None else _probe_json()
    commands = []

    async def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return _FakeResult()

    monkeypatch.setattr(worker_main.compat, "probe", lambda path: probe_json)
    monkeypatch.setattr(worker_main.compat, "moov_before_mdat", lambda path: True)
    monkeypatch.setattr(worker_main, "run_ffmpeg_with_progress", fake_run)
    monkeypatch.setattr(
        worker_main.worker_storage, "upload_file", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        worker_main.worker_storage, "abort_multipart", lambda *args, **kwargs: None
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
