"""Route tests for the folder confirmation screen (`GET /add-folder/{path}`)
-- 05-UI-SPEC.md Screen A/B. Follows `test_add_form.py`'s own shape: no
real Postgres or subprocess, `ffprobe` and the database layer both
monkeypatched at their module boundary.
"""
import json
from pathlib import Path

import httpx
import pytest

from mytube_media.compat import ProbeError
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.web import routes_folder, routes_share

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _probe_returning(fixture_name: str):
    data = _fixture(fixture_name)

    def _probe(path):
        return data

    return _probe


def _probe_raising():
    def _probe(path):
        raise ProbeError("simulated unreadable file")

    return _probe


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
def folder_share_root(tmp_path):
    """A season folder with three episodes, plus a subfolder holding a
    fourth video that must never be listed or counted -- the direct-
    files-only rule (D-166/D-167) this whole screen rests on.
    """
    root = tmp_path / "share"
    root.mkdir()
    season = root / "Horimiya"
    season.mkdir()
    for n in (1, 2, 3):
        (season / f"Horimiya - {n:02d}.mkv").write_bytes(b"stub video bytes")
    (season / "notes.txt").write_text("hello", encoding="utf-8")
    dubs = season / "RUS Sound" / "AniStar"
    dubs.mkdir(parents=True)
    (dubs / "bonus.mkv").write_bytes(b"stub video bytes")

    single = root / "OneFile"
    single.mkdir()
    (single / "a-video.mp4").write_bytes(b"stub video bytes")
    return root


@pytest.fixture
def _web_env(folder_share_root, tmp_path, monkeypatch):
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(folder_share_root))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_stub(monkeypatch):
    class _Stub:
        already_added_rows: list[dict] = []

    stub = _Stub()
    stub.already_added_rows = []

    async def _editions_by_source_paths(conn, paths):
        return [row for row in stub.already_added_rows if row["source_path"] in paths]

    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)
    return stub


@pytest.fixture
def web_client(_web_env, db_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


# --- Task 1: the button inside the opened folder -------------------------


async def test_button_renders_on_a_listing_with_two_video_files(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert resp.status_code == 200
    assert "share.cta.add_folder" not in resp.text  # a real translated label, not the key
    assert 'action="/add-folder/Horimiya"' in resp.text
    assert "Add folder as series" in resp.text


async def test_button_does_not_render_on_a_listing_with_one_video_file(web_client):
    async with web_client:
        resp = await web_client.get("/add/OneFile")

    assert resp.status_code == 200
    assert "/add-folder/" not in resp.text


async def test_button_does_not_render_on_a_listing_with_zero_video_files(web_client, tmp_path):
    async with web_client:
        resp = await web_client.get("/add")  # the root: two folders, no direct video

    assert resp.status_code == 200
    assert "/add-folder" not in resp.text


async def test_button_does_not_render_inside_a_folder_row_of_a_parent_listing(web_client):
    async with web_client:
        resp = await web_client.get("/add")

    assert resp.status_code == 200
    assert "Horimiya" in resp.text  # the folder row itself is still listed
    assert "/add-folder/" not in resp.text


async def test_button_is_not_nested_inside_the_row_link(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    # The folder-add form sits in its own action row, never inside
    # .share-row__link -- a real nesting bug would place the <form> between
    # <a class="share-row__link"...> and its own closing tag.
    listing_start = resp.text.index('<ul class="share-listing"')
    assert 'action="/add-folder' not in resp.text[listing_start:]


async def test_edition_label_shows_when_every_file_resolves_to_one_title(web_client, db_stub):
    db_stub.already_added_rows = [
        {"source_path": f"Horimiya/Horimiya - {n:02d}.mkv", "title_id": "same-title"}
        for n in (1, 2, 3)
    ]

    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add edition for these episodes" in resp.text
    assert "mode=edition" in resp.text


async def test_plain_label_shows_when_files_resolve_to_several_titles(web_client, db_stub):
    db_stub.already_added_rows = [
        {"source_path": "Horimiya/Horimiya - 01.mkv", "title_id": "title-a"},
        {"source_path": "Horimiya/Horimiya - 02.mkv", "title_id": "title-b"},
    ]

    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add folder as series" in resp.text
    assert "mode=edition" not in resp.text


async def test_plain_label_shows_when_no_files_are_already_added(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add folder as series" in resp.text


async def test_folder_route_refuses_a_path_outside_the_share(web_client, monkeypatch):
    """A literal `../` traversal cannot be exercised through httpx/ASGI --
    both normalize dot segments client-side before the request is ever
    sent (see 02-02-PLAN.md's own note on this). `safe_join` is the real
    control and has its own direct tests in test_share.py; here the
    contract under test is that this route calls it and answers 400 when
    it raises, exactly like every other share route.
    """
    from mytube_media.share import ShareEscape

    def _raise(root, rel):
        raise ShareEscape("simulated escape")

    monkeypatch.setattr(routes_share, "safe_join", _raise)

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    assert resp.status_code == 400


async def test_folder_route_refuses_a_file_path(web_client):
    async with web_client:
        resp = await web_client.get("/add-folder/OneFile/a-video.mp4")

    assert resp.status_code == 400


async def test_folder_route_refuses_a_folder_with_fewer_than_two_video_files(web_client):
    async with web_client:
        resp = await web_client.get("/add-folder/OneFile")

    assert resp.status_code == 400


async def test_both_catalogs_hold_the_folder_button_keys():
    import json as _json

    en = _json.loads(
        (Path(__file__).parent.parent / "orchestrator" / "locales" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    ru = _json.loads(
        (Path(__file__).parent.parent / "orchestrator" / "locales" / "ru.json").read_text(
            encoding="utf-8"
        )
    )
    for key in ("share.cta.add_folder", "share.cta.add_edition_folder", "folder.cta.probing"):
        assert key in en
        assert key in ru
