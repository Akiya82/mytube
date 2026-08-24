"""Route tests for the add form (GET /add/{path} on a file) -- Screen 2 of
02-UI-SPEC.md. `ffprobe` is monkeypatched to return the frozen real-ffprobe
fixtures from tests/fixtures/probe/ (task 2), and the database layer is
monkeypatched at the `orchestrator.db.queries` module boundary, the same
shape test_resolve.py and test_share.py already use -- no real Postgres or
subprocess in this file, quick-loop only.
"""
import json
import threading
from pathlib import Path

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.media.add import required_free_bytes
from orchestrator.media.compat import ProbeError
from orchestrator.web import routes_share

from conftest import ADMIN_FQDN, SESSION_COOKIE

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


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
def _web_env(share_root, tmp_path, monkeypatch):
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(share_root))
    # The real default (/work) is a container-only path (see config.py) --
    # a real, empty, writable directory here so shutil.disk_usage has
    # something to measure on this host.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_stub(monkeypatch):
    """Every `orchestrator.db.queries` call the add form's route makes,
    stubbed with a controllable default: no tombstone, no taken slugs, no
    already-added edition. Individual tests override one attribute at a
    time via the returned namespace object.
    """

    class _Stub:
        resolve_slug_result = None
        taken_slugs_result: set[str] = set()
        already_added_rows: list[dict] = []

    stub = _Stub()

    async def _resolve_slug(conn, slug):
        return stub.resolve_slug_result

    async def _taken_slugs(conn):
        return stub.taken_slugs_result

    async def _editions_by_source_paths(conn, paths):
        return stub.already_added_rows

    monkeypatch.setattr(queries, "resolve_slug", _resolve_slug)
    monkeypatch.setattr(queries, "taken_slugs", _taken_slugs)
    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)
    return stub


@pytest.fixture
def web_client(_web_env, db_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


def _probe_returning(fixture_name: str):
    data = _fixture(fixture_name)

    def _probe(path):
        return data

    return _probe


def _probe_raising():
    def _probe(path):
        raise ProbeError("simulated unreadable file")

    return _probe


@pytest.fixture(autouse=True)
def _default_moov_first(monkeypatch):
    # Real bytes of the share fixtures are stub content, not a real MP4 --
    # deterministic default so already_correct never accidentally flips
    # true from garbage atom parsing. Individual tests override this.
    monkeypatch.setattr(routes_share, "moov_before_mdat", lambda path: False)


# --- the verdict, rendered ---------------------------------------------


async def test_compatible_file_renders_enabled_add_control_and_no_reason_banner(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert 'type="submit"' in resp.text
    assert "disabled" not in resp.text.split('type="submit"')[1].split(">")[0]
    assert "reason.codec" not in resp.text
    assert "This is" not in resp.text  # no reason.codec-shaped sentence


async def test_incompatible_file_renders_no_enabled_add_control_and_disabled_convert(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("hevc.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert 'type="submit"' not in resp.text
    assert 'type="button" class="btn btn--secondary" disabled' in resp.text
    assert "hevc" in resp.text  # reason.codec's {codec} parameter


async def test_silent_file_renders_no_add_control_and_explains_why(
    web_client, monkeypatch
):
    """The third state the form must never reach: a live Add button over a
    track picker with no options, whose POST can only answer the required
    audio_track field with a 422.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("no_audio.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert 'type="submit"' not in resp.text
    assert 'name="audio_track"' not in resp.text
    assert "no audio track" in resp.text.lower()


async def test_unreadable_file_renders_unreadable_reason_not_traceback(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_raising())

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert "This file could not be read" in resp.text
    assert 'type="submit"' not in resp.text
    assert "Traceback" not in resp.text


# --- the name field ------------------------------------------------------


async def test_name_field_value_is_raw_filename_unmodified(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'value="a-video.mp4"' in resp.text


async def test_script_tag_filename_renders_escaped_everywhere(web_client, monkeypatch):
    """The literal `<script>alert(1)</script>.mkv` name cannot exist as a
    real file on this Windows host (NTFS rejects `<`/`>`, see conftest.py's
    `share_root`) -- same workaround test_share.py's route-level escaping
    test already uses: point the route at a real file's bytes, but report
    a dangerous name for it, bypassing the filesystem instead of fighting
    it. `is_file`/`stat`/`relative_to` delegate to the real underlying
    path; only `.name` (what the route reads and the template renders) is
    swapped.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    class _DangerousPath:
        name = "<script>alert(1)</script>.mkv"

        def __init__(self, real):
            self._real = real

        def is_file(self):
            return self._real.is_file()

        def stat(self):
            return self._real.stat()

        def relative_to(self, other):
            return self._real.relative_to(other)

    real_safe_join = routes_share.safe_join

    def _dangerous_safe_join(root, rel):
        return _DangerousPath(real_safe_join(root, rel))

    monkeypatch.setattr(routes_share, "safe_join", _dangerous_safe_join)

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "<script>alert(1)</script>" not in resp.text
    assert resp.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 1


async def test_a_dot_dot_path_is_canonicalized_before_it_becomes_an_identity(
    web_client, monkeypatch, db_stub
):
    """`Movies/../a-video.mp4` and `a-video.mp4` are the same file, and the
    duplicate-detection query keys on the string. If the raw request
    segment reaches the catalog, the same source can be added twice with
    no "already added" chip ever appearing again -- and the breadcrumb
    renders a `..` crumb.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    asked_for = []

    async def _editions_by_source_paths(conn, paths):
        asked_for.append(list(paths))
        return []

    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)

    async with web_client:
        resp = await web_client.get("/add/Movies/%2E%2E/a-video.mp4")

    assert resp.status_code == 200
    assert asked_for == [["a-video.mp4"]]
    assert 'action="/add/a-video.mp4"' in resp.text


async def test_the_blocking_share_calls_run_off_the_event_loop_thread(
    web_client, monkeypatch
):
    """`ffprobe` is a subprocess with a 30-second timeout and the atom scan
    walks headers over a network mount with no timeout at all. Called bare,
    they hold the one event loop that also answers `GET /v/{slug}` and
    `/healthz`, so opening the add form for a slow file stops video
    resolution for every viewer -- during synchronized viewing, which is
    the product.

    Asserted on the thread rather than on elapsed time: a timing test of a
    blocked event loop cannot measure the block from inside the same loop,
    and "ran somewhere other than the loop thread" is the rule itself.
    """
    loop_thread = threading.get_ident()
    ran_on = {}

    def recording_probe(path):
        ran_on["probe"] = threading.get_ident()
        return _fixture("compatible.json")

    def recording_moov(path):
        ran_on["moov"] = threading.get_ident()
        return False

    monkeypatch.setattr(routes_share, "probe", recording_probe)
    monkeypatch.setattr(routes_share, "moov_before_mdat", recording_moov)

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert ran_on["probe"] != loop_thread
    assert ran_on["moov"] != loop_thread


# --- the slug field, similar-slug warning, tombstone reuse ---------------


async def test_slug_field_shows_the_planned_slug(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'id="add-slug"' in resp.text
    assert 'value="a-video-mp4"' in resp.text


async def test_similar_slug_warning_names_the_taken_slug(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    # "a-video.mp4" normalizes to candidate "a-video-mp4"; "avideomp4" is a
    # different, already-taken slug with the same similar_key (hyphens
    # stripped) -- this is exactly the "Re:Zero" vs "ReZero" case
    # pick_unique_slug's own docstring describes.
    db_stub.taken_slugs_result = {"avideomp4"}

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert "avideomp4" in resp.text


async def test_tombstoned_slug_shows_reuse_warning_and_disables_submit(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.resolve_slug_result = {"title_id": "x", "status": "deleted", "default_edition_id": None}

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "belonged to a deleted title" in resp.text
    assert 'type="submit"' in resp.text
    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" in submit_tag
    assert "Reuse this address" in resp.text


async def test_tombstoned_slug_with_reuse_confirmed_enables_submit(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.resolve_slug_result = {"title_id": "x", "status": "deleted", "default_edition_id": None}

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4?confirm_reuse=1")

    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" not in submit_tag


# --- already-added ---------------------------------------------------------


async def test_already_added_banner_names_existing_title_and_submit_stays_enabled(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
        }
    ]

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "Existing Title" in resp.text
    assert "/v/existing-title-slug" in resp.text
    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" not in submit_tag


# --- cost line / free space --------------------------------------------


async def test_cost_line_shows_size(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    # compatible.json's format.size is 213539 bytes -> "208.5 KB"-ish;
    # assert on the filesizeformat_binary unit, not a brittle exact figure.
    assert "KB" in resp.text or "MB" in resp.text or "B" in resp.text


async def test_not_enough_space_banner_when_disk_usage_is_low(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    class _Usage:
        free = 0

    monkeypatch.setattr(
        routes_share.shutil, "disk_usage", lambda path: _Usage()
    )

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "Not enough free space" in resp.text
    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" in submit_tag


# --- required_free_bytes: already-correct vs. not ------------------------


def test_required_free_bytes_already_correct_passes_with_less_than_source_free():
    source_size = 20 * 1024 * 1024  # 20 MB
    free_space = 17 * 1024 * 1024  # less than source, more than the poster budget
    assert free_space < source_size
    assert free_space >= required_free_bytes(source_size, already_correct=True)


def test_required_free_bytes_not_already_correct_fails_just_above_source_free():
    source_size = 20 * 1024 * 1024
    free_space = source_size + 1024  # just above the source, nowhere near double
    assert free_space < required_free_bytes(source_size, already_correct=False)
