"""Route tests for the Library screen (GET /library, GET /library/{id}/row,
GET /poster/{edition_id}, POST /library/{id}/dismiss) -- Screen 3 of
02-UI-SPEC.md. The database layer is monkeypatched at the
`orchestrator.db.queries` module boundary and storage at the
`orchestrator.web.routes_titles` module boundary, the same shape
test_add_form.py already uses -- no real Postgres, no real S3, quick-loop
only.
"""
import pathlib
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx
import pytest
from botocore.exceptions import ClientError

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.storage import StorageUsage
from orchestrator.web import routes_titles

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN


class _DummyTxCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


class _DummyConn:
    """A stand-in `asyncpg.Connection`. `transaction()` exists only for
    `retry_route`'s own `async with conn.transaction():` wrapper --
    everything it does inside that block is a fully-stubbed
    `queries.*` call, so nothing here ever reaches real SQL.
    """

    def transaction(self):
        return _DummyTxCtx()


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
def _web_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(tmp_path))
    monkeypatch.setenv("MYTUBE_STORAGE_QUOTA_BYTES", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _QueriesStub:
    rows: list = field(default_factory=list)
    row_by_id: dict = field(default_factory=dict)
    preparing_ahead_by_id: dict = field(default_factory=dict)
    failed_keys_by_id: dict = field(default_factory=dict)
    dismiss_calls: list = field(default_factory=list)
    referenced: list = field(default_factory=list)
    # The seventh query library_route now calls, alongside library_rows --
    # defaulting to empty so every test in this file keeps describing a
    # library of videos alone and keeps passing unchanged.
    playlist_rows: list = field(default_factory=list)
    # Plan 09: episodes_for_library's own stub data, keyed by title_id --
    # empty by default, so a test that never sets is_series on a row never
    # triggers the series call at all (episodes_for_library_calls stays 0).
    episodes_by_title: dict = field(default_factory=dict)
    episodes_for_library_calls: int = 0
    episodes_for_library_title_ids: list = field(default_factory=list)
    # Plan 11: editions_for_library's own stub data, keyed by episode_id --
    # empty by default, so an episode whose own edition_count stays at 1
    # never triggers this call's own round trip at all.
    editions_by_episode: dict = field(default_factory=dict)
    editions_for_library_calls: int = 0
    editions_for_library_episode_ids: list = field(default_factory=list)


@pytest.fixture
def queries_stub(monkeypatch):
    stub = _QueriesStub()

    async def _library_rows(conn):
        return stub.rows

    async def _library_row(conn, title_id):
        return stub.row_by_id.get(title_id)

    async def _count_preparing_ahead(conn, title_id):
        return stub.preparing_ahead_by_id.get(title_id, 0)

    async def _failed_title_object_keys(conn, title_id):
        if title_id not in stub.failed_keys_by_id:
            raise LookupError("not found")
        return stub.failed_keys_by_id[title_id]

    async def _delete_failed_title(conn, title_id):
        stub.dismiss_calls.append(title_id)

    async def _referenced_objects(conn):
        return stub.referenced

    async def _episodes_for_library(conn, title_ids):
        stub.episodes_for_library_calls += 1
        stub.episodes_for_library_title_ids = list(title_ids)
        rows = []
        for title_id in title_ids:
            rows.extend(stub.episodes_by_title.get(title_id, []))
        return rows

    async def _playlist_rows(conn):
        return stub.playlist_rows

    async def _editions_for_library(conn, episode_ids):
        stub.editions_for_library_calls += 1
        stub.editions_for_library_episode_ids = list(episode_ids)
        rows = []
        for episode_id in episode_ids:
            rows.extend(stub.editions_by_episode.get(episode_id, []))
        return rows

    monkeypatch.setattr(queries, "library_rows", _library_rows)
    monkeypatch.setattr(queries, "library_row", _library_row)
    monkeypatch.setattr(queries, "count_preparing_ahead", _count_preparing_ahead)
    monkeypatch.setattr(queries, "failed_title_object_keys", _failed_title_object_keys)
    monkeypatch.setattr(queries, "delete_failed_title", _delete_failed_title)
    monkeypatch.setattr(queries, "referenced_objects", _referenced_objects)
    monkeypatch.setattr(queries, "playlist_rows", _playlist_rows, raising=False)
    monkeypatch.setattr(queries, "episodes_for_library", _episodes_for_library, raising=False)
    monkeypatch.setattr(queries, "editions_for_library", _editions_for_library, raising=False)
    return stub


@dataclass
class _StorageStub:
    usage: StorageUsage = None
    poster_bytes: dict = field(default_factory=dict)
    deleted: list = field(default_factory=list)


@pytest.fixture
def storage_stub(monkeypatch):
    stub = _StorageStub(
        usage=StorageUsage(total_bytes=0, object_count=0, free_bytes=None, listing={})
    )

    def _storage_usage():
        return stub.usage

    def _get_object_bytes(key):
        if key not in stub.poster_bytes:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return stub.poster_bytes[key]

    def _delete_object(key):
        stub.deleted.append(key)

    monkeypatch.setattr(routes_titles, "storage_usage", _storage_usage)
    monkeypatch.setattr(routes_titles, "get_object_bytes", _get_object_bytes)
    monkeypatch.setattr(routes_titles, "delete_object", _delete_object)
    return stub


@pytest.fixture
def client(_web_env, queries_stub, storage_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


def _row(**over) -> dict:
    row = {
        "title_id": uuid.uuid4(),
        "title_name": "A Clip",
        "title_status": "active",
        "failure_reason": None,
        "delete_at": None,
        "started_at": None,
        "created_at": None,
        "slug": "a-clip",
        "edition_id": uuid.uuid4(),
        "size_bytes": 1_500_000_000,
        "duration_seconds": 754.0,
        "width": 1920,
        "height": 1080,
        "audio_language": "eng",
        "audio_title": "Commentary",
        "audio_reencoded": True,
        "poster_key": None,
        "source_path": "a-clip.mkv",
        "source_codec": None,
        "subtitle_burned_language": None,
        "subtitle_burned_title": None,
        "stereo_layout": None,
        "job_id": None,
        "job_kind": None,
        "job_status": None,
        "job_db_stage": None,
        "job_progress_pct": None,
        "job_eta_seconds": None,
        "job_cancel_requested": False,
        "job_queued_ahead": 0,
        "worker_name": None,
        "worker_last_seen_at": None,
        "worker_status": None,
    }
    row.update(over)
    return row


def _job(**over) -> dict:
    """The job-carrying overrides a row needs to land on a given
    `job_stage` (04-UI-SPEC.md States -> Job progress) -- merged into
    `_row(**_job(...))`'s own kwargs. `job_id` alone is what
    `derive_job_ui` (orchestrator.web.routes_titles) treats as "a live
    job exists"; every other field defaults to a plain queued remux so a
    test that only cares about one stage never has to spell out the rest.
    """
    job = {
        "job_id": uuid.uuid4(),
        "job_kind": "remux",
        "job_status": "queued",
        "job_db_stage": "queued",
        "job_progress_pct": None,
        "job_eta_seconds": None,
        "job_cancel_requested": False,
        "job_queued_ahead": 0,
    }
    job.update(over)
    return job


def _episode(**over) -> dict:
    """One raw `episodes_for_library` row -- the shape
    `routes_titles._group_episodes_by_title` reads, before `derive_job_ui`
    turns its job/worker columns into `job_stage`. `title_id` is left
    unset here on purpose: a test assigns it to key the fixture into
    `queries_stub.episodes_by_title`, exactly the way `_row()`'s own
    `title_id` is generated fresh per call rather than shared.
    """
    episode = {
        "title_id": None,
        "episode_id": uuid.uuid4(),
        "number": 1,
        "episode_status": "active",
        "edition_id": uuid.uuid4(),
        "edition_status": "active",
        "edition_failure_reason": None,
        "size_bytes": 500_000_000,
        "duration_seconds": 1400.0,
        "width": 1920,
        "height": 1080,
        "audio_language": "jpn",
        "audio_title": None,
        "audio_reencoded": False,
        "source_codec": None,
        "subtitle_burned_language": None,
        "subtitle_burned_title": None,
        "stereo_layout": None,
        "edition_count": 1,
        "source_path": "episode-source.mkv",
        "job_id": None,
        "job_kind": None,
        "job_status": None,
        "job_db_stage": None,
        "job_progress_pct": None,
        "job_eta_seconds": None,
        "job_cancel_requested": False,
        "job_queued_ahead": 0,
        "worker_name": None,
        "worker_last_seen_at": None,
        "worker_status": None,
    }
    episode.update(over)
    return episode


def _edition(**over) -> dict:
    """One raw `editions_for_library` row -- the shape
    `routes_titles._group_editions_by_episode` reads. `episode_id` is left
    unset here on purpose, the same convention `_episode()` already uses:
    a test assigns it to key the fixture into
    `queries_stub.editions_by_episode`.
    """
    edition = {
        "episode_id": None,
        "edition_id": uuid.uuid4(),
        "slug": "dub-ru",
        "edition_status": "active",
        "edition_failure_reason": None,
        "is_default": False,
        "size_bytes": 500_000_000,
        "duration_seconds": 1400.0,
        "width": 1920,
        "height": 1080,
        "audio_language": "rus",
        "audio_title": None,
        "audio_reencoded": False,
        "source_codec": None,
        "subtitle_burned_language": None,
        "subtitle_burned_title": None,
        "subtitle_burned_signs_only": None,
        "stereo_layout": None,
    }
    edition.update(over)
    return edition


# --- Task 1: technical detail, address, copy button, header, empty ------


async def test_active_row_renders_technical_detail_and_audio_note(client, queries_stub):
    queries_stub.rows = [
        _row(title_name="My Clip", size_bytes=1_073_741_824, duration_seconds=125, width=1280, height=720)
    ]

    async with client:
        resp = await client.get("/library")

    assert resp.status_code == 200
    assert "My Clip" in resp.text
    assert "1.0 GB" in resp.text
    assert "2:05" in resp.text
    assert "1280&times;720" in resp.text
    assert "eng" in resp.text
    assert "Commentary" in resp.text
    assert "audio re-encoded" in resp.text


async def test_active_row_without_edition_measurements_renders_without_detail_line(client, queries_stub):
    """A title added through the command line reaches 'active' with no
    measurements on its edition -- create_title_with_edition defaults to
    'active' and the command line has no ffprobe output to pass. The row
    still has to render: the address is the whole reason the row exists,
    and one such row must not take the entire page down with it.
    """
    queries_stub.rows = [
        _row(
            title_name="Added From The Command Line",
            size_bytes=None,
            duration_seconds=None,
            width=None,
            height=None,
            audio_language=None,
            audio_title=None,
            audio_reencoded=None,
            source_path=None,
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert resp.status_code == 200
    assert "Added From The Command Line" in resp.text
    assert "data-copy-text" in resp.text
    assert 'class="library-row__detail"' not in resp.text
    assert "None" not in resp.text


async def test_active_row_untouched_audio_shows_untouched_note(client, queries_stub):
    queries_stub.rows = [_row(audio_reencoded=False)]

    async with client:
        resp = await client.get("/library")

    assert "audio untouched" in resp.text
    assert "audio re-encoded" not in resp.text


async def test_copy_link_data_attribute_is_exact_address_no_query_string(client, queries_stub):
    queries_stub.rows = [_row(slug="kontrolnyi-rolik")]

    async with client:
        resp = await client.get("/library")

    assert 'data-copy-text="https://mytube.example/v/kontrolnyi-rolik"' in resp.text
    assert "?" not in resp.text.split('data-copy-text="')[1].split('"')[0]


async def test_healthy_row_has_no_status_chip(client, queries_stub):
    queries_stub.rows = [_row()]

    async with client:
        resp = await client.get("/library")

    assert 'class="chip' not in resp.text


async def test_script_tag_title_renders_escaped_in_heading(client, queries_stub):
    queries_stub.rows = [_row(title_name="<script>alert(1)</script>")]

    async with client:
        resp = await client.get("/library")

    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


async def test_header_shows_used_and_free_when_quota_configured(client, storage_stub):
    storage_stub.usage = StorageUsage(
        total_bytes=1_073_741_824, object_count=3, free_bytes=2_147_483_648, listing={}
    )

    async with client:
        resp = await client.get("/library")

    assert "1.0 GB" in resp.text
    assert "used" in resp.text
    assert "free" in resp.text


async def test_header_shows_used_only_when_no_quota_configured(client, storage_stub):
    storage_stub.usage = StorageUsage(
        total_bytes=1_073_741_824, object_count=3, free_bytes=None, listing={}
    )

    async with client:
        resp = await client.get("/library")

    assert "used" in resp.text
    assert "free" not in resp.text


async def test_header_shows_reconciliation_counts_when_either_is_nonzero(
    client, queries_stub, storage_stub
):
    storage_stub.usage = StorageUsage(
        total_bytes=100, object_count=1, free_bytes=None, listing={"orphan.mp4": 100}
    )
    queries_stub.referenced = [
        {"key": "gone.mp4", "kind": "video", "title_name": "Gone", "slug": "gone-slug"}
    ]

    async with client:
        resp = await client.get("/library")

    assert "1" in resp.text  # one unreferenced, one missing -- both counts render


async def test_header_shows_no_reconciliation_clause_when_both_counts_are_zero(
    client, queries_stub, storage_stub
):
    storage_stub.usage = StorageUsage(
        total_bytes=100, object_count=1, free_bytes=None, listing={"a.mp4": 100}
    )
    queries_stub.referenced = [{"key": "a.mp4", "kind": "video", "title_name": "A", "slug": "a"}]

    async with client:
        resp = await client.get("/library")

    assert "storage-audit" not in resp.text


async def test_empty_library_renders_empty_state(client, queries_stub):
    queries_stub.rows = []

    async with client:
        resp = await client.get("/library")

    assert "Nothing added yet" in resp.text
    assert "Add a file from the share to see it here." in resp.text


# --- Task 2: the poster frame --------------------------------------------


async def test_active_row_with_poster_key_renders_image_with_title_alt(client, queries_stub):
    edition_id = uuid.uuid4()
    queries_stub.rows = [_row(title_name="Poster Clip", edition_id=edition_id, poster_key=f"{edition_id}.jpg")]

    async with client:
        resp = await client.get("/library")

    assert f'src="/poster/{edition_id}"' in resp.text
    assert 'alt="Poster Clip"' in resp.text


async def test_active_row_without_poster_key_renders_placeholder(client, queries_stub):
    queries_stub.rows = [_row(poster_key=None)]

    async with client:
        resp = await client.get("/library")

    assert "<img" not in resp.text
    assert "library-row__thumb--placeholder" in resp.text
    assert "No preview available" in resp.text


async def test_preparing_row_with_poster_key_still_shows_placeholder(client, queries_stub):
    edition_id = uuid.uuid4()
    queries_stub.rows = [
        _row(title_status="preparing", started_at=None, edition_id=edition_id, poster_key=f"{edition_id}.jpg")
    ]

    async with client:
        resp = await client.get("/library")

    assert "<img" not in resp.text
    assert "library-row__thumb--placeholder" in resp.text


async def test_script_tag_title_escaped_in_alt_attribute(client, queries_stub):
    edition_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_name='<script>alert(1)</script>',
            edition_id=edition_id,
            poster_key=f"{edition_id}.jpg",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'alt="<script>' not in resp.text
    assert "alt=\"&lt;script&gt;" in resp.text


async def test_poster_route_returns_image_bytes(client, storage_stub):
    edition_id = uuid.uuid4()
    storage_stub.poster_bytes[f"{edition_id}.jpg"] = b"\xff\xd8\xfffakejpeg"

    async with client:
        resp = await client.get(f"/poster/{edition_id}")

    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xfffakejpeg"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "max-age" in resp.headers["cache-control"]


async def test_poster_route_404_for_non_uuid(client):
    async with client:
        resp = await client.get("/poster/not-a-uuid")

    assert resp.status_code == 404


async def test_poster_route_404_when_object_missing(client):
    async with client:
        resp = await client.get(f"/poster/{uuid.uuid4()}")

    assert resp.status_code == 404


# --- Task 3: preparing/failed states, the fragment route, dismiss -------


async def test_fragment_route_returns_only_row_markup(client, queries_stub):
    title_id = uuid.uuid4()
    row = _row(title_id=title_id, title_name="Fragment Clip")
    queries_stub.row_by_id[title_id] = row

    async with client:
        resp = await client.get(f"/library/{title_id}/row")

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "<head" not in resp.text
    assert "<body" not in resp.text
    assert resp.text.count("<li ") == 1
    assert "Fragment Clip" in resp.text


async def test_waiting_row_reports_count_ahead_from_database(client, queries_stub):
    title_id = uuid.uuid4()
    row = _row(
        title_id=title_id,
        title_status="preparing",
        started_at=None,
        **_job(job_queued_ahead=1),
    )
    queries_stub.rows = [row]

    async with client:
        resp = await client.get("/library")

    assert "1 ahead of you" in resp.text


async def test_working_and_waiting_rows_have_different_state_text(client, queries_stub):
    waiting_id = uuid.uuid4()
    working_id = uuid.uuid4()
    queries_stub.rows = [
        _row(title_id=waiting_id, title_status="preparing", started_at=None, **_job()),
        _row(
            title_id=working_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(job_status="running", job_db_stage="working", job_progress_pct=42.0, job_eta_seconds=120),
        ),
    ]

    async with client:
        resp = await client.get("/library")

    assert "Waiting" in resp.text
    assert "Working" in resp.text


# --- Job progress: one attribute pair, six in-flight stages (04-11) -----
# The wrapper keeps data-row-status="preparing" (the exact value the
# poller already binds on) through every one of these; data-job-stage
# carries the finer value the chip dispatch above actually reads.


async def test_stage_queued_keeps_the_pollers_preparing_value(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at=None,
            **_job(job_kind="remux", job_status="queued", job_db_stage="queued", job_queued_ahead=4),
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' in resp.text
    assert 'data-job-stage="queued"' in resp.text
    assert "4 ahead of you" in resp.text


async def test_stage_waiting_for_converter_names_the_worker_and_time_since_seen(client, queries_stub):
    from datetime import datetime, timedelta, timezone

    title_id = uuid.uuid4()
    stale_seen = datetime.now(timezone.utc) - timedelta(minutes=10)
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at=None,
            **_job(job_kind="transcode", job_status="queued", job_db_stage="queued"),
            worker_name="ml-server",
            worker_last_seen_at=stale_seen,
            worker_status="idle",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' in resp.text
    assert 'data-job-stage="waiting_for_converter"' in resp.text
    assert "chip--waiting" in resp.text
    assert "Waiting for a converter" in resp.text
    assert "ml-server" in resp.text


async def test_stage_waiting_for_converter_with_no_worker_ever_connected(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at=None,
            **_job(job_kind="transcode", job_status="queued", job_db_stage="queued"),
            worker_name=None,
            worker_last_seen_at=None,
            worker_status=None,
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-job-stage="waiting_for_converter"' in resp.text
    assert "none connected yet" in resp.text


async def test_stage_waiting_for_gpu_is_a_separate_sentence_from_waiting_for_a_converter(
    client, queries_stub
):
    """D-113: a fresh check-in reporting waiting_for_gpu is not the
    waiting-for-a-converter sentence -- the converter is up and healthy,
    it is the shared card that is busy.
    """
    from datetime import datetime, timezone

    title_id = uuid.uuid4()
    fresh_seen = datetime.now(timezone.utc)
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at=None,
            **_job(job_kind="transcode", job_status="queued", job_db_stage="queued"),
            worker_name="ml-server",
            worker_last_seen_at=fresh_seen,
            worker_status="waiting_for_gpu",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-job-stage="waiting_for_gpu"' in resp.text
    assert "Waiting for GPU memory" in resp.text
    assert "Waiting for a converter" not in resp.text


async def test_stage_working_carries_percent_and_remaining(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(
                job_kind="transcode",
                job_status="running",
                job_db_stage="working",
                job_progress_pct=55.0,
                job_eta_seconds=120,
            ),
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' in resp.text
    assert 'data-job-stage="working"' in resp.text
    assert "55%" in resp.text


async def test_stage_working_with_no_estimate_carries_percent_alone(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(
                job_kind="transcode",
                job_status="running",
                job_db_stage="working",
                job_progress_pct=42.0,
                job_eta_seconds=None,
            ),
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-job-stage="working"' in resp.text
    assert "42%" in resp.text
    assert "about" not in resp.text
    assert "left" not in resp.text


async def test_stage_working_at_the_very_end_still_carries_a_remaining_time(client, queries_stub):
    """Zero is a real estimate, not a missing one. The converter reports it
    for a progress block whose output timestamp has already passed the
    source duration, and the row must say so rather than fall back to the
    form that carries the percentage alone.
    """
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(
                job_kind="transcode",
                job_status="running",
                job_db_stage="working",
                job_progress_pct=99.0,
                job_eta_seconds=0,
            ),
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-job-stage="working"' in resp.text
    assert "99%" in resp.text
    # Jinja autoescapes the literal "<" in the catalog value -- "&lt;1 min"
    # on the wire, "<1 min" once a browser parses it.
    assert "&lt;1 min" in resp.text


def test_humanize_seconds_reads_in_the_language_of_the_page():
    """The builder's own exact-string assertions, in both languages,
    against the real catalogs -- the phrase every chip and every estimate
    line reads is built here, and every caller only ever threads a
    catalog lookup down to this one function.
    """
    from orchestrator.web.templates import humanize_seconds, translator

    en, ru = translator("en"), translator("ru")
    assert humanize_seconds(180, en) == "3 min"
    assert humanize_seconds(180, ru) == "3 мин"
    assert humanize_seconds(7200, en) == "2.0 h"
    assert humanize_seconds(7200, ru) == "2.0 ч"
    assert humanize_seconds(30, en) == "<1 min"
    assert humanize_seconds(30, ru) == "<1 мин"


def test_no_duration_key_carries_a_hedge_word_of_its_own():
    """The doubling this task fixes ("about under a minute") came from
    the sub-minute branch carrying a hedge of its own on top of the one
    every surrounding sentence already has. Scans the three duration keys
    directly -- the actual surface of the fix -- rather than every
    catalog value, since the four sentences that interpolate a duration
    are asserted byte-identical elsewhere and were never the problem.
    """
    import json

    en = json.loads((REPO_ROOT / "orchestrator" / "locales" / "en.json").read_text(encoding="utf-8"))
    ru = json.loads((REPO_ROOT / "orchestrator" / "locales" / "ru.json").read_text(encoding="utf-8"))
    for catalog, hedge in ((en, "about"), (ru, "около")):
        for key in ("duration.under_a_minute", "duration.minutes", "duration.hours"):
            assert hedge not in catalog[key]


async def test_working_chip_on_a_ru_page_has_no_latin_letter(client, queries_stub):
    """A working chip's remaining-time phrase used to be built in English
    with no idea what language the page is in -- the Russian sentence
    would read "...около 3 minutes". Every word in the chip now comes
    from the catalog of the page's own language.
    """
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(
                job_kind="transcode",
                job_status="running",
                job_db_stage="working",
                job_progress_pct=42.0,
                job_eta_seconds=180,
            ),
        )
    ]

    async with client:
        client.cookies.set("mytube_lang", "ru")
        resp = await client.get("/library")

    chip = re.search(r'<span class="chip chip--preparing">([^<]*)</span>', resp.text)
    assert chip is not None
    assert not re.search(r"[A-Za-z]", chip.group(1))


async def test_stage_uploading_carries_percent(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(
                job_kind="remux",
                job_status="running",
                job_db_stage="uploading",
                job_progress_pct=90.0,
            ),
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' in resp.text
    assert 'data-job-stage="uploading"' in resp.text
    assert "90%" in resp.text


async def test_stage_cancelling_does_not_carry_the_countdown_class(client, queries_stub):
    """The cancelling chip has its own class rather than reusing
    chip--deleting: that class is selected by name by the countdown
    ticker, and a cancelling row picked up by that selector would start
    displaying a clock it has no deadline for.
    """
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(
            title_id=title_id,
            title_status="preparing",
            started_at="2026-08-22T00:00:00Z",
            **_job(
                job_kind="transcode",
                job_status="running",
                job_db_stage="working",
                job_cancel_requested=True,
            ),
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' in resp.text
    assert 'data-job-stage="cancelling"' in resp.text
    assert "chip--cancelling" in resp.text
    row_markup = resp.text.split("<li ")[1]
    assert "chip--deleting" not in row_markup
    # No Cancel control on an already-cancelling row -- there is nothing
    # left to confirm.
    assert f"/library/{title_id}/cancel" not in resp.text


# --- Converted-edition detail and the stereo badge (04-11, D-146/D-139) --


async def test_converted_detail_line_appears_only_for_a_conversion(client, queries_stub):
    queries_stub.rows = [
        _row(
            title_name="As-is Clip",
            source_codec=None,
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert "Converted from" not in resp.text


async def test_converted_detail_line_names_source_codec_and_bitrate(client, queries_stub):
    queries_stub.rows = [
        _row(
            title_name="Converted Clip",
            size_bytes=750_000_000,
            duration_seconds=1000.0,
            source_codec="hevc",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert "Converted from hevc" in resp.text
    assert "/s" in resp.text


async def test_converted_detail_line_names_the_burned_subtitle_only_when_one_was_burned(
    client, queries_stub
):
    queries_stub.rows = [
        _row(
            title_name="Subtitled Clip",
            size_bytes=750_000_000,
            duration_seconds=1000.0,
            source_codec="hevc",
            subtitle_burned_language="eng",
            subtitle_burned_title="English",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert "Subtitles burned: eng (English)" in resp.text


async def test_converted_detail_line_names_an_external_burn_with_no_language(
    client, queries_stub
):
    """An external subtitle file carries no language tag at all -- the
    line still has to say something was burned, in the same unknown-
    language wording the audio line already uses, rather than staying
    gated on a field an external burn can never carry.
    """
    queries_stub.rows = [
        _row(
            title_name="External Burn Clip",
            size_bytes=750_000_000,
            duration_seconds=1000.0,
            source_codec="hevc",
            subtitle_burned_language=None,
            subtitle_burned_title="movie.en.srt",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert "Subtitles burned: Unknown language (movie.en.srt)" in resp.text


async def test_stereo_badge_appears_for_a_non_flat_layout_on_an_as_is_edition_half_sbs(
    client, queries_stub
):
    """The stereo badge belongs to the layout, not to the conversion --
    an as-is edition with a half side-by-side layout carries it too
    (D-139), since a file prepared in the dedicated stereo tool arrives
    already compatible and is added as it is.
    """
    queries_stub.rows = [
        _row(
            title_name="As-is Stereo Clip",
            source_codec=None,
            stereo_layout="sbs_half",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert "chip--3d" in resp.text
    assert "3D SBS" in resp.text


async def test_stereo_badge_appears_for_a_non_flat_layout_on_a_converted_edition(
    client, queries_stub
):
    queries_stub.rows = [
        _row(
            title_name="Converted Stereo Clip",
            size_bytes=750_000_000,
            duration_seconds=1000.0,
            source_codec="hevc",
            stereo_layout="ou_full",
        )
    ]

    async with client:
        resp = await client.get("/library")

    assert "chip--3d" in resp.text
    assert "3D OU" in resp.text


async def test_no_stereo_badge_for_a_flat_layout(client, queries_stub):
    queries_stub.rows = [_row(title_name="Flat Clip", stereo_layout="2d")]

    async with client:
        resp = await client.get("/library")

    assert "chip--3d" not in resp.text


async def test_failed_row_has_reason_prefix_no_copy_no_thumbnail(client, queries_stub):
    queries_stub.rows = [
        _row(title_status="failed", failure_reason="failed.reason.upload")
    ]

    async with client:
        resp = await client.get("/library")

    assert "Adding failed" in resp.text
    assert "data-copy-text" not in resp.text
    assert "library-row__thumb" not in resp.text
    assert "Dismiss" in resp.text


async def test_dismiss_deletes_objects_before_rows_and_frees_slug(client, queries_stub, storage_stub):
    title_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    queries_stub.failed_keys_by_id[title_id] = [f"{edition_id}.mp4", f"{edition_id}.jpg"]

    async with client:
        resp = await client.post(f"/library/{title_id}/dismiss")

    assert resp.status_code in (200, 302)
    assert set(storage_stub.deleted) == {f"{edition_id}.mp4", f"{edition_id}.jpg"}
    assert queries_stub.dismiss_calls == [title_id]


async def test_dismiss_refuses_an_active_title(client, queries_stub, monkeypatch):
    title_id = uuid.uuid4()

    async def _refuse(conn, tid):
        raise ValueError("not failed")

    monkeypatch.setattr(queries, "failed_title_object_keys", _refuse)

    async with client:
        resp = await client.post(f"/library/{title_id}/dismiss")

    assert resp.status_code == 400
    assert queries_stub.dismiss_calls == []


# --- Cancel and retry: every kind of work, one lifecycle (04-11) --------


async def test_cancel_on_a_queued_job_deletes_objects_before_the_row(client, monkeypatch):
    title_id = uuid.uuid4()
    calls = []

    async def fake_job_for_title(conn, tid):
        return {"id": "job-1"}

    async def fake_request_cancel(conn, job_id):
        return {"status": "cancelled"}

    async def fake_never_active_keys(conn, tid):
        calls.append("keys")
        return ["a.mp4", "a.jpg"]

    async def fake_delete_never_active(conn, tid):
        calls.append("delete_rows")

    def spy_delete_object(key):
        calls.append(f"object:{key}")

    monkeypatch.setattr(queries, "job_for_title", fake_job_for_title, raising=False)
    monkeypatch.setattr(queries, "request_job_cancel", fake_request_cancel, raising=False)
    monkeypatch.setattr(
        queries, "never_active_title_object_keys", fake_never_active_keys, raising=False
    )
    monkeypatch.setattr(
        queries, "delete_never_active_title", fake_delete_never_active, raising=False
    )
    monkeypatch.setattr(routes_titles, "delete_object", spy_delete_object)

    async with client:
        resp = await client.post(f"/library/{title_id}/cancel", follow_redirects=False)

    assert resp.status_code == 302
    assert calls == ["keys", "object:a.mp4", "object:a.jpg", "delete_rows"]


async def test_cancel_on_a_running_job_only_sets_the_flag_no_hard_delete(client, monkeypatch):
    title_id = uuid.uuid4()
    delete_calls = []

    async def fake_job_for_title(conn, tid):
        return {"id": "job-1"}

    async def fake_request_cancel(conn, job_id):
        return {"status": "running", "cancel_requested": True}

    async def fake_delete_never_active(conn, tid):
        delete_calls.append(tid)

    monkeypatch.setattr(queries, "job_for_title", fake_job_for_title, raising=False)
    monkeypatch.setattr(queries, "request_job_cancel", fake_request_cancel, raising=False)
    monkeypatch.setattr(
        queries, "delete_never_active_title", fake_delete_never_active, raising=False
    )

    async with client:
        resp = await client.post(f"/library/{title_id}/cancel", follow_redirects=False)

    assert resp.status_code == 302
    assert delete_calls == []


async def test_cancel_against_a_title_with_no_live_job_is_refused(client, monkeypatch):
    async def fake_job_for_title(conn, tid):
        return None

    monkeypatch.setattr(queries, "job_for_title", fake_job_for_title, raising=False)

    async with client:
        resp = await client.post(f"/library/{uuid.uuid4()}/cancel", follow_redirects=False)

    assert resp.status_code == 404


@pytest.mark.integration
async def test_cancel_never_active_delete_refuses_a_title_that_has_ever_been_active(raw_conn):
    """T-04-32's own guard, proven against real Postgres: a title that
    ever went active can never be hard-deleted through this door, no
    matter how it is reached -- only a title whose address never went
    live (still 'preparing', or 'failed') may be erased outright.
    """
    title_id, _, _ = await _create_title(raw_conn, "Once Active", "once-active-slug")

    with pytest.raises(ValueError):
        await queries.delete_never_active_title(raw_conn, title_id)

    row = await queries.resolve_slug(raw_conn, "once-active-slug")
    assert row is not None and row["status"] == "active"


async def test_retry_queues_a_fresh_job_with_the_same_payload_as_the_failed_one(
    client, queries_stub, monkeypatch
):
    title_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    queries_stub.row_by_id[title_id] = _row(
        title_id=title_id, title_status="failed", edition_id=edition_id
    )
    failed_job = {
        "kind": "transcode",
        "edition_id": edition_id,
        "source_path": "clip.mkv",
        "audio_stream_index": 1,
        "subtitle_kind": "embedded",
        "subtitle_stream_index": 2,
        "subtitle_path": None,
        "stereo_layout": "sbs_full",
    }
    enqueued = []
    requeued = []

    async def fake_last_job(conn, tid):
        return failed_job

    async def fake_enqueue(conn, **kwargs):
        enqueued.append(kwargs)
        return uuid.uuid4()

    async def fake_requeue_title(conn, tid):
        requeued.append(tid)

    monkeypatch.setattr(queries, "last_job_for_title", fake_last_job, raising=False)
    monkeypatch.setattr(queries, "enqueue_job", fake_enqueue, raising=False)
    monkeypatch.setattr(queries, "requeue_title", fake_requeue_title, raising=False)

    async with client:
        resp = await client.post(f"/library/{title_id}/retry", follow_redirects=False)

    assert resp.status_code == 302
    assert enqueued == [
        {
            "kind": "transcode",
            "title_id": title_id,
            "edition_id": edition_id,
            "source_path": "clip.mkv",
            "audio_stream_index": 1,
            "subtitle_kind": "embedded",
            "subtitle_stream_index": 2,
            "subtitle_path": None,
            "stereo_layout": "sbs_full",
        }
    ]
    assert requeued == [title_id]


async def test_retry_refuses_a_title_that_is_not_failed(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.row_by_id[title_id] = _row(title_id=title_id, title_status="active")

    async with client:
        resp = await client.post(f"/library/{title_id}/retry", follow_redirects=False)

    assert resp.status_code == 404


async def test_failed_row_has_retry_before_dismiss(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [
        _row(title_id=title_id, title_status="failed", failure_reason="failed.reason.upload")
    ]

    async with client:
        resp = await client.get("/library")

    retry_index = resp.text.index(f"/library/{title_id}/retry")
    dismiss_index = resp.text.index(f"/library/{title_id}/dismiss")
    assert retry_index < dismiss_index


# --- No client library, no vendored/CDN code -----------------------------


def test_app_js_has_a_poll_timer_and_stays_short():
    text = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "setTimeout" in text
    # Code lines only. What this guard is watching for is a client library
    # creeping in, and a vendored one passes far more than this ceiling on
    # its own -- a comment explaining why a fallback exists is not the
    # thing to ration, and counting it pushes the next person toward
    # deleting the explanation rather than the complexity. Raised from 80
    # once hand-written drag-and-drop reordering (D-107) landed alongside
    # the poller, the countdown and the copy handler; raised again from
    # 140 once the on-demand preview's two delegated handlers (04-UI-
    # SPEC.md States -> Preview) landed alongside them; raised again from
    # 180 once the preview-disabled gate, the manual audio/subtitle path
    # join and the sidecar/audio picker's own open/choose handlers (T-05-14)
    # landed alongside them; raised again from 260 once the series row's
    # disclosure-state-and-focus survival (05-UI-SPEC.md Screen D item 3)
    # landed -- the poller itself gained three lines, the rest is the two
    # small helpers those lines call -- still well under a vendored drag-
    # and-drop or lightbox library's own line count, so the guard still
    # catches what it was built to catch.
    code = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]
    assert len(code) < 300


def test_no_htmx_or_cdn_references():
    # Case-sensitive, matching the plan's own grep criterion exactly --
    # the module docstrings say "no HTMX" (capitalized, documentation
    # prose) on purpose; the lowercase token is the one that must never
    # appear as a vendored/CDN reference.
    targets = [REPO_ROOT / "static" / "app.js"]
    targets += list((REPO_ROOT / "orchestrator" / "web" / "templates").rglob("*.html"))
    for target in targets:
        text = target.read_text(encoding="utf-8")
        assert "htmx" not in text
        assert "unpkg" not in text
        assert "jsdelivr" not in text


# --- Plan 06 Task 1/2: row actions and the delete countdown markup ------


async def test_active_row_has_rename_delete_and_check_actions(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, slug="active-actions-clip")]

    async with client:
        resp = await client.get("/library")

    assert f"/library/{title_id}/rename" in resp.text
    assert f"/library/{title_id}/delete" in resp.text
    assert "/check/active-actions-clip" in resp.text


async def test_pending_delete_row_shows_countdown_chip_and_future_deadline(client, queries_stub):
    from datetime import datetime, timedelta, timezone

    deadline = datetime.now(timezone.utc) + timedelta(seconds=200)
    queries_stub.rows = [_row(title_status="pending_delete", delete_at=deadline)]

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="pending_delete"' in resp.text
    assert "chip--deleting" in resp.text
    match = re.search(r'data-deadline="(\d+)"', resp.text)
    assert match is not None
    assert int(match.group(1)) > time.time() * 1000


async def test_pending_delete_row_has_undo_and_purge_controls_with_native_confirm(client, queries_stub):
    from datetime import datetime, timedelta, timezone

    title_id = uuid.uuid4()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=200)
    queries_stub.rows = [_row(title_id=title_id, title_status="pending_delete", delete_at=deadline)]

    async with client:
        resp = await client.get("/library")

    assert f"/library/{title_id}/undo" in resp.text
    assert f"/library/{title_id}/purge" in resp.text
    assert "data-confirm=" in resp.text
    assert "Rename" not in resp.text
    assert "Delete now" in resp.text


# --- Plan 06 Task 1/2: rename, delete, undo, purge -- real Postgres -----
# These need the shared slug planner and the mark_for_delete/cancel_delete/
# reap_once query layer to behave for real, so they run against a real,
# migrated Postgres (migrated_db_url/raw_conn from conftest.py) rather than
# the monkeypatched queries stub above -- the same split test_add_flow.py
# and test_deletion.py already draw between route-shape tests and
# behavior tests.


class _SingleActionAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class _SingleActionConnPool:
    """Wraps one already-open, really-committing connection behind the
    .acquire() interface the routes expect from an asyncpg.Pool -- the
    same shape test_deletion.py's _SinglePoolLike uses, so rename/delete/
    undo/purge can be exercised end to end without a real pool.
    """

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _SingleActionAcquireCtx(self._conn)


@pytest.fixture
def _action_env(migrated_db_url, monkeypatch):
    monkeypatch.setenv("MYTUBE_DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def action_client(_action_env, raw_conn, signed_in):
    app.state.db_pool = _SingleActionConnPool(raw_conn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as ac:
        yield ac


async def _create_title(conn, name: str, slug: str):
    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    title_id = await queries.create_title_with_edition(conn, name, slug, edition_id, object_key)
    return title_id, edition_id, object_key


@pytest.mark.integration
async def test_rename_keeps_old_address_resolving_and_new_one_works(action_client, raw_conn):
    title_id, _, _ = await _create_title(raw_conn, "Rename Source", "rename-source-slug")

    resp = await action_client.post(f"/library/{title_id}/rename", data={"name": "Rename Target"})

    assert resp.status_code == 200
    assert "rename-source-slug" in resp.text
    assert "rename-target" in resp.text

    old = await queries.resolve_slug(raw_conn, "rename-source-slug")
    new = await queries.resolve_slug(raw_conn, "rename-target")
    assert old is not None and old["status"] == "active"
    assert new is not None and new["status"] == "active"
    assert old["title_id"] == new["title_id"] == title_id

    old_v_resp = await action_client.get("/v/rename-source-slug", follow_redirects=False)
    new_v_resp = await action_client.get("/v/rename-target", follow_redirects=False)
    assert old_v_resp.status_code == 302
    assert new_v_resp.status_code == 302


@pytest.mark.integration
async def test_rename_onto_taken_slug_gets_suffixed_and_banner_names_it(action_client, raw_conn):
    await _create_title(raw_conn, "Rename Collision Target", "rename-collision-target")
    title_id, _, _ = await _create_title(raw_conn, "Rename Collision Source", "rename-collision-source")

    resp = await action_client.post(
        f"/library/{title_id}/rename", data={"name": "Rename Collision Target"}
    )

    assert resp.status_code == 200
    assert "rename-collision-target-2" in resp.text

    resolved = await queries.resolve_slug(raw_conn, "rename-collision-target-2")
    assert resolved["title_id"] == title_id


@pytest.mark.integration
async def test_rename_similar_slug_shows_warning_in_response(action_client, raw_conn):
    await _create_title(raw_conn, "RenameSimilarWarnCase", "renamesimilarwarncase")
    title_id, _, _ = await _create_title(raw_conn, "Rename Similar Source", "rename-similar-source")

    resp = await action_client.post(
        f"/library/{title_id}/rename", data={"name": "Rename:Similar:Warn:Case"}
    )

    assert resp.status_code == 200
    assert "A similar address is already taken" in resp.text
    assert "renamesimilarwarncase" in resp.text


@pytest.mark.integration
async def test_delete_marks_pending_and_row_carries_a_future_deadline(action_client, raw_conn):
    title_id, edition_id, _ = await _create_title(raw_conn, "Delete Wording", "delete-wording-slug")
    # The row's technical-detail line has to survive a real render -- give
    # this edition the fields the add worker always sets before flipping a
    # title to 'active' (create_title_with_edition alone, the CLI's own
    # path, never does; that gap is pre-existing and out of this plan's
    # scope, but the row this test renders needs to look like a real one).
    await queries.set_edition_technical(
        raw_conn,
        edition_id,
        size_bytes=123_456_789,
        duration_seconds=90.0,
        width=1280,
        height=720,
        audio_language="eng",
        audio_title=None,
        audio_reencoded=False,
    )

    resp = await action_client.post(f"/library/{title_id}/delete")
    assert resp.status_code == 302

    v_resp = await action_client.get("/v/delete-wording-slug", follow_redirects=False)
    assert v_resp.status_code == 410

    row_resp = await action_client.get(f"/library/{title_id}/row")
    assert row_resp.status_code == 200
    match = re.search(r'data-deadline="(\d+)"', row_resp.text)
    assert match is not None
    assert int(match.group(1)) > time.time() * 1000


@pytest.mark.integration
async def test_undo_restores_active_and_v_redirects_again(action_client, raw_conn):
    title_id, _, _ = await _create_title(raw_conn, "Undo Target", "undo-target-slug")
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=300)

    resp = await action_client.post(f"/library/{title_id}/undo")
    assert resp.status_code == 302

    row = await queries.resolve_slug(raw_conn, "undo-target-slug")
    assert row["status"] == "active"

    v_resp = await action_client.get("/v/undo-target-slug", follow_redirects=False)
    assert v_resp.status_code == 302


@pytest.mark.integration
async def test_undo_refuses_a_title_not_pending_deletion(action_client, raw_conn):
    title_id, _, _ = await _create_title(raw_conn, "Undo Not Pending", "undo-not-pending-slug")

    resp = await action_client.post(f"/library/{title_id}/undo")

    assert resp.status_code == 400
    row = await queries.resolve_slug(raw_conn, "undo-not-pending-slug")
    assert row["status"] == "active"


@pytest.mark.integration
async def test_purge_deletes_objects_and_marks_deleted_before_response_returns(
    action_client, raw_conn, monkeypatch
):
    from orchestrator import reaper as reaper_module

    deleted = []
    monkeypatch.setattr(reaper_module, "delete_object", lambda key: deleted.append(key))

    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    poster_key = f"{edition_id}.jpg"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Purge Target", "purge-target-slug", edition_id, object_key
    )
    await queries.set_edition_poster(raw_conn, edition_id, poster_key)
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=300)

    resp = await action_client.post(f"/library/{title_id}/purge")

    assert resp.status_code == 302
    assert object_key in deleted
    assert poster_key in deleted

    row = await queries.resolve_slug(raw_conn, "purge-target-slug")
    assert row["status"] == "deleted"


@pytest.mark.integration
async def test_purge_refuses_a_title_that_is_not_pending_deletion(action_client, raw_conn):
    title_id, _, _ = await _create_title(raw_conn, "Purge Not Pending", "purge-not-pending-slug")

    resp = await action_client.post(f"/library/{title_id}/purge")

    assert resp.status_code == 400
    row = await queries.resolve_slug(raw_conn, "purge-not-pending-slug")
    assert row["status"] == "active"


def test_routes_titles_never_writes_slug_history_directly():
    text = (REPO_ROOT / "orchestrator" / "web" / "routes_titles.py").read_text(encoding="utf-8")
    assert text.count("INSERT INTO slug_history") == 0
    assert text.count("reap_once") >= 1


async def test_the_storage_calls_run_off_the_event_loop_thread(
    client, queries_stub, storage_stub, monkeypatch
):
    """The bucket listing behind the usage figure and the poster GET share
    an event loop with `GET /v/{slug}` and `/healthz`. Run bare, a cold
    enumeration stops both for its whole duration -- and the library page
    is the screen the operator leaves open.

    Asserted on the thread rather than on elapsed time: a timing test of a
    blocked event loop cannot measure the block from inside the same loop.
    """
    import threading

    loop_thread = threading.get_ident()
    ran_on = {}

    def recording_usage():
        ran_on["usage"] = threading.get_ident()
        return storage_stub.usage

    edition_id = uuid.uuid4()

    def recording_get(key):
        ran_on["poster"] = threading.get_ident()
        return b"jpeg-bytes"

    monkeypatch.setattr(routes_titles, "storage_usage", recording_usage)
    monkeypatch.setattr(routes_titles, "get_object_bytes", recording_get)

    async with client:
        library = await client.get("/library")
        poster = await client.get(f"/poster/{edition_id}")

    assert library.status_code == 200
    assert poster.status_code == 200
    assert ran_on["usage"] != loop_thread
    assert ran_on["poster"] != loop_thread


# --- Cross-origin write refusal ------------------------------------------


async def test_a_destructive_post_from_another_site_is_refused(client, queries_stub, storage_stub):
    """A page open in another tab can cross-origin-POST these routes with a
    plain form -- no CORS permission is needed to *send* one, and these
    routes matter for their side effects, not their bodies.
    """
    title_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    queries_stub.failed_keys_by_id[title_id] = [f"{edition_id}.mp4"]

    async with client:
        resp = await client.post(
            f"/library/{title_id}/dismiss",
            headers={"Origin": "http://evil.example"},
        )

    assert resp.status_code == 403
    assert storage_stub.deleted == []
    assert queries_stub.dismiss_calls == []


async def test_the_interfaces_own_forms_still_post(client, queries_stub, storage_stub):
    title_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    queries_stub.failed_keys_by_id[title_id] = [f"{edition_id}.mp4"]

    async with client:
        resp = await client.post(
            f"/library/{title_id}/dismiss",
            headers={"Origin": "http://testserver"},
        )

    assert resp.status_code in (200, 302)
    assert queries_stub.dismiss_calls == [title_id]


async def test_a_request_with_no_origin_is_allowed(client, queries_stub, storage_stub):
    """curl and the CLI send no Origin, and anything with network access to
    this port can already act by the same decision that leaves it
    unauthenticated -- refusing them would break tooling without closing
    anything a browser can do.
    """
    title_id = uuid.uuid4()
    edition_id = uuid.uuid4()
    queries_stub.failed_keys_by_id[title_id] = [f"{edition_id}.mp4"]

    async with client:
        resp = await client.post(f"/library/{title_id}/dismiss")

    assert resp.status_code in (200, 302)
    assert queries_stub.dismiss_calls == [title_id]


async def test_a_read_from_another_site_is_still_served(client, queries_stub):
    async with client:
        resp = await client.get("/library", headers={"Origin": "http://evil.example"})

    assert resp.status_code == 200


# --- Plan 09 Task 1: the whole hierarchy in one round trip ----------------


async def test_a_one_episode_title_film_row_context_is_unchanged(client, queries_stub):
    """D-162: a film (episode_count defaults to 1 via derive_series_ui,
    the same as a stub row that never sets it) renders exactly today's
    row -- no series class, no disclosure -- even though library_rows now
    always returns the new aggregate columns.
    """
    queries_stub.rows = [_row(episode_count=1, ready_count=1, failed_count=0)]

    async with client:
        resp = await client.get("/library")

    assert "library-row--series" not in resp.text
    assert "<details" not in resp.text
    # Called once regardless -- with an empty title id list, since no row
    # here is a series -- never skipped outright, matching the "two
    # queries per screen" arithmetic for every screen, not only one with
    # a series on it.
    assert queries_stub.episodes_for_library_calls == 1
    assert queries_stub.episodes_for_library_title_ids == []


async def test_library_route_query_count_does_not_scale_with_episode_count(client, queries_stub):
    """However many series a screen holds, and however many episodes each
    one has, GET /library calls queries.episodes_for_library exactly
    once -- never once per title and never once per episode.
    """
    for _ in range(3):
        title_id = uuid.uuid4()
        queries_stub.rows.append(_row(title_id=title_id, episode_count=5, ready_count=5))
        queries_stub.episodes_by_title[title_id] = [
            _episode(title_id=title_id, number=n) for n in range(1, 6)
        ]

    async with client:
        resp = await client.get("/library")

    assert resp.status_code == 200
    assert queries_stub.episodes_for_library_calls == 1
    assert len(queries_stub.episodes_for_library_title_ids) == 3


async def test_episode_size_and_duration_round_trip_into_the_episode_row(client, queries_stub):
    """The technical detail `episodes_for_library` reads per episode
    (size, duration) survives all the way to the rendered row -- the
    facts D-163 moves down from the title row.
    """
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1, size_bytes=1_073_741_824, duration_seconds=125),
        _episode(title_id=title_id, number=2, size_bytes=1_073_741_824, duration_seconds=125),
    ]

    async with client:
        resp = await client.get("/library")

    assert resp.text.count("1.0 GB") == 2
    assert resp.text.count("2:05") == 2


async def test_aggregate_chip_shows_ready_of_total_and_current_episode(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", episode_count=12, ready_count=3,
        job_episode_number=4, **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=57.0),
    )]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=n) for n in range(1, 13)
    ]

    async with client:
        resp = await client.get("/library")

    assert "3 of 12 ready, episode 4" in resp.text


async def test_aggregate_chip_absent_once_settled(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=4, ready_count=4)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=n) for n in range(1, 5)
    ]

    async with client:
        resp = await client.get("/library")

    assert "ready, episode" not in resp.text


async def test_aggregate_sentence_names_the_in_flight_episode_number(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", episode_count=12, ready_count=4,
        job_episode_number=5, **_job(job_kind="transcode", job_db_stage="queued"),
    )]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=n) for n in range(1, 13)
    ]

    async with client:
        resp = await client.get("/library")

    assert "4 of 12 ready, episode 5" in resp.text


# --- Plan 09 Task 2: the row that opens ------------------------------------


async def test_disclosure_renders_with_the_right_episode_count_when_settled(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=3, ready_count=3)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=n) for n in (1, 2, 3)
    ]

    async with client:
        resp = await client.get("/library")

    assert "<details" in resp.text
    assert "3 episodes" in resp.text
    assert f'data-episode-id="{title_id}"' in resp.text


async def test_disclosure_is_closed_by_default_when_everything_is_settled(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2),
    ]

    async with client:
        resp = await client.get("/library")

    details_tag = re.search(r"<details[^>]*>", resp.text).group(0)
    assert " open" not in details_tag


async def test_disclosure_is_open_by_default_when_an_episode_is_in_flight(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", episode_count=2, ready_count=1,
        job_episode_number=2, **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=10.0),
    )]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(
            title_id=title_id, number=2, edition_status="preparing",
            **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=10.0),
        ),
    ]

    async with client:
        resp = await client.get("/library")

    details_tag = re.search(r"<details[^>]*>", resp.text).group(0)
    assert " open" in details_tag


async def test_disclosure_is_open_by_default_when_an_episode_has_failed(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=1, failed_count=1)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2, edition_status="failed", edition_failure_reason="failed.reason.upload"),
    ]

    async with client:
        resp = await client.get("/library")

    details_tag = re.search(r"<details[^>]*>", resp.text).group(0)
    assert " open" in details_tag


async def test_disclosure_summary_shows_failed_count_and_chip(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=1, failed_count=1)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2, edition_status="failed"),
    ]

    async with client:
        resp = await client.get("/library")

    assert "2 episodes, 1 failed" in resp.text
    assert "chip--refused" in resp.text


async def test_two_chips_render_together_while_the_title_is_already_active(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", slug="my-season",
        episode_count=2, ready_count=1, job_episode_number=2,
        **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=57.0, job_eta_seconds=200),
    )]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(
            title_id=title_id, number=2, edition_status="preparing",
            **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=57.0, job_eta_seconds=200),
        ),
    ]

    async with client:
        resp = await client.get("/library")

    assert "https://mytube.example/v/my-season" in resp.text
    assert "1 of 2 ready, episode 2" in resp.text
    assert "57%" in resp.text


async def test_episode_row_shows_its_own_address_copy_button_chip_size_and_duration(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", slug="my-show", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1, size_bytes=1_073_741_824, duration_seconds=125),
        _episode(title_id=title_id, number=2, size_bytes=1_073_741_824, duration_seconds=125),
    ]

    async with client:
        resp = await client.get("/library")

    assert "https://mytube.example/v/my-show/1" in resp.text
    assert "https://mytube.example/v/my-show/2" in resp.text
    assert "data-copy-text=\"https://mytube.example/v/my-show/1\"" in resp.text
    assert resp.text.count("1.0 GB") == 2


async def test_episode_row_carries_its_own_data_episode_id_attribute(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1)
    ep2 = _episode(title_id=title_id, number=2)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]

    async with client:
        resp = await client.get("/library")

    assert f'data-episode-id="{ep1["episode_id"]}"' in resp.text
    assert f'data-episode-id="{ep2["episode_id"]}"' in resp.text


async def test_episode_row_in_flight_collapses_actions_to_cancel_only(client, queries_stub):
    title_id = uuid.uuid4()
    ep2 = _episode(
        title_id=title_id, number=2, edition_status="preparing",
        **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=10.0),
    )
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", episode_count=2, ready_count=1,
        job_episode_number=2, **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=10.0),
    )]
    queries_stub.episodes_by_title[title_id] = [_episode(title_id=title_id, number=1), ep2]

    async with client:
        resp = await client.get("/library")

    section = resp.text.split(f'data-episode-id="{ep2["episode_id"]}"')[1].split("</li>")[0]
    assert 'data-control="cancel"' in section
    assert 'data-control="check"' not in section


async def test_episode_row_queued_chip_carries_no_number(client, queries_stub):
    """D-188 gap closure, 2026-09-09: a season's queue position belongs
    on the title row once, not repeated on every queued episode of it --
    05-UI-SPEC.md Screen D item 4.
    """
    title_id = uuid.uuid4()
    ep2 = _episode(
        title_id=title_id,
        number=2,
        **_job(job_kind="remux", job_status="queued", job_db_stage="queued"),
    )
    queries_stub.rows = [
        _row(title_id=title_id, title_status="active", episode_count=2, ready_count=1)
    ]
    queries_stub.episodes_by_title[title_id] = [_episode(title_id=title_id, number=1), ep2]

    async with client:
        resp = await client.get("/library")

    section = re.search(
        rf'<li class="episode-row[^>]*data-episode-id="{ep2["episode_id"]}"[^>]*>.*?</li>',
        resp.text,
        re.S,
    ).group(0)
    chip = re.search(r'<span class="chip chip--preparing">([^<]*)</span>', section)
    assert chip is not None
    assert chip.group(1).strip() == "Queued"
    assert not re.search(r"\d", chip.group(1))


async def test_episode_row_failed_variant_and_reason_line(client, queries_stub):
    title_id = uuid.uuid4()
    ep2 = _episode(title_id=title_id, number=2, edition_status="failed", edition_failure_reason="failed.reason.upload")
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=1, failed_count=1)]
    queries_stub.episodes_by_title[title_id] = [_episode(title_id=title_id, number=1), ep2]

    async with client:
        resp = await client.get("/library")

    section = re.search(
        rf'<li class="episode-row[^>]*data-episode-id="{ep2["episode_id"]}"[^>]*>.*?</li>',
        resp.text,
        re.S,
    ).group(0)
    assert "episode-row--failed" in section
    assert "did not finish" in section


async def test_episode_row_ready_and_settled_shows_check_link(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", slug="settled-show", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2),
    ]

    async with client:
        resp = await client.get("/library")

    assert 'href="/check/settled-show"' in resp.text
    assert 'data-control="check"' in resp.text


async def test_episode_row_ready_and_settled_check_link_is_episode_scoped(client, queries_stub):
    """Plan 10: an episode's own Check link points at its own numbered
    check page, not the title's bare one -- distinct from the title row's
    own Check action asserted above, which still exists and still points
    at the bare address.
    """
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", slug="settled-show-2", episode_count=2, ready_count=2)]
    ep1 = _episode(title_id=title_id, number=1)
    ep2 = _episode(title_id=title_id, number=2)
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]

    async with client:
        resp = await client.get("/library")

    section1 = resp.text.split(f'data-episode-id="{ep1["episode_id"]}"')[1].split("</li>")[0]
    section2 = resp.text.split(f'data-episode-id="{ep2["episode_id"]}"')[1].split("</li>")[0]
    assert 'href="/check/settled-show-2/1"' in section1
    assert 'href="/check/settled-show-2/2"' in section2


async def test_episode_row_ready_and_settled_shows_delete_episode_button(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", slug="deletable-show", episode_count=2, ready_count=2)]
    ep1 = _episode(title_id=title_id, number=1)
    queries_stub.episodes_by_title[title_id] = [ep1, _episode(title_id=title_id, number=2)]

    async with client:
        resp = await client.get("/library")

    section = resp.text.split(f'data-episode-id="{ep1["episode_id"]}"')[1].split("</li>")[0]
    assert f'action="/library/episode/{ep1["episode_id"]}/delete"' in section
    assert 'data-control="delete"' in section


async def test_episode_row_failed_shows_retry_and_dismiss(client, queries_stub):
    title_id = uuid.uuid4()
    ep2 = _episode(title_id=title_id, number=2, edition_status="failed", edition_failure_reason="failed.reason.upload")
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=1, failed_count=1)]
    queries_stub.episodes_by_title[title_id] = [_episode(title_id=title_id, number=1), ep2]

    async with client:
        resp = await client.get("/library")

    section = resp.text.split(f'data-episode-id="{ep2["episode_id"]}"')[1].split("</li>")[0]
    assert f'action="/library/episode/{ep2["episode_id"]}/retry"' in section
    assert f'action="/library/episode/{ep2["episode_id"]}/dismiss"' in section
    assert 'data-control="retry"' in section
    assert 'data-control="dismiss"' in section


# --- Plan 11 Task 2: one click to add an edition ----------------------------


async def test_episode_row_ready_and_settled_shows_add_edition_control_with_right_target(
    client, queries_stub
):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, source_path="ep-01.mkv")
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, _episode(title_id=title_id, number=2)]

    async with client:
        resp = await client.get("/library")

    section = resp.text.split(f'data-episode-id="{ep1["episode_id"]}"')[1].split("</li>")[0]
    assert 'data-control="add-edition"' in section
    assert f'href="/add/ep-01.mkv?target_episode_id={ep1["episode_id"]}"' in section


async def test_episode_row_failed_also_shows_add_edition_control(client, queries_stub):
    title_id = uuid.uuid4()
    ep2 = _episode(title_id=title_id, number=2, edition_status="failed", edition_failure_reason="failed.reason.upload")
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=1, failed_count=1)]
    queries_stub.episodes_by_title[title_id] = [_episode(title_id=title_id, number=1), ep2]

    async with client:
        resp = await client.get("/library")

    section = resp.text.split(f'data-episode-id="{ep2["episode_id"]}"')[1].split("</li>")[0]
    assert 'data-control="add-edition"' in section


async def test_episode_row_pending_delete_shows_countdown_and_undo_purge(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, episode_status="pending_delete")
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=1)]
    queries_stub.episodes_by_title[title_id] = [ep1, _episode(title_id=title_id, number=2)]

    async with client:
        resp = await client.get("/library")

    section = resp.text.split(f'data-episode-id="{ep1["episode_id"]}"')[1].split("</li>")[0]
    assert "chip--deleting" in section
    assert f'action="/library/episode/{ep1["episode_id"]}/undo"' in section
    assert f'action="/library/episode/{ep1["episode_id"]}/purge"' in section
    assert 'data-control="undo"' in section
    assert 'data-control="purge"' in section


# --- Plan 11 Task 1: the edition list, the badge, and the pointer that only
# --- the operator moves -----------------------------------------------------


def _edition_section(resp_text: str, edition_id) -> str:
    """One edition row's own markup, bounded by its own `data-edition-id`
    and its own closing `</li>` -- safe against the nested `<li>` an
    episode's own edition-list disclosure now contains, unlike splitting on
    an episode's `data-episode-id` (which an edition row's own `</li>`
    would close too early).
    """
    return resp_text.split(f'data-edition-id="{edition_id}"')[1].split("</li>")[0]


async def test_edition_row_disclosure_absent_for_a_single_edition_episode(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1, edition_count=1),
        _episode(title_id=title_id, number=2, edition_count=1),
    ]

    async with client:
        resp = await client.get("/library")

    assert "edition-list" not in resp.text
    assert queries_stub.editions_for_library_calls == 1
    assert queries_stub.editions_for_library_episode_ids == []


async def test_edition_row_disclosure_renders_closed_with_count_for_two_editions(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, edition_count=2)
    ep2 = _episode(title_id=title_id, number=2, edition_count=1)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", slug="two-ed", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]
    default_edition = _edition(episode_id=ep1["episode_id"], is_default=True, slug=None)
    other_edition = _edition(episode_id=ep1["episode_id"], is_default=False, slug="dub-ru")
    queries_stub.editions_by_episode[ep1["episode_id"]] = [default_edition, other_edition]

    async with client:
        resp = await client.get("/library")

    assert queries_stub.editions_for_library_episode_ids == [ep1["episode_id"]]
    details_tag = re.search(r'<details class="edition-list"[^>]*>', resp.text).group(0)
    assert " open" not in details_tag
    assert "2 editions" in resp.text


async def test_edition_row_renders_for_every_edition_including_the_default(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, edition_count=2)
    ep2 = _episode(title_id=title_id, number=2, edition_count=1)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]
    default_edition = _edition(episode_id=ep1["episode_id"], is_default=True, slug=None)
    other_edition = _edition(episode_id=ep1["episode_id"], is_default=False, slug="dub-ru")
    queries_stub.editions_by_episode[ep1["episode_id"]] = [default_edition, other_edition]

    async with client:
        resp = await client.get("/library")

    assert f'data-edition-id="{default_edition["edition_id"]}"' in resp.text
    assert f'data-edition-id="{other_edition["edition_id"]}"' in resp.text


async def test_edition_row_default_has_badge_no_make_default_and_disabled_delete_blocked_reason(
    client, queries_stub
):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, edition_count=2)
    ep2 = _episode(title_id=title_id, number=2, edition_count=1)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]
    default_edition = _edition(episode_id=ep1["episode_id"], is_default=True, slug=None)
    other_edition = _edition(episode_id=ep1["episode_id"], is_default=False)
    queries_stub.editions_by_episode[ep1["episode_id"]] = [default_edition, other_edition]

    async with client:
        resp = await client.get("/library")

    section = _edition_section(resp.text, default_edition["edition_id"])
    assert "Default" in section
    assert 'data-control="edition-make-default"' not in section
    assert "disabled" in section
    assert "This is the default edition" in section


async def test_edition_row_non_default_has_make_default_and_enabled_delete(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, edition_count=2)
    ep2 = _episode(title_id=title_id, number=2, edition_count=1)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]
    default_edition = _edition(episode_id=ep1["episode_id"], is_default=True, slug=None)
    other_edition = _edition(episode_id=ep1["episode_id"], is_default=False)
    queries_stub.editions_by_episode[ep1["episode_id"]] = [default_edition, other_edition]

    async with client:
        resp = await client.get("/library")

    section = _edition_section(resp.text, other_edition["edition_id"])
    assert f'action="/library/edition/{other_edition["edition_id"]}/make-default"' in section
    assert f'action="/library/edition/{other_edition["edition_id"]}/delete"' in section
    assert 'data-control="edition-make-default"' in section
    assert 'data-control="edition-delete"' in section
    assert "disabled" not in section


async def test_edition_row_signs_only_detail_line_renders_when_resolved(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, edition_count=2)
    ep2 = _episode(title_id=title_id, number=2, edition_count=1)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]
    default_edition = _edition(episode_id=ep1["episode_id"], is_default=True, slug=None)
    signs_edition = _edition(
        episode_id=ep1["episode_id"],
        is_default=False,
        subtitle_burned_language="ru",
        subtitle_burned_signs_only=True,
    )
    queries_stub.editions_by_episode[ep1["episode_id"]] = [default_edition, signs_edition]

    async with client:
        resp = await client.get("/library")

    section = _edition_section(resp.text, signs_edition["edition_id"])
    assert "signs only" in section
    assert "Subtitles burned: ru, signs only" in section


async def test_edition_row_detail_line_unaffected_when_signs_only_unresolved(client, queries_stub):
    title_id = uuid.uuid4()
    ep1 = _episode(title_id=title_id, number=1, edition_count=2)
    ep2 = _episode(title_id=title_id, number=2, edition_count=1)
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [ep1, ep2]
    default_edition = _edition(episode_id=ep1["episode_id"], is_default=True, slug=None)
    unresolved_edition = _edition(
        episode_id=ep1["episode_id"],
        is_default=False,
        subtitle_burned_language="ru",
        subtitle_burned_signs_only=None,
    )
    queries_stub.editions_by_episode[ep1["episode_id"]] = [default_edition, unresolved_edition]

    async with client:
        resp = await client.get("/library")

    section = _edition_section(resp.text, unresolved_edition["edition_id"])
    assert "signs only" not in section
    assert "Subtitles burned: ru" in section


async def _create_episode_with_two_editions(conn, name: str, slug: str):
    """A one-episode title whose one episode carries a second, non-default
    edition -- the shape every edition-action integration test below acts
    on, mirroring test_deletion.py's own `_create_two_episode_series`.
    """
    edition1 = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        conn, name, slug, edition1, f"{edition1}.mp4"
    )
    episode_row = await queries.episode_by_number(conn, title_id, 1)
    episode_id = episode_row["episode_id"]
    edition2 = uuid.uuid4()
    await queries.add_edition_to_episode(
        conn, episode_id, "dub-ru", edition2, f"{edition2}.mp4"
    )
    return title_id, episode_id, edition1, edition2


@pytest.mark.integration
async def test_make_default_moves_the_pointer_and_previous_default_keeps_its_own_address(
    action_client, raw_conn
):
    _title_id, episode_id, edition1, edition2 = await _create_episode_with_two_editions(
        raw_conn, "Make Default", "make-default-slug"
    )

    resp = await action_client.post(f"/library/edition/{edition2}/make-default")

    assert resp.status_code == 302
    episode = await queries.episode_for_action(raw_conn, episode_id)
    assert episode["default_edition_id"] == edition2
    still_there = await queries.resolve_edition(raw_conn, edition1)
    assert still_there is not None


@pytest.mark.integration
async def test_deleting_the_default_edition_is_blocked_while_another_exists(
    action_client, raw_conn
):
    _title_id, episode_id, edition1, _edition2 = await _create_episode_with_two_editions(
        raw_conn, "Blocked Default", "blocked-default-slug"
    )

    resp = await action_client.post(f"/library/edition/{edition1}/delete")

    assert resp.status_code == 404
    episode = await queries.episode_for_action(raw_conn, episode_id)
    assert episode["default_edition_id"] == edition1


@pytest.mark.integration
async def test_deleting_a_non_default_edition_leaves_the_pointer_where_it_was(
    action_client, raw_conn
):
    _title_id, episode_id, edition1, edition2 = await _create_episode_with_two_editions(
        raw_conn, "Delete Non Default", "delete-non-default-slug"
    )

    resp = await action_client.post(f"/library/edition/{edition2}/delete")

    assert resp.status_code == 302
    episode = await queries.episode_for_action(raw_conn, episode_id)
    assert episode["default_edition_id"] == edition1
    target = await queries.edition_for_action(raw_conn, edition2)
    assert target["edition_status"] == "pending_delete"

    # raw_conn commits for real and outlives this test (conftest.py's own
    # docstring on that fixture) -- a `pending_delete` edition is a status
    # value that only exists from this migration onward, so leaving one
    # committed would break a later downgrade-to-base round trip
    # elsewhere in this same session. Cleaned up the same way an operator
    # would finish the countdown, not left to rot.
    await queries.delete_edition(raw_conn, edition2)


@pytest.mark.integration
async def test_undo_on_a_pending_edition_cancels_the_countdown(action_client, raw_conn):
    _title_id, _episode_id, _edition1, edition2 = await _create_episode_with_two_editions(
        raw_conn, "Undo Edition", "undo-edition-slug"
    )
    await queries.mark_edition_for_delete(raw_conn, edition2, grace_seconds=3600)

    resp = await action_client.post(f"/library/edition/{edition2}/undo")

    assert resp.status_code == 302
    target = await queries.edition_for_action(raw_conn, edition2)
    assert target["edition_status"] == "active"


@pytest.mark.integration
async def test_purge_on_a_pending_edition_deletes_the_object_and_the_row(
    action_client, raw_conn, monkeypatch
):
    from orchestrator import reaper as reaper_module

    deleted = []
    monkeypatch.setattr(reaper_module, "delete_object", lambda key: deleted.append(key))

    _title_id, _episode_id, _edition1, edition2 = await _create_episode_with_two_editions(
        raw_conn, "Purge Edition", "purge-edition-slug"
    )
    await queries.mark_edition_for_delete(raw_conn, edition2, grace_seconds=3600)

    resp = await action_client.post(f"/library/edition/{edition2}/purge")

    assert resp.status_code == 302
    assert f"{edition2}.mp4" in deleted
    target = await queries.edition_for_action(raw_conn, edition2)
    assert target is None


# --- Plan 09 Task 3: the poll that does not throw away what the operator did


async def test_poll_status_follows_in_flight_episodes_for_an_already_active_series(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", episode_count=2, ready_count=1,
        job_episode_number=2, **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=10.0),
    )]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(
            title_id=title_id, number=2, edition_status="preparing",
            **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=10.0),
        ),
    ]

    async with client:
        resp = await client.get("/library")

    li_tag = re.search(r'<li class="library-row[^>]*>', resp.text).group(0)
    assert 'data-row-status="preparing"' in li_tag


async def test_poll_status_is_the_title_status_once_a_series_settles(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(title_id=title_id, title_status="active", episode_count=2, ready_count=2)]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2),
    ]

    async with client:
        resp = await client.get("/library")

    li_tag = re.search(r'<li class="library-row[^>]*>', resp.text).group(0)
    assert 'data-row-status="active"' in li_tag


async def test_in_flight_active_row_renders_address_copy_button_and_chips_together(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.rows = [_row(
        title_id=title_id, title_status="active", slug="in-flight-show",
        episode_count=2, ready_count=1, job_episode_number=2,
        **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=40.0),
    )]
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(
            title_id=title_id, number=2, edition_status="preparing",
            **_job(job_kind="transcode", job_db_stage="working", job_progress_pct=40.0),
        ),
    ]

    async with client:
        resp = await client.get("/library")

    assert "https://mytube.example/v/in-flight-show" in resp.text
    assert "data-copy-text=\"https://mytube.example/v/in-flight-show\"" in resp.text
    assert "1 of 2 ready, episode 2" in resp.text
    assert "40%" in resp.text


async def test_fragment_route_never_emits_the_open_attribute_on_the_episode_list(client, queries_stub):
    """The default-open rule is initial-render only (05-UI-SPEC.md Screen D
    item 3) -- a fragment carries a failed episode (which would default-
    open on a full render) and still never sets `open`, since the poller
    already copied the operator's own choice onto it before the swap.
    """
    title_id = uuid.uuid4()
    row = _row(title_id=title_id, title_status="active", episode_count=2, ready_count=1, failed_count=1)
    queries_stub.row_by_id[title_id] = row
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2, edition_status="failed"),
    ]

    async with client:
        resp = await client.get(f"/library/{title_id}/row")

    details_tag = re.search(r"<details[^>]*>", resp.text).group(0)
    assert " open" not in details_tag


async def test_fragment_route_renders_the_same_episode_content_as_the_full_page(client, queries_stub):
    title_id = uuid.uuid4()
    row = _row(title_id=title_id, title_status="active", slug="parity-show", episode_count=2, ready_count=2)
    queries_stub.rows = [row]
    queries_stub.row_by_id[title_id] = dict(row)
    queries_stub.episodes_by_title[title_id] = [
        _episode(title_id=title_id, number=1),
        _episode(title_id=title_id, number=2),
    ]

    async with client:
        full_resp = await client.get("/library")
        fragment_resp = await client.get(f"/library/{title_id}/row")

    assert "https://mytube.example/v/parity-show/1" in full_resp.text
    assert "https://mytube.example/v/parity-show/1" in fragment_resp.text
    assert "2 episodes" in full_resp.text
    assert "2 episodes" in fragment_resp.text


def test_poller_syncs_episode_disclosure_state_and_restores_focus_by_data_episode_id():
    """Reads the script rather than driving a browser (this codebase has
    none) -- the poller's own three added lines, and the two small
    helpers they call, both keyed off data-episode-id per 05-UI-SPEC.md
    Screen D item 3.
    """
    text = (REPO_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "_syncEpisodeState(row, fresh)" in text
    assert "_restoreFocus(fresh, focusKey)" in text
    assert 'details[data-episode-id' in text
    # The poller's existing entry points are unchanged -- same signature,
    # same fetch/redirect/remove/catch shape around the three new lines.
    assert "function pollLibraryRow(row) {" in text
    assert "row.replaceWith(fresh);" in text


# --- An episode's number can be corrected without breaking its link
# --- (Screen F, D-161) -- real Postgres, the same action_client/raw_conn
# --- split every other action test above draws -----------------------------


async def _create_two_episode_series(conn, name: str, slug: str):
    """A two-episode series, episode 2 added directly through the query
    layer -- no folder-add flow exists to drive this through in a route
    test, mirroring test_resolve_episode.py's own `_add_episode`. Returns
    (title_id, slug, episode_1_id, episode_2_id).
    """
    edition1 = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        conn, name, slug, edition1, f"{edition1}.mp4"
    )
    episode1 = await queries.episode_by_number(conn, title_id, 1)
    episode1_id = episode1["episode_id"]

    season_id = await conn.fetchval("SELECT id FROM seasons WHERE title_id = $1", title_id)
    edition2 = uuid.uuid4()
    episode2_id = await conn.fetchval(
        "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
    )
    await conn.execute(
        "INSERT INTO editions (id, episode_id, object_key, status) VALUES ($1, $2, $3, 'active')",
        edition2,
        episode2_id,
        f"{edition2}.mp4",
    )
    await conn.execute(
        "UPDATE episodes SET default_edition_id = $1 WHERE id = $2", edition2, episode2_id
    )
    await queries.claim_episode_number(conn, title_id, episode2_id, 2)
    return title_id, slug, episode1_id, episode2_id


@pytest.mark.integration
async def test_renumber_form_shows_current_address_prefilled_number_and_padded_preview(
    action_client, raw_conn
):
    _title_id, slug, episode1_id, _episode2_id = await _create_two_episode_series(
        raw_conn, "Renumber Form", "renumber-form-slug"
    )

    resp = await action_client.get(f"/library/episode/{episode1_id}/renumber")

    assert resp.status_code == 200
    # The current address line and the preview both show the canonical
    # zero-padded form (D-155), never the bare "1".
    assert f"{slug}/01" in resp.text
    assert 'value="1"' in resp.text


@pytest.mark.integration
async def test_renumber_confirm_refuses_a_blank_number_inline(action_client, raw_conn):
    _title_id, _slug, episode1_id, _episode2_id = await _create_two_episode_series(
        raw_conn, "Renumber Invalid", "renumber-invalid-slug"
    )

    resp = await action_client.post(
        f"/library/episode/{episode1_id}/renumber", data={"number": ""}
    )

    assert resp.status_code == 200
    assert "Enter a whole number" in resp.text


@pytest.mark.integration
async def test_renumber_confirm_refuses_a_number_a_sibling_currently_holds(action_client, raw_conn):
    _title_id, _slug, episode1_id, _episode2_id = await _create_two_episode_series(
        raw_conn, "Renumber Taken", "renumber-taken-slug"
    )

    resp = await action_client.post(
        f"/library/episode/{episode1_id}/renumber", data={"number": "2"}
    )

    assert resp.status_code == 200
    assert "already exists in this title" in resp.text
    # Refused -- episode_1 is still at its own original number.
    row = await queries.episode_for_number(raw_conn, _title_id, 1)
    assert row["episode_id"] == episode1_id
    assert row["is_current"] is True


@pytest.mark.integration
async def test_renumber_gate_appears_for_a_siblings_former_number_and_not_for_a_free_one(
    action_client, raw_conn
):
    title_id, _slug, episode1_id, episode2_id = await _create_two_episode_series(
        raw_conn, "Renumber Gate", "renumber-gate-slug"
    )
    # episode_2 moves off 2, onto 20 -- 2 is now only its *former* number.
    await queries.claim_episode_number(raw_conn, title_id, episode2_id, 20)

    gated_resp = await action_client.post(
        f"/library/episode/{episode1_id}/renumber", data={"number": "2"}
    )
    assert gated_resp.status_code == 200
    assert "used to belong to episode" in gated_resp.text
    # Not saved yet -- episode_1 is still at 1 until the gate is confirmed.
    still_one = await queries.episode_for_number(raw_conn, title_id, 1)
    assert still_one["episode_id"] == episode1_id
    assert still_one["is_current"] is True

    free_resp = await action_client.post(
        f"/library/episode/{episode1_id}/renumber", data={"number": "55"}
    )
    assert free_resp.status_code == 200
    assert "used to belong to episode" not in free_resp.text
    # A genuinely free number is not gated -- it saves immediately.
    assert "The old number keeps working" in free_resp.text


@pytest.mark.integration
async def test_renumber_confirmed_gate_saves_and_old_number_still_resolves_to_the_same_episode(
    action_client, raw_conn
):
    title_id, slug, episode1_id, episode2_id = await _create_two_episode_series(
        raw_conn, "Renumber Confirm", "renumber-confirm-slug"
    )
    await queries.claim_episode_number(raw_conn, title_id, episode2_id, 20)

    resp = await action_client.post(
        f"/library/episode/{episode1_id}/renumber",
        data={"number": "2", "confirm_reuse": "true"},
    )

    assert resp.status_code == 200
    assert "The old number keeps working" in resp.text
    # Done state prints both addresses, canonically padded.
    assert f"{slug}/01" in resp.text
    assert f"{slug}/02" in resp.text
    assert "/library" in resp.text
    assert f"/check/{slug}/2" in resp.text

    new_row = await queries.resolve_episode_slug(raw_conn, slug, 2)
    assert new_row["episode_id"] == episode1_id
    old_row = await queries.resolve_episode_slug(raw_conn, slug, 1)
    assert old_row["episode_id"] == episode1_id


@pytest.mark.integration
async def test_renumber_after_a_later_episode_claims_the_old_number_the_newer_claim_wins(
    action_client, raw_conn
):
    title_id, slug, episode1_id, episode2_id = await _create_two_episode_series(
        raw_conn, "Renumber Handover", "renumber-handover-slug"
    )

    # episode_1 (number 1) moves to 9, freeing 1 as its own former number.
    resp = await action_client.post(
        f"/library/episode/{episode1_id}/renumber", data={"number": "9"}
    )
    assert resp.status_code == 200

    # episode_2 (currently 2) now claims 1 -- only a former number of a
    # *different* episode (episode_1), never episode_2's own, so no gate.
    resp2 = await action_client.post(
        f"/library/episode/{episode2_id}/renumber", data={"number": "1"}
    )
    assert resp2.status_code == 200
    assert "used to belong to episode" in resp2.text

    resp3 = await action_client.post(
        f"/library/episode/{episode2_id}/renumber",
        data={"number": "1", "confirm_reuse": "true"},
    )
    assert resp3.status_code == 200

    winner = await queries.resolve_episode_slug(raw_conn, slug, 1)
    assert winner["episode_id"] == episode2_id
    # episode_1 is still reachable at its own current number.
    still_reachable = await queries.resolve_episode_slug(raw_conn, slug, 9)
    assert still_reachable["episode_id"] == episode1_id

# --- A standalone one-episode title can move into a title, from the
# --- library (Screen G, D-160) ----------------------------------------------


async def test_move_control_renders_only_on_a_one_episode_title(client, queries_stub):
    """Screen D's own single addition to a one-episode title's row
    (D-160): a film or any one-episode title renders as it does today,
    plus this one ghost action. A series row never gets it.
    """
    standalone_id = uuid.uuid4()
    series_id = uuid.uuid4()
    queries_stub.rows = [
        _row(title_id=standalone_id, slug="move-control-standalone"),
        _row(title_id=series_id, slug="move-control-series", episode_count=2, ready_count=2),
    ]
    queries_stub.episodes_by_title[series_id] = [
        _episode(title_id=series_id, number=1),
        _episode(title_id=series_id, number=2),
    ]

    async with client:
        resp = await client.get("/library")

    standalone_section = resp.text.split(f'data-title-id="{series_id}"')[0]
    series_section = resp.text.split(f'data-title-id="{series_id}"')[1]
    assert f"/library/title/{standalone_id}/move" in standalone_section
    assert f"/library/title/{series_id}/move" not in series_section


@pytest.mark.integration
async def test_move_form_prefills_name_and_number_from_the_recogniser(action_client, raw_conn):
    title_id, edition_id, _object_key = await _create_title(
        raw_conn, "Bocchi the Rock - 12", "bocchi-move-source"
    )
    await raw_conn.execute(
        "UPDATE editions SET source_path = $1 WHERE id = $2",
        "Bocchi the Rock - 12.mkv",
        edition_id,
    )

    resp = await action_client.get(f"/library/title/{title_id}/move")

    assert resp.status_code == 200
    assert 'value="12"' in resp.text
    assert "Bocchi the Rock" in resp.text


@pytest.mark.integration
async def test_move_existing_title_repoints_and_leaves_the_object_key_untouched(
    action_client, raw_conn
):
    target_title_id, _target_edition_id, _ = await _create_title(
        raw_conn, "Move Target Series", "move-target-series"
    )
    source_title_id, source_edition_id, source_object_key = await _create_title(
        raw_conn, "Move Source Standalone", "move-source-standalone"
    )

    resp = await action_client.post(
        f"/library/title/{source_title_id}/move",
        data={"mode": "existing", "target_title_id": str(target_title_id), "number": "7"},
    )

    assert resp.status_code == 200
    assert "The old address keeps working" in resp.text
    assert "move-source-standalone" in resp.text
    assert "move-target-series/07" in resp.text

    old_row = await queries.resolve_slug(raw_conn, "move-source-standalone")
    assert old_row is not None
    assert old_row["title_id"] == target_title_id

    new_row = await queries.resolve_episode_slug(raw_conn, "move-target-series", 7)
    assert new_row is not None
    assert new_row["edition_id"] == source_edition_id

    edition_row = await raw_conn.fetchrow(
        "SELECT object_key FROM editions WHERE id = $1", source_edition_id
    )
    assert edition_row["object_key"] == source_object_key


@pytest.mark.integration
async def test_move_taken_number_refuses_inline_and_keeps_the_title_selected(
    action_client, raw_conn
):
    target_title_id, _target_edition_id, _ = await _create_title(
        raw_conn, "Move Taken Target", "move-taken-target"
    )
    source_title_id, _source_edition_id, _ = await _create_title(
        raw_conn, "Move Taken Source", "move-taken-source"
    )

    resp = await action_client.post(
        f"/library/title/{source_title_id}/move",
        data={"mode": "existing", "target_title_id": str(target_title_id), "number": "1"},
    )

    assert resp.status_code == 200
    assert "already exists in this title" in resp.text
    assert f'value="{target_title_id}"' in resp.text or "checked" in resp.text


@pytest.mark.integration
async def test_move_filter_narrows_the_candidate_list_server_side(action_client, raw_conn):
    await _create_title(raw_conn, "Filter Match Show", "filter-match-show")
    await _create_title(raw_conn, "Unrelated Other Show", "unrelated-other-show")
    source_title_id, _edition_id, _ = await _create_title(
        raw_conn, "Filter Source Standalone", "filter-source-standalone"
    )

    resp = await action_client.get(
        f"/library/title/{source_title_id}/move", params={"filter": "Filter Match"}
    )

    assert resp.status_code == 200
    assert "Filter Match Show" in resp.text
    assert "Unrelated Other Show" not in resp.text


@pytest.mark.integration
async def test_move_filter_with_pattern_characters_is_matched_as_text(action_client, raw_conn):
    await _create_title(raw_conn, "50% Off Show", "fifty-percent-off-show")
    source_title_id, _edition_id, _ = await _create_title(
        raw_conn, "Percent Source Standalone", "percent-source-standalone"
    )

    resp = await action_client.get(
        f"/library/title/{source_title_id}/move", params={"filter": "50%"}
    )

    assert resp.status_code == 200
    assert "50% Off Show" in resp.text


@pytest.mark.integration
async def test_move_new_title_creates_it_then_repoints_and_old_address_stays_alive(
    action_client, raw_conn
):
    source_title_id, source_edition_id, _ = await _create_title(
        raw_conn, "Merge Source Standalone", "merge-source-standalone"
    )

    resp = await action_client.post(
        f"/library/title/{source_title_id}/move",
        data={"mode": "new", "name": "Merge New Series", "number": "1"},
    )

    assert resp.status_code == 200
    assert "The old address keeps working" in resp.text

    old_row = await queries.resolve_slug(raw_conn, "merge-source-standalone")
    assert old_row is not None
    new_row = await queries.resolve_episode_slug(raw_conn, "merge-new-series", 1)
    assert new_row is not None
    assert new_row["edition_id"] == source_edition_id


@pytest.mark.integration
async def test_move_merges_two_standalone_titles_with_both_old_addresses_alive(
    action_client, raw_conn
):
    """D-160's own second scenario: no folder involved at all, two
    standalone one-episode titles merged into one series.
    """
    target_title_id, _target_edition_id, _ = await _create_title(
        raw_conn, "Merge Target Standalone", "merge-target-standalone"
    )
    source_title_id, _source_edition_id, _ = await _create_title(
        raw_conn, "Merge Second Standalone", "merge-second-standalone"
    )

    resp = await action_client.post(
        f"/library/title/{source_title_id}/move",
        data={"mode": "existing", "target_title_id": str(target_title_id), "number": "2"},
    )

    assert resp.status_code == 200
    target_still = await queries.resolve_slug(raw_conn, "merge-target-standalone")
    assert target_still is not None
    source_still = await queries.resolve_slug(raw_conn, "merge-second-standalone")
    assert source_still is not None
    assert source_still["title_id"] == target_title_id


@pytest.mark.integration
async def test_move_then_renumber_the_same_episode_every_address_it_ever_had_still_resolves(
    action_client, raw_conn
):
    """The plan's own overall verification: a move and a renumber on the
    same episode, in sequence -- every address that episode has ever
    answered at (its old standalone slug, its first numbered address in
    the series, and its renumbered address) still resolves to it
    afterwards.
    """
    target_title_id, _target_edition_id, _ = await _create_title(
        raw_conn, "Combined Target Series", "combined-target-series"
    )
    source_title_id, source_edition_id, _ = await _create_title(
        raw_conn, "Combined Source Standalone", "combined-source-standalone"
    )

    move_resp = await action_client.post(
        f"/library/title/{source_title_id}/move",
        data={"mode": "existing", "target_title_id": str(target_title_id), "number": "5"},
    )
    assert move_resp.status_code == 200

    moved_episode = await queries.episode_by_number(raw_conn, target_title_id, 5)
    episode_id = moved_episode["episode_id"]

    renumber_resp = await action_client.post(
        f"/library/episode/{episode_id}/renumber", data={"number": "8"}
    )
    assert renumber_resp.status_code == 200

    old_standalone = await queries.resolve_slug(raw_conn, "combined-source-standalone")
    assert old_standalone is not None
    assert old_standalone["title_id"] == target_title_id

    old_number = await queries.resolve_episode_slug(raw_conn, "combined-target-series", 5)
    assert old_number is not None
    assert old_number["episode_id"] == episode_id
    assert old_number["edition_id"] == source_edition_id

    new_number = await queries.resolve_episode_slug(raw_conn, "combined-target-series", 8)
    assert new_number is not None
    assert new_number["episode_id"] == episode_id
    assert new_number["edition_id"] == source_edition_id
