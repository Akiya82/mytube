"""Route-level assertions for the worker-facing surface -- POST
/worker/claim, /worker/jobs/{id}/heartbeat, .../done, .../failed and
.../cancelled -- with the database mocked and driven through
httpx.ASGITransport, in the style of test_resolve.py.

RED state: `orchestrator.web.routes_worker` does not exist yet, and
`orchestrator.auth` carries no `TOKEN_GATED_PREFIXES` tuple -- the worker
prefix is not reachable at all today: the session gate answers every
request below with a 302 to /login (the existing "an operator exists"
default), never a 401, since /worker/ is absent from every list the gate
consults. That misbehaviour is exactly the contract this module fixes.
Per this plan's own prohibition, no test below imports
`orchestrator.web.routes_worker` at module scope -- everything is driven
through the application instead, and the two constants
(`WORKER_PROTOCOL_VERSION`, `ALLOWED_FAILURE_REASONS`) are imported inside
the one test each that needs them.
"""
import uuid

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.main import app

from conftest import WORKER_TOKEN

STUB_JOB_ID = uuid.uuid4()

# The six new failure-reason keys this phase introduces: four for the
# transcode branch's own new ways to fail, plus a name each for an
# unreadable source and a full scratch disk (Pitfall 4) -- the frozen
# vocabulary `ALLOWED_FAILURE_REASONS` must carry.
NEW_FAILURE_REASON_KEYS = frozenset(
    {
        "failed.reason.crashed_converter",
        "failed.reason.gpu_out_of_memory",
        "failed.reason.probe_mismatch",
        "failed.reason.protocol_mismatch",
        "failed.reason.unreadable_source",
        "failed.reason.disk_full",
    }
)


class _DummyConn:
    pass


class _DummyAcquireCtx:
    async def __aenter__(self):
        return _DummyConn()

    async def __aexit__(self, *exc_info):
        return False


class _DummyPool:
    def acquire(self, *args, **kwargs):
        return _DummyAcquireCtx()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _app_env(settings):
    """Pulls in conftest's `settings` fixture for its side effect (env vars
    get_settings() needs, including MYTUBE_WORKER_TOKEN). get_settings is
    @lru_cache'd, so clear it before and after -- otherwise whichever test
    in the whole session calls it first pins its Settings() for every
    later test/module.
    """
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client():
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _worker_requests():
    """(path, body) for each of the five worker-facing routes, in the
    frozen shape this plan fixes.
    """
    return [
        ("/worker/claim", {"protocol": 1, "worker": "ml-server"}),
        (
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            {
                "protocol": 1,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
                "progress_pct": 12.0,
                "eta_seconds": 90,
                "realtime_factor": 1.1,
            },
        ),
        (
            f"/worker/jobs/{STUB_JOB_ID}/done",
            {"protocol": 1, "worker": "ml-server", "attempt": 1, "object_key": "x.mp4"},
        ),
        (
            f"/worker/jobs/{STUB_JOB_ID}/failed",
            {
                "protocol": 1,
                "worker": "ml-server",
                "attempt": 1,
                "reason_key": "failed.reason.upload",
            },
        ),
        (
            f"/worker/jobs/{STUB_JOB_ID}/cancelled",
            {"protocol": 1, "worker": "ml-server", "attempt": 1},
        ),
    ]


WORKER_REQUESTS = _worker_requests()


# --- The gate (the reason this surface exists at all) --------------------


@pytest.mark.parametrize("path,body", WORKER_REQUESTS)
async def test_every_worker_route_refuses_a_caller_with_no_authorization_header(
    client, path, body
):
    """Five routes, one parametrised test, so a sixth route added later
    without a gate fails a test that already exists rather than passing
    unnoticed.
    """
    async with client:
        resp = await client.post(path, json=body)
    assert resp.status_code == 401, f"{path} answered {resp.status_code}"


async def test_the_route_refuses_a_wrong_token(client):
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": "Bearer wrong-token-value"},
        )
    assert resp.status_code == 401


async def test_the_route_refuses_a_token_in_the_wrong_scheme(client):
    async with client:
        bare = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": WORKER_TOKEN},
        )
        basic = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Basic {WORKER_TOKEN}"},
        )
    assert bare.status_code == 401
    assert basic.status_code == 401


async def test_the_route_refuses_a_non_ascii_token_rather_than_crashing(client):
    """A comparison written over two text values raises on exactly this
    input, and a crash on an unauthenticated request is a worse outcome
    than a refusal.
    """
    # A plain str header value is refused by httpx itself (>=0.28 encodes
    # str header values as strict ASCII, before the request ever leaves
    # the client) -- bytes bypass that client-side encoding and are what
    # actually reach the comparison this test means to exercise.
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": "Bearer tökén-ü".encode("utf-8")},
        )
    assert resp.status_code == 401


async def test_the_route_refuses_a_token_of_the_right_length_but_wrong_content(client):
    """Pins that the check compares content, not shape."""
    wrong_same_length = "x" * len(WORKER_TOKEN)
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {wrong_same_length}"},
        )
    assert resp.status_code == 401


@pytest.mark.parametrize("path,body", WORKER_REQUESTS)
async def test_the_correct_token_reaches_the_route(client, path, body):
    """This is a gate test, not a business-logic test -- it exists so a
    sixth route added later without the credential dependency fails a
    test that already exists rather than passing unnoticed (see the
    module docstring above). It does not assert a specific success status
    for the four job-id routes: against `_DummyPool`'s bare connection
    (no real Postgres, no monkeypatch) `STUB_JOB_ID` is exactly as
    unmatched as any other id, and the correct, specific answer for that
    -- 404 for heartbeat, 409 for done/failed/cancelled -- is pinned by
    `test_an_unknown_job_identifier_answers_a_refusal_rather_than_an_error`
    and `test_a_superseded_report_refuses_with_a_status_the_worker_can_act_on`
    below, which send this exact request and require exactly those codes.
    Asserting (200, 204) here as well would contradict those two tests
    outright for the same input; what this test actually verifies is that
    the token was accepted -- the request was not refused as
    unauthenticated (401) by the dependency this whole module exists to
    add.
    """
    async with client:
        resp = await client.post(
            path, json=body, headers={"Authorization": f"Bearer {WORKER_TOKEN}"}
        )
    assert resp.status_code != 401


async def test_the_worker_prefix_is_gated_by_its_token_and_by_no_other_list():
    """Either half alone is a wrong implementation: the three existing
    tuples exempt a path from the session check with nothing else behind
    it, so putting the worker prefix there would open it to anyone who can
    reach the host. But the gate as it stands today redirects any unlisted
    path carrying no session, so a prefix in none of the four lists never
    reaches its own credential check at all.
    """
    from orchestrator import auth

    assert "/worker/" not in auth.HOST_EXEMPT_PREFIXES
    assert "/worker/" not in auth.PUBLIC_PATHS
    assert "/worker/" not in auth.PUBLIC_PREFIXES
    assert any(prefix.startswith("/worker") for prefix in auth.TOKEN_GATED_PREFIXES)


async def test_a_token_gated_request_is_granted_no_operator_session(client):
    """What makes the fourth tuple different in kind from the three it
    sits beside -- it grants passage, never authorization.
    """
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code in (200, 204)
    assert "set-cookie" not in {name.lower() for name in resp.headers.keys()}


async def test_a_worker_route_refuses_rather_than_redirects_before_any_operator_exists(
    client, no_operator
):
    """A converter brought up against a freshly deployed service must fail
    with something it can log, not with a page.
    """
    async with client:
        resp = await client.post("/worker/claim", json={"protocol": 1, "worker": "ml-server"})
    assert resp.status_code == 401
    assert resp.status_code != 302


async def test_no_response_body_or_header_ever_echoes_the_token(client):
    async with client:
        good = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
        bad = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": "Bearer wrong-token-value"},
        )
    for resp in (good, bad):
        assert WORKER_TOKEN not in resp.text
        for value in resp.headers.values():
            assert WORKER_TOKEN not in value


# --- Protocol and payload --------------------------------------------


async def test_a_claim_from_an_unknown_protocol_version_is_refused(client):
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 999, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert 400 <= resp.status_code < 500
    body = resp.json()
    assert "protocol" in str(body).lower()


async def test_an_empty_queue_answers_no_content(client):
    """A worker polling every few seconds should be able to tell the two
    apart without parsing.
    """
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 204
    assert resp.content == b""


async def test_a_claim_carries_only_the_operator_s_choices(monkeypatch, client):
    """A later plan cannot quietly start shipping a built command over the
    network.

    `_DummyPool`/`_DummyConn` carry no real data (this file drives no
    Postgres at all), so a genuine "job found" claim can only be produced
    here the same way `test_a_claim_records_the_check_in_even_when_it_
    finds_nothing` and `test_a_heartbeat_returns_the_cancel_flag` already
    produce their own specific outcomes above: by monkeypatching the
    queries this route calls, rather than depending on a row this fixture
    has no way to seed.
    """
    from orchestrator.db import queries

    fake_row = {
        "id": STUB_JOB_ID,
        "attempts": 1,
        "kind": "transcode",
        "title_id": uuid.uuid4(),
        "edition_id": uuid.uuid4(),
        "source_path": "Movies/example.mkv",
        "audio_stream_index": 1,
        "subtitle_kind": "embedded",
        "subtitle_stream_index": 2,
        "subtitle_path": None,
        "stereo_layout": None,
    }

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        pass

    async def _fake_claim_job(conn, *, kind, worker_name):
        return fake_row

    async def _fake_edition_object_key(conn, edition_id):
        return "x.mp4"

    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)
    monkeypatch.setattr(queries, "claim_job", _fake_claim_job, raising=False)
    monkeypatch.setattr(queries, "edition_object_key", _fake_edition_object_key, raising=False)

    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 200
    payload = resp.json()
    expected_keys = {
        "job_id",
        "attempt",
        "kind",
        "title_id",
        "edition_id",
        "object_key",
        "source_path",
        "audio_stream_index",
        "subtitle_kind",
        "subtitle_stream_index",
        "subtitle_path",
        "stereo_layout",
    }
    assert set(payload.keys()) == expected_keys


async def test_a_claim_records_the_check_in_even_when_it_finds_nothing(monkeypatch, client):
    """This is the data the waiting-for-a-converter state reads, and it
    must not depend on there being work.
    """
    from orchestrator.db import queries

    seen = []

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        seen.append((worker_name, status))

    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)

    async with client:
        await client.post(
            "/worker/claim",
            json={"protocol": 1, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert seen, "record_worker_seen was never called on an empty queue"


# --- Reports ------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
async def test_a_superseded_report_refuses_with_a_status_the_worker_can_act_on(client, outcome):
    """The signal to delete what it uploaded."""
    body = {"protocol": 1, "worker": "ml-server", "attempt": 1}
    if outcome == "done":
        body["object_key"] = "x.mp4"
    if outcome == "failed":
        body["reason_key"] = "failed.reason.upload"

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/{outcome}",
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 409
    payload = resp.json()
    assert payload


async def test_the_route_refuses_a_failure_reason_outside_the_allowed_set(client):
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/failed",
            json={
                "protocol": 1,
                "worker": "ml-server",
                "attempt": 1,
                "reason_key": "not a real reason at all",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert 400 <= resp.status_code < 500

    from orchestrator.web import routes_worker

    assert routes_worker.ALLOWED_FAILURE_REASONS
    assert NEW_FAILURE_REASON_KEYS <= routes_worker.ALLOWED_FAILURE_REASONS


@pytest.mark.parametrize("cancel_requested", [True, False])
async def test_a_heartbeat_returns_the_cancel_flag(monkeypatch, client, cancel_requested):
    from orchestrator.db import queries

    async def _fake_heartbeat_job(conn, job_id, **kwargs):
        return {"cancel_requested": cancel_requested}

    monkeypatch.setattr(queries, "heartbeat_job", _fake_heartbeat_job, raising=False)

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": 1,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
                "progress_pct": 10.0,
                "eta_seconds": 60,
                "realtime_factor": 1.0,
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 200
    assert resp.json()["cancel_requested"] == cancel_requested


async def test_an_unknown_job_identifier_answers_a_refusal_rather_than_an_error(client):
    async with client:
        resp = await client.post(
            f"/worker/jobs/{uuid.uuid4()}/heartbeat",
            json={
                "protocol": 1,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 404
    assert "traceback" not in resp.text.lower()
