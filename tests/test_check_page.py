"""Route tests for GET /check/{slug} -- Screen 4 of 02-UI-SPEC.md (D-65).

The database layer is monkeypatched at the `orchestrator.db.queries`
module boundary, the same shape test_library.py and test_add_form.py
already use -- no real Postgres, quick-loop only. `resolve_slug` answers
what /v/{slug} itself would answer (title id + status); `library_row`
supplies the title name the same way the Library screen already reads it,
so this page needs no new query.
"""
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN


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


@pytest.fixture
def _web_env(monkeypatch):
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _QueriesStub:
    resolved_by_slug: dict = field(default_factory=dict)
    row_by_title_id: dict = field(default_factory=dict)


@pytest.fixture
def queries_stub(monkeypatch):
    stub = _QueriesStub()

    async def _resolve_slug(conn, slug):
        return stub.resolved_by_slug.get(slug)

    async def _library_row(conn, title_id):
        return stub.row_by_title_id.get(title_id)

    monkeypatch.setattr(queries, "resolve_slug", _resolve_slug)
    monkeypatch.setattr(queries, "library_row", _library_row)
    return stub


@pytest.fixture
def client(_web_env, queries_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


def _resolved(**over) -> dict:
    row = {"title_id": uuid.uuid4(), "status": "active", "default_edition_id": uuid.uuid4()}
    row.update(over)
    return row


def _row(**over) -> dict:
    row = {"title_id": uuid.uuid4(), "title_name": "Checked Clip"}
    row.update(over)
    return row


async def test_video_source_is_exactly_the_public_address_for_the_slug(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["kontrolnyi-rolik"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Kontrolnyi Rolik")

    async with client:
        resp = await client.get("/check/kontrolnyi-rolik")

    assert resp.status_code == 200
    assert '<video class="check-page__video" controls src="https://mytube.example/v/kontrolnyi-rolik">' in resp.text
    assert "/blob/" not in resp.text
    assert "sig=" not in resp.text


async def test_unknown_slug_is_404(client, queries_stub):
    async with client:
        resp = await client.get("/check/does-not-exist")

    assert resp.status_code == 404


async def test_pending_deletion_title_renders_no_video_element(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["going-away"] = _resolved(title_id=title_id, status="pending_delete")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Going Away")

    async with client:
        resp = await client.get("/check/going-away")

    assert resp.status_code == 200
    assert "<video" not in resp.text
    assert "This address is not playing right now." in resp.text


async def test_no_related_or_other_title_list_on_the_page(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["solo-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Solo Clip")

    async with client:
        resp = await client.get("/check/solo-clip")

    assert "<ul" not in resp.text
    assert "<ol" not in resp.text
    assert "library-list" not in resp.text


async def test_hairpin_note_names_the_same_address_and_is_always_present(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["hairpin-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Hairpin Clip")

    async with client:
        resp = await client.get("/check/hairpin-clip")

    assert "https://mytube.example/v/hairpin-clip" in resp.text
    # The note is a standing explanation, not an error state -- present
    # for every active title's check page, not conditioned on anything a
    # server-rendered response could ever know about playback success.
    assert "router" in resp.text.lower()
