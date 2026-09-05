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

    async def _playlist_rows(conn):
        return stub.playlist_rows

    monkeypatch.setattr(queries, "library_rows", _library_rows)
    monkeypatch.setattr(queries, "library_row", _library_row)
    monkeypatch.setattr(queries, "count_preparing_ahead", _count_preparing_ahead)
    monkeypatch.setattr(queries, "failed_title_object_keys", _failed_title_object_keys)
    monkeypatch.setattr(queries, "delete_failed_title", _delete_failed_title)
    monkeypatch.setattr(queries, "referenced_objects", _referenced_objects)
    monkeypatch.setattr(queries, "playlist_rows", _playlist_rows, raising=False)
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
    # SPEC.md States -> Preview) landed alongside them -- still well under
    # a vendored drag-and-drop or lightbox library's own line count, so
    # the guard still catches what it was built to catch.
    code = [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]
    assert len(code) < 180


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
