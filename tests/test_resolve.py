"""Unit tests for /v/{slug} and /refresh/{edition_id} -- the db.queries layer
is monkeypatched, no real database involved (see conftest.py's `settings`
fixture for the env vars this needs).
"""
import uuid

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app

ACTIVE_EDITION_ID = uuid.uuid4()
ACTIVE_TITLE_ID = uuid.uuid4()


class _DummyConn:
    pass


class _DummyAcquireCtx:
    async def __aenter__(self):
        return _DummyConn()

    async def __aexit__(self, *exc_info):
        return False


class _DummyPool:
    def acquire(self):
        return _DummyAcquireCtx()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _app_env(settings):
    """Pull in conftest's `settings` fixture for its side effect: setting
    MYTUBE_* env vars so get_settings() (used by resolve.py) doesn't blow up
    with a missing-field ValidationError. get_settings is @lru_cache'd, so
    clear it before and after -- otherwise whichever test in the whole
    session calls it first pins its Settings() for every later test/module.
    """
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client():
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _active_row(edition_id=ACTIVE_EDITION_ID):
    return {
        "title_id": ACTIVE_TITLE_ID,
        "status": "active",
        "default_edition_id": edition_id,
    }


async def test_resolve_active_slug_returns_302_with_signed_absolute_location(
    monkeypatch, client
):
    async def fake_resolve_slug(conn, slug):
        assert slug == "ataka-titanov"
        return _active_row()

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    async with client:
        resp = await client.get(
            "/v/ataka-titanov", headers={"host": "mytube.example"}, follow_redirects=False
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://mytube.example/blob/")
    assert "/blob/" in location
    assert "exp=" in location
    assert "sig=" in location


async def test_resolve_unknown_slug_returns_404(monkeypatch, client):
    async def fake_resolve_slug(conn, slug):
        return None

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    async with client:
        resp = await client.get("/v/does-not-exist", follow_redirects=False)

    assert resp.status_code == 404


@pytest.mark.parametrize("status", ["pending_delete", "deleted"])
async def test_resolve_non_active_title_returns_410(monkeypatch, client, status):
    async def fake_resolve_slug(conn, slug):
        return {
            "title_id": ACTIVE_TITLE_ID,
            "status": status,
            "default_edition_id": ACTIVE_EDITION_ID,
        }

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    async with client:
        resp = await client.get("/v/gone", follow_redirects=False)

    assert resp.status_code == 410


async def test_resolve_after_rename_gives_same_redirect_path_as_before(
    monkeypatch, client
):
    """The queries layer already guarantees old and new slug both
    resolve to the same title/edition (proven in test_slug_history.py) --
    this checks the HTTP layer treats them identically, i.e. it doesn't
    special-case "is this the current slug" anywhere.
    """

    async def fake_resolve_slug(conn, slug):
        assert slug in ("old-slug", "new-slug")
        return _active_row()

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    async with client:
        old_resp = await client.get("/v/old-slug", follow_redirects=False)
        new_resp = await client.get("/v/new-slug", follow_redirects=False)

    old_path = old_resp.headers["location"].split("?")[0]
    new_path = new_resp.headers["location"].split("?")[0]
    assert old_path == new_path == f"https://testserver/blob/{ACTIVE_EDITION_ID}.mp4"


async def test_response_does_not_depend_on_user_agent_or_accept(monkeypatch, client):
    """Browser and AVPro get the same 302, no User-Agent/Accept branching."""

    async def fake_resolve_slug(conn, slug):
        return _active_row()

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    async with client:
        browser_resp = await client.get(
            "/v/ataka-titanov",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            follow_redirects=False,
        )
        avpro_resp = await client.get(
            "/v/ataka-titanov",
            headers={"User-Agent": "AVProVideo/1.0", "Accept": "*/*"},
            follow_redirects=False,
        )

    browser_path = browser_resp.headers["location"].split("?")[0]
    avpro_path = avpro_resp.headers["location"].split("?")[0]
    assert browser_resp.status_code == avpro_resp.status_code == 302
    assert browser_path == avpro_path


async def test_forwarded_host_changes_location_host(monkeypatch, client):
    async def fake_resolve_slug(conn, slug):
        return _active_row()

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    async with client:
        resp_a = await client.get(
            "/v/ataka-titanov",
            headers={"x-forwarded-host": "a.example", "host": "wrong.example"},
            follow_redirects=False,
        )
        resp_b = await client.get(
            "/v/ataka-titanov",
            headers={"x-forwarded-host": "b.example", "host": "wrong.example"},
            follow_redirects=False,
        )
        resp_no_forwarded = await client.get(
            "/v/ataka-titanov",
            headers={"host": "fallback.example"},
            follow_redirects=False,
        )

    assert resp_a.headers["location"].startswith("https://a.example/")
    assert resp_b.headers["location"].startswith("https://b.example/")
    assert resp_no_forwarded.headers["location"].startswith("https://fallback.example/")


async def test_refresh_valid_edition_returns_302_with_retry_marker(monkeypatch, client):
    async def fake_resolve_edition(conn, edition_id):
        assert edition_id == ACTIVE_EDITION_ID
        return {"edition_id": ACTIVE_EDITION_ID, "status": "active"}

    monkeypatch.setattr(queries, "resolve_edition", fake_resolve_edition)

    async with client:
        resp = await client.get(
            f"/refresh/{ACTIVE_EDITION_ID}", follow_redirects=False
        )

    assert resp.status_code == 302
    assert "retry=1" in resp.headers["location"]
    assert f"/blob/{ACTIVE_EDITION_ID}.mp4" in resp.headers["location"]


async def test_refresh_unknown_edition_returns_404(monkeypatch, client):
    async def fake_resolve_edition(conn, edition_id):
        return None

    monkeypatch.setattr(queries, "resolve_edition", fake_resolve_edition)

    async with client:
        resp = await client.get(f"/refresh/{uuid.uuid4()}", follow_redirects=False)

    assert resp.status_code == 404


async def test_refresh_deleted_titles_edition_returns_410(monkeypatch, client):
    async def fake_resolve_edition(conn, edition_id):
        return {"edition_id": edition_id, "status": "deleted"}

    monkeypatch.setattr(queries, "resolve_edition", fake_resolve_edition)

    async with client:
        resp = await client.get(
            f"/refresh/{ACTIVE_EDITION_ID}", follow_redirects=False
        )

    assert resp.status_code == 410


async def test_refresh_non_uuid_returns_404_without_touching_db(monkeypatch, client):
    called = False

    async def fake_resolve_edition(conn, edition_id):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(queries, "resolve_edition", fake_resolve_edition)

    async with client:
        resp = await client.get("/refresh/not-a-uuid", follow_redirects=False)

    assert resp.status_code == 404
    assert called is False


async def test_signature_expiry_matches_sign_ttl_seconds(monkeypatch, client, settings):
    import time

    async def fake_resolve_slug(conn, slug):
        return _active_row()

    monkeypatch.setattr(queries, "resolve_slug", fake_resolve_slug)

    before = int(time.time())
    async with client:
        resp = await client.get("/v/ataka-titanov", follow_redirects=False)
    after = int(time.time())

    query = resp.headers["location"].split("?", 1)[1]
    params = dict(p.split("=") for p in query.split("&"))
    exp = int(params["exp"])

    assert before + settings.sign_ttl_seconds <= exp <= after + settings.sign_ttl_seconds
