"""Route-level assertions for the worker-facing surface -- POST
/worker/claim, /worker/jobs/{id}/heartbeat, .../done, .../failed and
.../cancelled -- with the database mocked and driven through
httpx.ASGITransport, in the style of test_resolve.py.

Every request body below carries `WORKER_PROTOCOL_VERSION`, imported at
module scope (rather than hand-written as a literal) so a future rise in
that constant is a one-line change here too, not a sweep of every body
in this file. `ALLOWED_FAILURE_REASONS` is still imported inline, in the
one test that needs it -- everything else is driven through the
application, never a second, independent route import.
"""
import asyncio
import json
import uuid

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.main import app
from orchestrator.web.routes_worker import WORKER_PROTOCOL_VERSION

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


class _DummyTransactionCtx:
    """A no-op stand-in for asyncpg's own transaction context manager --
    enters and exits without touching `conn`, and never swallows whatever
    the `async with` block raises (the real one does not either).
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _DummyConn:
    def transaction(self):
        return _DummyTransactionCtx()

    async def execute(self, *args, **kwargs):
        """`record_worker_seen` issues `conn.execute` for its upsert --
        without this, every route that now reaches the check-in call
        raises `AttributeError` inside the route's own broad exception
        handler, turning heartbeats into false not-founds and terminal
        reports into false retryable errors. Returns a plain command
        status string, the shape asyncpg's own `execute` returns.
        """
        return "INSERT 0 1"


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


def _valid_done_body(**overrides) -> dict:
    """A `DoneBody` with every field this plan added filled in with a
    plausible value -- the baseline every route test below posts, then
    mutates one field at a time to prove the field's own bound or its own
    required-but-nullable shape. `source_profile`, `subtitle_burned_*`,
    `video_level`, `audio_language`, `audio_title` and `poster_key` are
    exactly the seven fields `DoneBody`'s own docstring declares as
    accepting absence -- carried here as real values so a test that wants
    the absent case sets one to `None` explicitly rather than omitting it.
    """
    body = {
        "protocol": WORKER_PROTOCOL_VERSION,
        "worker": "ml-server",
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
    body.update(overrides)
    return body


def _worker_requests():
    """(path, body) for each of the five worker-facing routes, in the
    frozen shape this plan fixes.
    """
    return [
        ("/worker/claim", {"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"}),
        (
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            {
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
                "progress_pct": 12.0,
                "eta_seconds": 90,
                "realtime_factor": 1.1,
            },
        ),
        (f"/worker/jobs/{STUB_JOB_ID}/done", _valid_done_body()),
        (
            f"/worker/jobs/{STUB_JOB_ID}/failed",
            {
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "reason_key": "failed.reason.upload",
            },
        ),
        (
            f"/worker/jobs/{STUB_JOB_ID}/cancelled",
            {"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server", "attempt": 1},
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
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
            headers={"Authorization": "Bearer wrong-token-value"},
        )
    assert resp.status_code == 401


async def test_the_route_refuses_a_token_in_the_wrong_scheme(client):
    async with client:
        bare = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
            headers={"Authorization": WORKER_TOKEN},
        )
        basic = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
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
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
            headers={"Authorization": "Bearer tökén-ü".encode("utf-8")},
        )
    assert resp.status_code == 401


async def test_the_route_refuses_a_token_of_the_right_length_but_wrong_content(client):
    """Pins that the check compares content, not shape."""
    wrong_same_length = "x" * len(WORKER_TOKEN)
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
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
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
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
        resp = await client.post("/worker/claim", json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"})
    assert resp.status_code == 401
    assert resp.status_code != 302


async def test_no_response_body_or_header_ever_echoes_the_token(client):
    async with client:
        good = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
        bad = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
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


async def test_a_protocol_mismatch_answer_names_both_versions(client):
    """The named exception the client raises on this answer needs both
    numbers to say, in one line, which is which -- naming only one would
    leave a converter reading its own log guessing what the service
    actually wants.
    """
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": 999, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    body = resp.json()
    assert body["error"] == "protocol_mismatch"
    assert body["expected_protocol"] == WORKER_PROTOCOL_VERSION
    assert body["got_protocol"] == 999


async def test_a_client_driven_through_a_stubbed_transport_raises_the_named_mismatch(
    client, monkeypatch
):
    """Drives the actual `WorkerClient` against this route (through the
    ASGI transport, no real network) rather than a hand-built response --
    the same shape the converter itself hits at its first claim.
    """
    import worker.client as worker_client_module
    from worker.client import ProtocolMismatch, WorkerClient

    monkeypatch.setattr(worker_client_module, "WORKER_PROTOCOL_VERSION", 999)

    worker_client = WorkerClient(
        base_url="http://testserver",
        token=WORKER_TOKEN,
        worker_name="ml-server",
        transport=httpx.ASGITransport(app=app),
    )
    try:
        with pytest.raises(ProtocolMismatch) as excinfo:
            await worker_client.claim()
    finally:
        await worker_client.aclose()

    assert excinfo.value.service_protocol == WORKER_PROTOCOL_VERSION


async def test_an_empty_queue_answers_no_content(client):
    """A worker polling every few seconds should be able to tell the two
    apart without parsing.
    """
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
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
        "external_audio_path": None,
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
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
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
        "external_audio_path",
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
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert seen, "record_worker_seen was never called on an empty queue"


# --- Reports ------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
async def test_a_superseded_report_refuses_with_a_status_the_worker_can_act_on(
    monkeypatch, client, outcome
):
    """The signal to delete what it uploaded -- driven here as a clean
    superseded write (the terminal write's own honest `False`, no
    exception raised), since task 3 gives a raised exception a different
    answer entirely (503, see the dedicated tests below). `job_by_id`
    reports a `running`, not-yet-done job, so this is the genuine-conflict
    case: deletion is attempted for `done` (the only outcome whose
    `_reject_superseded` call carries `delete_key=True`).
    """
    from orchestrator.db import queries
    from orchestrator.web import routes_worker

    fake_job_row = {
        "id": STUB_JOB_ID,
        "title_id": None,
        "edition_id": uuid.uuid4(),
        "stereo_layout": None,
        "status": "running",
    }

    async def fake_job_by_id(conn, job_id):
        return fake_job_row

    async def fake_finish_job(conn, job_id, **kwargs):
        return False

    async def fake_edition_object_key(conn, edition_id):
        return "x.mp4"

    deleted = []

    monkeypatch.setattr(queries, "job_by_id", fake_job_by_id, raising=False)
    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)
    monkeypatch.setattr(queries, "edition_object_key", fake_edition_object_key, raising=False)
    monkeypatch.setattr(routes_worker, "delete_object", deleted.append, raising=False)

    if outcome == "done":
        body = _valid_done_body()
    else:
        body = {"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server", "attempt": 1}
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
    if outcome == "done":
        assert deleted == ["x.mp4"], "a conflict on a job that is not done still deletes"
    else:
        assert deleted == []


async def test_a_conflict_on_a_job_that_is_already_done_deletes_nothing(monkeypatch, client):
    """CR-03(b): both attempts of a job write the same object key, so a
    late report against a job a different attempt already finished points
    at the object an active title is serving right now -- the delete must
    be skipped, not just attempted against a key that happens to be gone.
    """
    from orchestrator.db import queries
    from orchestrator.web import routes_worker

    fake_job_row = {
        "id": STUB_JOB_ID,
        "title_id": None,
        "edition_id": uuid.uuid4(),
        "stereo_layout": None,
        "status": "done",
    }

    async def fake_job_by_id(conn, job_id):
        return fake_job_row

    async def fake_finish_job(conn, job_id, **kwargs):
        return False

    edition_object_key_called = []

    async def fake_edition_object_key(conn, edition_id):
        edition_object_key_called.append(edition_id)
        return "x.mp4"

    deleted = []

    monkeypatch.setattr(queries, "job_by_id", fake_job_by_id, raising=False)
    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)
    monkeypatch.setattr(queries, "edition_object_key", fake_edition_object_key, raising=False)
    monkeypatch.setattr(routes_worker, "delete_object", deleted.append, raising=False)

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/done",
            json=_valid_done_body(),
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 409
    assert deleted == []
    assert edition_object_key_called == [], "the key is never even looked up for a done job"


# --- The check-in stops freezing at the moment of the claim (05-17) -----
#
# Each terminal report closes the window between the converter's last
# heartbeat and its next claim poll: a converter that just finished (or
# was cancelled, or gave up) is idle, not still "working" as the last
# heartbeat left it. Refreshed only on the branch where the terminal
# write itself was accepted, inside the same transaction -- a rolled-back
# report must leave no trace of a check-in it never earned.


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
async def test_an_accepted_terminal_report_refreshes_the_check_in_with_the_idle_status(
    monkeypatch, client, outcome
):
    from orchestrator.db import queries

    monkeypatch.setattr(queries, "job_by_id", _fake_job_by_id, raising=False)

    async def fake_finish_job(conn, job_id, **kwargs):
        return True

    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)

    seen = []

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        seen.append((worker_name, status))

    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)

    if outcome == "done":
        body = _valid_done_body()
    else:
        body = {"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server", "attempt": 1}
        if outcome == "failed":
            body["reason_key"] = "failed.reason.upload"

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/{outcome}",
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 204
    assert seen == [("ml-server", "idle")]


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
async def test_a_superseded_terminal_report_refreshes_no_check_in(monkeypatch, client, outcome):
    from orchestrator.db import queries
    from orchestrator.web import routes_worker

    fake_job_row = {
        "id": STUB_JOB_ID,
        "title_id": None,
        "edition_id": uuid.uuid4(),
        "stereo_layout": None,
        "status": "running",
    }

    async def fake_job_by_id(conn, job_id):
        return fake_job_row

    async def fake_finish_job(conn, job_id, **kwargs):
        return False

    async def fake_edition_object_key(conn, edition_id):
        return "x.mp4"

    seen = []

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        seen.append((worker_name, status))

    monkeypatch.setattr(queries, "job_by_id", fake_job_by_id, raising=False)
    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)
    monkeypatch.setattr(queries, "edition_object_key", fake_edition_object_key, raising=False)
    monkeypatch.setattr(routes_worker, "delete_object", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)

    if outcome == "done":
        body = _valid_done_body()
    else:
        body = {"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server", "attempt": 1}
        if outcome == "failed":
            body["reason_key"] = "failed.reason.upload"

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/{outcome}",
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 409
    assert seen == [], "a superseded terminal report must refresh no check-in"


# --- Every terminal report announces the bucket this process never saw --
#
# The converter uploads its object with its own client, in its own pod --
# this process's cached bucket listing has never seen that key. These
# three reports are the first moment this process can know the bucket
# changed, so all three clear orchestrator.storage's own cache on their
# accepted branch. A raised write, by contrast, must leave a still-correct
# cache alone.


def _accepted_job_row(title_id=None):
    """A job row with `title_id=None` by default -- the minimal shape
    that lets `wrote=True` short-circuit every route's own further write
    (set_edition_conversion_detail, mark_edition_failed, etc.), which
    this plan does not touch and does not need mocked to prove the cache
    is cleared on the accepted branch.
    """
    return {
        "id": STUB_JOB_ID,
        "title_id": title_id,
        "edition_id": uuid.uuid4(),
        "stereo_layout": None,
        "status": "running",
        "make_default_when_ready": False,
    }


async def _fake_job_by_id(conn, job_id):
    return _accepted_job_row()


async def test_a_successful_done_report_clears_the_cached_storage_listing(monkeypatch, client):
    from orchestrator.db import queries
    import orchestrator.storage as storage

    monkeypatch.setattr(queries, "job_by_id", _fake_job_by_id, raising=False)

    async def fake_finish_job(conn, job_id, **kwargs):
        return True

    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)
    storage._usage_cache["usage"] = "stub-usage"
    storage._usage_cache["expires_at"] = 9_999_999_999.0

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/done",
            json=_valid_done_body(),
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert resp.status_code == 204
    assert storage._usage_cache["usage"] is None


async def test_a_failed_report_clears_the_cached_storage_listing_too(monkeypatch, client):
    """The poster case: the converter uploads the poster before the
    video (D-115), so a failed conversion can have already left an
    object in the bucket. The failed report is the only announcement of
    it, so it clears the cache exactly like the done report does.
    """
    from orchestrator.db import queries
    import orchestrator.storage as storage

    monkeypatch.setattr(queries, "job_by_id", _fake_job_by_id, raising=False)

    async def fake_finish_job(conn, job_id, **kwargs):
        return True

    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)
    storage._usage_cache["usage"] = "stub-usage"
    storage._usage_cache["expires_at"] = 9_999_999_999.0

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/failed",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "reason_key": "failed.reason.upload",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert resp.status_code == 204
    assert storage._usage_cache["usage"] is None


async def test_a_cancelled_report_clears_the_cached_storage_listing_too(monkeypatch, client):
    from orchestrator.db import queries
    import orchestrator.storage as storage

    monkeypatch.setattr(queries, "job_by_id", _fake_job_by_id, raising=False)

    async def fake_finish_job(conn, job_id, **kwargs):
        return True

    monkeypatch.setattr(queries, "finish_job", fake_finish_job, raising=False)
    storage._usage_cache["usage"] = "stub-usage"
    storage._usage_cache["expires_at"] = 9_999_999_999.0

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/cancelled",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert resp.status_code == 204
    assert storage._usage_cache["usage"] is None


async def test_a_raise_during_a_done_report_leaves_the_cached_storage_listing_untouched(
    monkeypatch, client
):
    """A rolled-back write must not throw away a cache that is still
    correct -- the raise path answers 503 (retryable) and never reaches
    the accepted branch that clears the cache.
    """
    from orchestrator.db import queries
    import orchestrator.storage as storage

    monkeypatch.setattr(queries, "job_by_id", _fake_job_by_id, raising=False)

    async def failing_finish_job(conn, job_id, **kwargs):
        raise RuntimeError("the database hiccuped mid-write")

    monkeypatch.setattr(queries, "finish_job", failing_finish_job, raising=False)
    storage._usage_cache["usage"] = "stub-usage"
    storage._usage_cache["expires_at"] = 9_999_999_999.0

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/done",
            json=_valid_done_body(),
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert resp.status_code == 503
    assert storage._usage_cache["usage"] == "stub-usage"

    storage.invalidate_usage_cache()


async def test_the_route_refuses_a_failure_reason_outside_the_allowed_set(client):
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/failed",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
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
                "protocol": WORKER_PROTOCOL_VERSION,
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


# --- The check-in stops freezing at the moment of the claim (05-17) -----
#
# A converter that holds a job stops polling /worker/claim -- the only
# route that used to write worker_checkins -- so about a minute into
# every conversion the interface read a busy converter as one that had
# vanished. The heartbeat and every accepted terminal report now refresh
# the same row, guarded by the same accepted-write condition their own
# database call already enforces.


async def test_an_accepted_heartbeat_refreshes_the_check_in_with_the_working_status(
    monkeypatch, client
):
    from orchestrator.db import queries

    async def _fake_heartbeat_job(conn, job_id, **kwargs):
        return {"cancel_requested": False}

    seen = []

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        seen.append((worker_name, status))

    monkeypatch.setattr(queries, "heartbeat_job", _fake_heartbeat_job, raising=False)
    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 200
    assert seen == [("ml-server", "working")]


async def test_a_superseded_heartbeat_refreshes_no_check_in(monkeypatch, client):
    """The negative case matters more than the positive one: an attempt
    that lost its job must not be able to refresh a check-in its
    successor now owns.
    """
    from orchestrator.db import queries

    async def _fake_heartbeat_job(conn, job_id, **kwargs):
        return None

    seen = []

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        seen.append((worker_name, status))

    monkeypatch.setattr(queries, "heartbeat_job", _fake_heartbeat_job, raising=False)
    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 404
    assert seen == [], "a superseded heartbeat must refresh no check-in"


async def test_a_heartbeat_whose_write_raises_is_refused_and_refreshes_no_check_in(
    monkeypatch, client
):
    from orchestrator.db import queries

    async def _failing_heartbeat_job(conn, job_id, **kwargs):
        raise RuntimeError("the database hiccuped mid-write")

    seen = []

    async def _fake_record_worker_seen(conn, *, worker_name, status):
        seen.append((worker_name, status))

    monkeypatch.setattr(queries, "heartbeat_job", _failing_heartbeat_job, raising=False)
    monkeypatch.setattr(queries, "record_worker_seen", _fake_record_worker_seen, raising=False)

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 404
    assert seen == [], "a heartbeat whose write raised must refresh no check-in"


async def test_an_unknown_job_identifier_answers_a_refusal_rather_than_an_error(client):
    async with client:
        resp = await client.post(
            f"/worker/jobs/{uuid.uuid4()}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 404
    assert "traceback" not in resp.text.lower()


# --- Bounds and enumerations (WR-05/IN-13/T-04-57) -----------------------


async def test_a_heartbeat_whose_progress_value_is_not_a_number_is_refused(
    monkeypatch, client
):
    """The exact reproduction WR-05 recorded (a JSON string carrying
    `"NaN"`, not a bare float) -- `allow_inf_nan=False` is what refuses
    it, never a value that would otherwise reach `heartbeat_job` and then
    raise while the library page renders it.
    """
    from orchestrator.db import queries

    written = []

    async def spy_heartbeat_job(conn, job_id, **kwargs):
        written.append(kwargs)
        return {"cancel_requested": False}

    monkeypatch.setattr(queries, "heartbeat_job", spy_heartbeat_job, raising=False)

    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
                "progress_pct": "NaN",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 422
    assert written == [], "a value that fails validation must never reach the database"


async def test_a_progress_value_outside_the_percentage_range_is_refused(client):
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "working",
                "progress_pct": 142.0,
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [("eta_seconds", -1), ("realtime_factor", 0), ("attempt", 0)],
)
async def test_a_negative_estimate_a_non_positive_speed_and_an_attempt_below_one_are_refused(
    client, field, value
):
    body = {
        "protocol": WORKER_PROTOCOL_VERSION,
        "worker": "ml-server",
        "attempt": 1,
        "stage": "working",
    }
    body[field] = value
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat", json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_a_heartbeat_with_an_unknown_stage_word_is_refused_naming_the_field(client):
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/heartbeat",
            json={
                "protocol": WORKER_PROTOCOL_VERSION,
                "worker": "ml-server",
                "attempt": 1,
                "stage": "not-a-real-stage",
            },
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 422
    assert "stage" in str(resp.json()).lower()


async def test_a_claim_with_an_unknown_check_in_status_is_refused(client):
    async with client:
        resp = await client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server", "status": "not-a-real-status"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 422
    assert "status" in str(resp.json()).lower()


async def test_a_success_body_missing_the_stored_objects_size_is_refused(client):
    body = _valid_done_body()
    del body["size_bytes"]
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/done",
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code == 422


async def test_a_success_body_with_no_subtitle_burned_is_accepted(client):
    """A conversion with no subtitle chosen has nothing to name there --
    the two fields are still required keys, but `null` is a legitimate
    value for both, never a reason to refuse the whole report.
    """
    body = _valid_done_body(subtitle_burned_language=None, subtitle_burned_title=None)
    async with client:
        resp = await client.post(
            f"/worker/jobs/{STUB_JOB_ID}/done",
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
    assert resp.status_code != 422


def test_done_body_carries_room_for_every_technical_field_the_row_needs():
    from orchestrator.web.routes_worker import DoneBody

    fields = set(DoneBody.model_fields)
    needed = {
        "source_codec",
        "source_profile",
        "subtitle_burned_language",
        "subtitle_burned_title",
        "size_bytes",
        "duration_seconds",
        "width",
        "height",
        "fps",
        "video_profile",
        "video_level",
        "pix_fmt",
        "audio_codec",
        "audio_sample_rate",
        "audio_channel_layout",
        "audio_language",
        "audio_title",
        "audio_reencoded",
        "poster_key",
        "realtime_factor",
    }
    assert needed <= fields, needed - fields


async def test_the_success_body_the_client_posts_matches_the_route_models_own_fields():
    """Assembles a success body through the real `WorkerClient.done()`
    against a substituted transport (the same mechanism
    `test_worker_client.py` already uses for the client's own tests) and
    compares the posted keys against `DoneBody`'s own field names -- no
    key the service does not know, and no required field the converter
    omits. Both sides are read from their own definitions: the posted
    body comes from actually calling `done()`, never a hand-written list,
    and the expected set comes from `DoneBody.model_fields` itself.

    Mutation this test guards against: a field added to `DoneBody`
    without a matching parameter added to `WorkerClient.done()` (or the
    reverse) makes the two sets stop matching -- this test then fails in
    the same second, instead of after a four-minute encode posts a body
    the service refuses.
    """
    from orchestrator.web.routes_worker import DoneBody
    from worker.client import WorkerClient

    posted: dict = {}

    async def handler(request):
        posted.update(json.loads(request.content))
        return httpx.Response(204)

    worker_client = WorkerClient(
        base_url="http://testserver",
        token=WORKER_TOKEN,
        worker_name="ml-server",
        transport=httpx.MockTransport(handler),
    )
    done_kwargs = {
        k: v for k, v in _valid_done_body().items() if k not in ("protocol", "worker")
    }
    try:
        await worker_client.done(STUB_JOB_ID, **done_kwargs)
    finally:
        await worker_client.aclose()

    assert set(posted) == set(DoneBody.model_fields)


# --- Atomicity against a real Postgres (CR-03/IN-10) ---------------------
#
# Everything above drives the routes with `_DummyPool`/`_DummyConn` --
# adequate for the gate, the protocol check and the model's own
# validation, but a fake connection cannot exhibit a real transaction
# rolling back. These few tests run the real app against the real
# migrated Postgres this suite already stands up for its other
# integration coverage, so "the job's status is unchanged" and "no job is
# left running" are read back off the actual row, not inferred from a
# spy.


@pytest.fixture
def worker_client(app_pool):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.integration
async def test_a_raise_after_the_terminal_write_leaves_the_job_running_and_deletes_nothing(
    raw_conn, worker_client, monkeypatch
):
    from orchestrator.db import queries
    from orchestrator.web import routes_worker

    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn,
        "Atomic Done Route",
        f"atomic-done-route-{uuid.uuid4().hex[:8]}",
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    job_id = await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=edition_id
    )
    claimed = await queries.claim_job(raw_conn, kind="transcode", worker_name="ml-server")
    assert claimed["id"] == job_id

    deleted = []
    monkeypatch.setattr(routes_worker, "delete_object", deleted.append, raising=False)

    async def failing_set_technical(*args, **kwargs):
        raise RuntimeError("a write after the terminal write exploded")

    monkeypatch.setattr(queries, "set_edition_technical", failing_set_technical)

    body = _valid_done_body(attempt=claimed["attempts"], worker="ml-server")

    async with worker_client:
        resp = await worker_client.post(
            f"/worker/jobs/{job_id}/done",
            json=body,
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert resp.status_code == 503
    assert deleted == []

    row = await raw_conn.fetchrow(
        "SELECT status, finished_at FROM jobs WHERE id = $1", job_id
    )
    assert row["status"] == "running"
    assert row["finished_at"] is None

    title = await raw_conn.fetchrow("SELECT status FROM titles WHERE id = $1", title_id)
    assert title["status"] == "preparing"


@pytest.mark.integration
async def test_a_raise_between_the_claim_and_the_object_key_lookup_leaves_no_job_running(
    raw_conn, worker_client, monkeypatch
):
    from orchestrator.db import queries

    title_id, edition_id = await _title_and_edition_for_claim(raw_conn)
    await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=edition_id
    )

    async def failing_edition_object_key(conn, edition_id):
        raise RuntimeError("the lookup after the claim exploded")

    monkeypatch.setattr(queries, "edition_object_key", failing_edition_object_key)

    async with worker_client:
        resp = await worker_client.post(
            "/worker/claim",
            json={"protocol": WORKER_PROTOCOL_VERSION, "worker": "ml-server"},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )

    assert resp.status_code == 204, "the existing tolerance: a hiccup reads as an empty queue"

    row = await raw_conn.fetchrow(
        "SELECT status, worker_name, attempts FROM jobs WHERE title_id = $1", title_id
    )
    assert row["status"] == "queued", "the claim itself must have rolled back too"
    assert row["worker_name"] is None
    assert row["attempts"] == 0


async def _title_and_edition_for_claim(conn) -> tuple:
    from orchestrator.db import queries

    edition_id = uuid.uuid4()
    slug = f"claim-atomic-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        conn, "Atomic Claim Route", slug, edition_id, f"{edition_id}.mp4", status="preparing"
    )
    return title_id, edition_id


# --- the converter's own probe-mismatch guard branches around a null
# embedded index (D-180) ---------------------------------------------------
#
# The two tests below drive `worker.__main__._process_job` directly, the
# same layer `test_worker_job.py` exercises -- placed here rather than
# there because this plan's own file list names this module, and the
# claim response this file already pins (`external_audio_path`) is the
# field that makes the job below reachable at all.


def _external_audio_job_probe_json():
    """One video stream plus one embedded Japanese track -- the embedded
    track the operator is replacing, not using, when the job also carries
    an external audio choice.
    """
    return {
        "streams": [
            {
                "index": 0, "codec_type": "video", "codec_name": "h264",
                "profile": "High", "level": 40, "pix_fmt": "yuv420p",
                "width": 1920, "height": 1080, "avg_frame_rate": "24/1",
            },
            {
                "index": 1, "codec_type": "audio", "codec_name": "aac",
                "channels": 2, "sample_rate": "48000", "channel_layout": "stereo",
                "tags": {"language": "jpn"},
            },
        ],
        "format": {"duration": "60.0", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }


def _external_audio_dub_probe_json():
    """The external `.mka`'s own probe -- a different channel count and
    language than the embedded track, so a report reading the wrong one
    is easy to catch.
    """
    return {
        "streams": [
            {
                "index": 0, "codec_type": "audio", "codec_name": "flac",
                "channels": 6, "sample_rate": "48000", "channel_layout": "5.1",
                "tags": {"language": "rus"},
            },
        ],
        "format": {"duration": "60.5"},
    }


class _FakeConverterClient:
    """The minimal `WorkerClient` surface `_process_job` calls -- enough
    to record the outcome, nothing that touches the network.
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


def _external_audio_job(**overrides) -> dict:
    job = {
        "job_id": str(uuid.uuid4()),
        "attempt": 1,
        "object_key": "out.mp4",
        "edition_id": str(uuid.uuid4()),
        "source_path": "movie.mkv",
        "audio_stream_index": None,
        "subtitle_kind": "none",
        "subtitle_stream_index": None,
        "subtitle_path": None,
        "stereo_layout": "2d",
        "external_audio_path": None,
    }
    job.update(overrides)
    return job


def _run_converter_job(monkeypatch, tmp_path, job) -> _FakeConverterClient:
    import worker.__main__ as worker_main
    from worker.settings import WorkerSettings

    client = _FakeConverterClient()

    def fake_probe(path):
        text = str(path)
        if text.endswith("output.mp4"):
            return _external_audio_job_probe_json()
        if text.endswith(".mka"):
            return _external_audio_dub_probe_json()
        return _external_audio_job_probe_json()

    async def fake_run(cmd, *, should_cancel=lambda: False, on_progress=None, **kwargs):
        from pathlib import Path

        Path(cmd[-1]).write_bytes(b"stub-output-bytes")
        return type("FakeResult", (), {"cancelled": False})()

    monkeypatch.setattr(worker_main.compat, "probe", fake_probe)
    monkeypatch.setattr(worker_main.compat, "moov_before_mdat", lambda path: True)
    monkeypatch.setattr(worker_main, "run_ffmpeg_with_progress", fake_run)
    monkeypatch.setattr(worker_main.worker_storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(worker_main.worker_storage, "abort_multipart", lambda *a, **k: None)
    monkeypatch.setattr(worker_main.remux, "run_ffmpeg", lambda *a, **k: None)

    share = tmp_path / "share"
    work = tmp_path / "work"
    share.mkdir()
    work.mkdir()
    settings = WorkerSettings(
        orchestrator_url="http://orchestrator.local",
        worker_token=WORKER_TOKEN,
        s3_endpoint="http://storage:8333",
        s3_bucket="mytube",
        worker_s3_key="worker-key",
        worker_s3_secret="worker-secret",
        share_root=str(share),
        work_dir=str(work),
    )

    asyncio.run(
        worker_main._process_job(
            client, object(), settings, job, tmp_path / "scratch", asyncio.Event()
        )
    )
    return client


def test_a_convert_job_with_external_audio_and_a_null_index_reaches_a_done_report(
    monkeypatch, tmp_path
):
    """The owner's own primary case (D-180): the embedded track is never
    consulted, the external file is probed instead, and the job finishes
    rather than tripping the guard built for an embedded index.
    """
    job = _external_audio_job(external_audio_path="RUS Sound/AniStar/dub.mka")

    client = _run_converter_job(monkeypatch, tmp_path, job)

    assert client.failed_calls == []
    assert client.done_calls


def test_a_missing_embedded_index_with_no_external_audio_still_raises_probe_mismatch(
    monkeypatch, tmp_path
):
    """The embedded guard was branched around for an external-audio job,
    not weakened or removed -- a job naming an embedded index that is no
    longer on the file still fails exactly as it did before this plan.
    """
    job = _external_audio_job(audio_stream_index=99, external_audio_path=None)

    client = _run_converter_job(monkeypatch, tmp_path, job)

    assert client.done_calls == []
    assert client.failed_calls == ["failed.reason.probe_mismatch"]
