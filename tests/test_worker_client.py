"""The worker's own HTTP client (`worker.client.WorkerClient`) against a
mocked transport -- no network.

RED state: the `worker` package does not exist at all yet -- every
subject below is imported inside its own test body (mirrors
`tests/test_playlist_build.py`), so the module still collects cleanly and
each test fails as its own `ModuleNotFoundError` rather than one
collection error hiding the whole file.
"""

import json
import uuid

import httpx
import pytest

WORKER_NAME = "ml-server"
STUB_JOB_ID = uuid.uuid4()
STUB_TOKEN = "test-worker-token-do-not-use-in-prod"


def _client(handler):
    """A `WorkerClient` built over a mocked transport -- imported inside
    the body since `worker.client` does not exist yet.
    """
    from worker.client import WorkerClient

    transport = httpx.MockTransport(handler)
    return WorkerClient(
        base_url="https://admin.mytube.example",
        token=STUB_TOKEN,
        worker_name=WORKER_NAME,
        transport=transport,
    )


def _valid_done_kwargs(**overrides) -> dict:
    """Every keyword `WorkerClient.done()` requires, filled with a
    plausible value -- the baseline every call site below sends, mirroring
    `test_worker_api.py`'s own `_valid_done_body()` for the same widened
    shape, one field name apart (no `protocol`/`worker`, which `_body()`
    adds itself).
    """
    kwargs = {
        "attempt": 1,
        "object_key": "x.mp4",
        "source_codec": "hevc",
        "source_profile": "Main 10",
        "subtitle_burned_language": "eng",
        "subtitle_burned_title": "English",
        "size_bytes": 123_456_789,
        "duration_seconds": 1423.5,
        "width": 1920,
        "height": 1080,
        "fps": 23.976,
        "video_profile": "High",
        "video_level": 41,
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "audio_channel_layout": "stereo",
        "audio_language": "eng",
        "audio_title": "English",
        "audio_reencoded": False,
        "poster_key": "edition-1.jpg",
        "realtime_factor": 1.4,
    }
    kwargs.update(overrides)
    return kwargs


def _method_calls():
    return {
        "claim": lambda c: c.claim(),
        "heartbeat": lambda c: c.heartbeat(
            STUB_JOB_ID,
            attempt=1,
            stage="working",
            progress_pct=10.0,
            eta_seconds=60,
            realtime_factor=1.0,
        ),
        "done": lambda c: c.done(STUB_JOB_ID, **_valid_done_kwargs()),
        "failed": lambda c: c.failed(
            STUB_JOB_ID, attempt=1, reason_key="failed.reason.upload"
        ),
        "cancelled": lambda c: c.cancelled(STUB_JOB_ID, attempt=1),
    }


METHOD_CALLS = _method_calls()


@pytest.mark.parametrize("method_name", sorted(METHOD_CALLS))
async def test_every_call_carries_the_bearer_token_and_the_protocol_version(
    method_name,
):
    from worker.client import WORKER_PROTOCOL_VERSION

    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"job_id": None})

    client = _client(handler)
    try:
        await METHOD_CALLS[method_name](client)
    except Exception:
        pass

    request = captured.get("request")
    assert request is not None
    assert request.headers.get("authorization") == f"Bearer {STUB_TOKEN}"
    if request.content:
        body = json.loads(request.content)
        assert body.get("protocol") == WORKER_PROTOCOL_VERSION


@pytest.mark.parametrize("method_name", sorted(METHOD_CALLS))
async def test_every_call_carries_the_worker_s_own_name(method_name):
    """So a job can say who took it."""
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"job_id": None})

    client = _client(handler)
    try:
        await METHOD_CALLS[method_name](client)
    except Exception:
        pass

    request = captured["request"]
    body = json.loads(request.content)
    assert body.get("worker") == WORKER_NAME


async def test_an_empty_queue_is_returned_as_nothing_rather_than_an_error():
    async def handler(request):
        return httpx.Response(204)

    client = _client(handler)
    result = await client.claim()
    assert result is None


async def test_an_unreachable_orchestrator_is_retried_within_a_bounded_window(
    monkeypatch,
):
    from worker import client as client_module

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    attempts = {"n": 0}

    async def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(204)

    client = _client(handler)
    result = await client.claim()

    assert result is None
    assert attempts["n"] > 1
    assert sum(sleeps) <= client_module.REPORT_RETRY_WINDOW_SECONDS


async def test_the_retry_gives_up_at_the_window_and_raises(monkeypatch):
    from worker import client as client_module

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    async def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(Exception):
        await client.claim()

    assert sum(sleeps) <= (
        client_module.REPORT_RETRY_WINDOW_SECONDS
        + client_module.RETRY_BACKOFF_CAP_SECONDS
    )


async def test_a_refused_report_is_raised_as_a_rejection_the_caller_can_act_on():
    """This is the signal to delete the object that was just uploaded, and
    retrying it would only produce a second refusal while the orphan stays
    in the bucket.
    """
    from worker.client import ReportRejected

    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(409, json={"reason": "superseded"})

    client = _client(handler)
    with pytest.raises(ReportRejected):
        await client.done(STUB_JOB_ID, **_valid_done_kwargs())

    assert calls["n"] == 1


async def test_a_not_found_answer_is_raised_as_a_lost_job_never_retried():
    """The heartbeat route answers not-found once this attempt no longer
    owns the job -- the converter's only correct response is to stop, not
    to keep asking the same question.
    """
    from worker.client import JobLost

    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    client = _client(handler)
    with pytest.raises(JobLost):
        await client.heartbeat(STUB_JOB_ID, attempt=1, stage="working")

    assert calls["n"] == 1


async def test_a_refusal_of_the_token_is_not_retried_forever():
    """A wrong credential is not a transient condition."""
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(401)

    client = _client(handler)
    with pytest.raises(Exception):
        await client.claim()

    assert calls["n"] <= 2


async def test_a_protocol_mismatch_answer_is_raised_naming_both_versions():
    """The service's own machine-readable refusal (`_protocol_mismatch`
    in `routes_worker.py`) must become a named exception here, not a
    generic status error the check-in loop logs a traceback for.
    """
    from worker.client import ProtocolMismatch

    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(
            400,
            json={
                "error": "protocol_mismatch",
                "expected_protocol": 99,
                "got_protocol": 2,
            },
        )

    client = _client(handler)
    with pytest.raises(ProtocolMismatch) as excinfo:
        await client.claim()

    assert excinfo.value.service_protocol == 99
    assert calls["n"] == 1, "a mismatch never resolves on a second attempt"


async def test_a_plain_bad_request_with_no_mismatch_body_is_not_mistaken_for_one():
    """A 400 answers plenty of requests that have nothing to do with the
    protocol -- only the specific `protocol_mismatch` error key raises
    `ProtocolMismatch`, everything else still falls through to the
    generic status error.
    """
    from worker.client import ProtocolMismatch

    async def handler(request):
        return httpx.Response(400, json={"error": "reason_not_allowed"})

    client = _client(handler)
    with pytest.raises(Exception) as excinfo:
        await client.claim()

    assert not isinstance(excinfo.value, ProtocolMismatch)


async def test_a_server_error_is_retried_within_the_same_bounded_window(monkeypatch):
    """A transient database failure at the moment of reporting (the
    service's own 503 `try_again`) is exactly the condition this retry
    exists for -- it must not cost a finished GPU encode.
    """
    from worker import client as client_module

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    attempts = {"n": 0}

    async def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"error": "try_again"})
        return httpx.Response(204)

    client = _client(handler)
    result = await client.claim()

    assert result is None
    assert attempts["n"] > 1
    assert sum(sleeps) <= client_module.REPORT_RETRY_WINDOW_SECONDS


async def test_a_server_error_retry_gives_up_at_the_same_boundary_a_transport_failure_does(
    monkeypatch,
):
    from worker import client as client_module

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    async def handler(request):
        return httpx.Response(503, json={"error": "try_again"})

    client = _client(handler)
    with pytest.raises(Exception):
        await client.claim()

    assert sum(sleeps) <= (
        client_module.REPORT_RETRY_WINDOW_SECONDS
        + client_module.RETRY_BACKOFF_CAP_SECONDS
    )


async def test_the_backoff_never_exceeds_its_cap(monkeypatch):
    from worker import client as client_module

    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", _fake_sleep)

    async def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    with pytest.raises(Exception):
        await client.claim()

    assert sleeps
    assert all(s <= client_module.RETRY_BACKOFF_CAP_SECONDS for s in sleeps)


def test_the_client_and_the_service_declare_the_same_protocol_version():
    """The two numbers are the same fact written twice -- a future change
    to one that forgets the other must fail here, not four minutes into
    a real encode.
    """
    from orchestrator.web.routes_worker import (
        WORKER_PROTOCOL_VERSION as service_version,
    )
    from worker.client import WORKER_PROTOCOL_VERSION as client_version

    assert client_version == service_version


async def test_every_route_lives_under_the_service_s_worker_prefix():
    """The service mounts its worker-facing routes under one prefix and the
    address the converter is configured with is the service's own root, so
    the client, not the operator, has to add that prefix -- a bare root URL
    otherwise lands on the sign-in redirect and never reaches the route.
    """
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(204)

    client = _client(handler)
    try:
        await client.claim()
    finally:
        await client.aclose()
    assert seen == ["/worker/claim"]
