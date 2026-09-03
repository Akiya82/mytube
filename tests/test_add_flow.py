"""End-to-end coverage of preparing -> active and preparing -> failed
(D-45..D-53), against a real Postgres (testcontainers) and real
ffmpeg/ffprobe on small synthetic clips built once per module -- never
hand-typed JSON, matching plan 03's own precedent for this analyzer.

**Storage is mocked.** Assert an upload was *called* with the expected key
and content type, the way `tests/test_deletion.py` already asserts
deletion against a fake `delete_object` -- there is no bucket this test
can reach (the `storage` service publishes no port and sits on an
`internal: true` network, and the endpoint the stack is actually
configured against now is the owner's real storage). The real object is
confirmed in the manual live-run row of this plan's own SUMMARY.
"""
import asyncio
import contextlib
import subprocess
import time
import uuid

import httpx
import pytest
from httpx import ASGITransport

from mytube_media import remux as remux_module
from mytube_media.compat import audio_tracks
from mytube_media.compat import probe as ffprobe_probe
from orchestrator.config import get_settings
from orchestrator.main import app
from orchestrator.media import queue as queue_module
from orchestrator.media.queue import AddJob, AddQueue

from conftest import ADMIN_FQDN, SESSION_COOKIE

pytestmark = pytest.mark.integration


def _run_ffmpeg(args):
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """Two small, real H.264 clips, one second long -- one already fully
    compliant (AAC, faststart), one whose audio needs re-encoding (mp3).
    Built once per module with the real local ffmpeg/ffprobe, the same
    "capture from a real run, never hand-type" discipline plan 03 already
    established for the compatibility fixtures.
    """
    root = tmp_path_factory.mktemp("clips")
    already_correct = root / "already-correct.mp4"
    needs_reencode = root / "needs-reencode.mp4"

    common = [
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1",
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
    ]
    _run_ffmpeg(
        [*common, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(already_correct)]
    )
    _run_ffmpeg(
        [*common, "-c:a", "libmp3lame", "-b:a", "128k", "-movflags", "+faststart", str(needs_reencode)]
    )

    ac_track = audio_tracks(ffprobe_probe(already_correct))[0]
    nr_track = audio_tracks(ffprobe_probe(needs_reencode))[0]

    return {
        "already_correct": {
            "path": already_correct,
            "audio_index": ac_track.index,
            "channels": ac_track.channels,
        },
        "needs_reencode": {
            "path": needs_reencode,
            "audio_index": nr_track.index,
            "channels": nr_track.channels,
        },
    }


@pytest.fixture
def env(migrated_db_url, clips, tmp_path, monkeypatch):
    share_root = tmp_path / "share"
    share_root.mkdir()
    for clip in clips.values():
        source = clip["path"]
        (share_root / source.name).write_bytes(source.read_bytes())

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    monkeypatch.setenv("MYTUBE_DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(share_root))
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield {"share_root": share_root, "work_dir": work_dir}
    get_settings.cache_clear()


@pytest.fixture
def storage_spy(monkeypatch):
    """Every storage call the worker and the startup sweep make, recorded
    instead of reaching a real bucket -- patched at the name each caller
    actually imported, not at `orchestrator.storage` itself (a `from x
    import y` binds its own reference; patching the origin module leaves
    the caller's copy untouched).
    """
    calls = {"put": [], "abort": [], "delete": []}

    def fake_put(path, key, content_type="video/mp4"):
        calls["put"].append((path, key, content_type))

    def fake_abort(key):
        calls["abort"].append(key)

    def fake_delete(key):
        calls["delete"].append(key)

    monkeypatch.setattr(queue_module, "put_object_from_file", fake_put)
    monkeypatch.setattr(queue_module, "abort_dangling_multipart", fake_abort)
    monkeypatch.setattr(queue_module, "delete_object", fake_delete)

    import orchestrator.main as main_module

    monkeypatch.setattr(main_module, "abort_dangling_multipart", fake_abort)
    monkeypatch.setattr(main_module, "delete_object", fake_delete)
    return calls


@pytest.fixture
async def client(env, storage_spy, signed_in):
    """Runs the real lifespan (real Postgres pool, real add worker task,
    real startup sweep) against an in-memory ASGI transport -- the only
    way to exercise D-49's "the row is written, the response returns, and
    a background task does the rest" shape for real.

    Because the lifespan runs for real here, it also queries the real
    (shared, session-scoped) `operator_credential` table for real -- and
    this file never creates a row in it, so without the override below
    the gate's own first-run branch would redirect every request in this
    whole suite to `/setup`, `signed_in`'s stubbed session notwithstanding
    (that stub only replaces `session_operator_id`, which the first-run
    check runs ahead of). These are the add flow's own tests, not the
    first-run gate's, so this treats "an operator exists" as already
    established the same way `signed_in` already treats "a session
    exists" as already established.
    """
    async with app.router.lifespan_context(app):
        app.state.operator_exists = True
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
        ) as ac:
            yield ac


async def _wait_until(predicate, timeout=30.0, interval=0.05):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def _post_data(clip: dict, *, name: str, slug: str, confirm_reuse: str = "false") -> dict:
    return {
        "name": name,
        "slug": slug,
        "audio_track": str(clip["audio_index"]),
        "confirm_reuse": confirm_reuse,
    }


# --- happy path -------------------------------------------------------


async def test_post_add_returns_redirect_with_preparing_row_and_null_started_at(
    client, raw_conn, clips, monkeypatch
):
    """The claim under test -- "the row is written and the response
    returns before the background work does anything" -- is a real
    design property (D-49), but asserting it by racing a wall-clock
    check against the worker task after `await client.post(...)` returns
    is not reliable: asyncio gives the worker task turns to run during
    that same `await`, and on a fast enough machine it can call
    `mark_title_started` before this test's own next line runs. Gate the
    worker deterministically instead -- block it before it touches
    anything, so what this test observes is genuinely "immediately after
    the transaction commits and the route returns", not "whatever the
    scheduler happened to interleave this run".
    """
    worker_may_proceed = asyncio.Event()
    real_process_add = queue_module.process_add

    async def gated_process_add(pool, job):
        await worker_may_proceed.wait()
        await real_process_add(pool, job)

    monkeypatch.setattr(queue_module, "process_add", gated_process_add)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Preparing Row Clip", slug="preparing-row-clip"),
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/library"

    row = await raw_conn.fetchrow(
        "SELECT status, started_at FROM titles WHERE name = $1", "Preparing Row Clip"
    )
    assert row is not None
    assert row["status"] == "preparing"
    assert row["started_at"] is None

    worker_may_proceed.set()


async def test_worker_drains_to_active_upload_called_and_v_slug_redirects(
    client, raw_conn, clips, storage_spy
):
    clip = clips["already_correct"]
    start = time.monotonic()
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Drain Clip", slug="drain-clip"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_active():
        row = await raw_conn.fetchrow("SELECT status FROM titles WHERE name = $1", "Drain Clip")
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)
    elapsed = time.monotonic() - start
    assert elapsed < 60, "the control-clip-sized add must land in under a minute (D-51)"

    title = await raw_conn.fetchrow(
        "SELECT id, started_at FROM titles WHERE name = $1", "Drain Clip"
    )
    assert title["started_at"] is not None

    # CAT-04: exactly one season, one episode, one edition for the film --
    # no separate entity type.
    counts = await raw_conn.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM seasons WHERE title_id = $1) AS seasons,
            (SELECT count(*) FROM episodes ep JOIN seasons s ON s.id = ep.season_id
                WHERE s.title_id = $1) AS episodes,
            (SELECT count(*) FROM editions e JOIN episodes ep ON ep.id = e.episode_id
                JOIN seasons s ON s.id = ep.season_id WHERE s.title_id = $1) AS editions
        """,
        title["id"],
    )
    assert (counts["seasons"], counts["episodes"], counts["editions"]) == (1, 1, 1)

    edition = await raw_conn.fetchrow(
        """
        SELECT e.object_key FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        WHERE s.title_id = $1
        """,
        title["id"],
    )
    assert any(
        key == edition["object_key"] and content_type == "video/mp4"
        for _path, key, content_type in storage_spy["put"]
    )

    v_resp = await client.get("/v/drain-clip", follow_redirects=False)
    assert v_resp.status_code == 302


async def test_needs_reencode_clip_also_reaches_active(client, raw_conn, clips):
    clip = clips["needs_reencode"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Reencode Clip", slug="reencode-clip"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_active():
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Reencode Clip"
        )
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)

    edition = await raw_conn.fetchrow(
        """
        SELECT e.audio_reencoded FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        WHERE t.name = $1
        """,
        "Reencode Clip",
    )
    assert edition["audio_reencoded"] is True


async def test_poster_key_set_after_successful_add(client, raw_conn, clips):
    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Poster OK", slug="poster-ok"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_active():
        row = await raw_conn.fetchrow("SELECT status FROM titles WHERE name = $1", "Poster OK")
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)

    edition = await raw_conn.fetchrow(
        """
        SELECT e.poster_key FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        WHERE t.name = $1
        """,
        "Poster OK",
    )
    assert edition["poster_key"] is not None


async def test_poster_extraction_failure_still_ends_active_with_null_poster_key(
    client, raw_conn, clips, monkeypatch
):
    def bad_poster_command(*args, **kwargs):
        raise RuntimeError("poster extraction blew up")

    monkeypatch.setattr(remux_module, "build_poster_command", bad_poster_command)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Poster Fail", slug="poster-fail"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_active():
        row = await raw_conn.fetchrow("SELECT status FROM titles WHERE name = $1", "Poster Fail")
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)

    edition = await raw_conn.fetchrow(
        """
        SELECT e.poster_key FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        WHERE t.name = $1
        """,
        "Poster Fail",
    )
    assert edition["poster_key"] is None


# --- validation ---------------------------------------------------------


async def test_invalid_audio_index_rejected_with_400_and_writes_no_row(
    client, raw_conn, clips
):
    clip = clips["already_correct"]
    before = await raw_conn.fetchval("SELECT count(*) FROM titles")

    resp = await client.post(
        f"/add/{clip['path'].name}",
        data={
            "name": "Bad Audio Index",
            "slug": "bad-audio-index",
            "audio_track": "999",
            "confirm_reuse": "false",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    after = await raw_conn.fetchval("SELECT count(*) FROM titles")
    assert after == before


async def test_duplicate_source_path_add_proceeds_without_a_confirmation_field(
    client, clips
):
    clip = clips["already_correct"]
    r1 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Dup Source A", slug="dup-source-a"),
        follow_redirects=False,
    )
    assert r1.status_code == 302

    # No confirm_duplicate field anywhere in this payload -- the design
    # contract discloses a duplicate via the listing chip and the form
    # banner, and never blocks it (D-48).
    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Dup Source B", slug="dup-source-b"),
        follow_redirects=False,
    )
    assert r2.status_code == 302


# --- concurrency ----------------------------------------------------------


async def test_concurrency_of_one_second_job_waits_for_the_first(monkeypatch):
    order: list[tuple[str, int]] = []

    async def fake_process_add(pool, job):
        order.append(("start", job.title_id))
        await asyncio.sleep(0.2)
        order.append(("end", job.title_id))

    monkeypatch.setattr(queue_module, "process_add", fake_process_add)

    add_queue = AddQueue()
    worker = asyncio.create_task(add_queue.run(pool=None))
    try:
        job1 = AddJob(
            title_id=1, edition_id=uuid.uuid4(), object_key="a.mp4",
            source_rel_path="a.mp4", audio_stream_index=0, channels=2,
        )
        job2 = AddJob(
            title_id=2, edition_id=uuid.uuid4(), object_key="b.mp4",
            source_rel_path="b.mp4", audio_stream_index=0, channels=2,
        )
        await add_queue.enqueue(job1)
        await add_queue.enqueue(job2)

        await asyncio.sleep(0.05)
        assert order == [("start", 1)], "the second job must still be waiting"

        await asyncio.sleep(0.3)
        assert ("end", 1) in order
        assert ("start", 2) in order
        assert order.index(("end", 1)) < order.index(("start", 2))

        await asyncio.sleep(0.3)
        assert ("end", 2) in order
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


# --- event loop stays answerable -------------------------------------


async def test_healthz_stays_responsive_while_upload_is_slow(
    client, clips, storage_spy, monkeypatch
):
    def slow_put(path, key, content_type="video/mp4"):
        time.sleep(2)
        storage_spy["put"].append((path, key, content_type))

    monkeypatch.setattr(queue_module, "put_object_from_file", slow_put)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Slow Upload", slug="slow-upload"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    # Give the worker a moment to reach the (now slow) upload call.
    await asyncio.sleep(0.3)

    t0 = time.monotonic()
    health = await client.get("/healthz")
    elapsed = time.monotonic() - t0

    assert health.status_code == 200
    assert elapsed < 0.5


# --- failure paths (task 3) ------------------------------------------


async def test_upload_failure_marks_failed_with_upload_key_and_aborts_multipart(
    client, raw_conn, clips, storage_spy, monkeypatch, env
):
    def failing_put(path, key, content_type="video/mp4"):
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(queue_module, "put_object_from_file", failing_put)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Upload Fail", slug="upload-fail"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_failed():
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Upload Fail"
        )
        return row is not None and row["status"] == "failed"

    await _wait_until(is_failed)

    title = await raw_conn.fetchrow(
        "SELECT id, failure_reason FROM titles WHERE name = $1", "Upload Fail"
    )
    assert title["failure_reason"] == "failed.reason.upload"

    edition = await raw_conn.fetchrow(
        """
        SELECT e.object_key FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        WHERE s.title_id = $1
        """,
        title["id"],
    )
    assert edition["object_key"] in storage_spy["abort"]

    leftover = [p for p in env["work_dir"].iterdir() if p.is_file()]
    assert leftover == []


async def test_failure_after_successful_upload_deletes_the_completed_object(
    client, raw_conn, clips, storage_spy, monkeypatch
):
    async def failing_set_technical(*args, **kwargs):
        raise RuntimeError("db write exploded after upload succeeded")

    monkeypatch.setattr(queue_module.queries, "set_edition_technical", failing_set_technical)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Post Upload Fail", slug="post-upload-fail"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_failed():
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Post Upload Fail"
        )
        return row is not None and row["status"] == "failed"

    await _wait_until(is_failed)

    title = await raw_conn.fetchrow(
        "SELECT id FROM titles WHERE name = $1", "Post Upload Fail"
    )
    edition = await raw_conn.fetchrow(
        """
        SELECT e.object_key FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        WHERE s.title_id = $1
        """,
        title["id"],
    )
    assert edition["object_key"] in storage_spy["delete"]


async def test_failure_after_poster_upload_deletes_the_poster_key_too(
    client, raw_conn, clips, storage_spy, monkeypatch
):
    async def failing_mark_active(*args, **kwargs):
        raise RuntimeError("flip to active exploded")

    monkeypatch.setattr(queue_module.queries, "mark_title_active", failing_mark_active)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Post Poster Fail", slug="post-poster-fail"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_failed():
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Post Poster Fail"
        )
        return row is not None and row["status"] == "failed"

    await _wait_until(is_failed)

    poster_keys = [key for key in storage_spy["delete"] if key.endswith(".jpg")]
    assert len(poster_keys) == 1


async def test_insufficient_free_space_fails_before_any_ffmpeg_starts(
    client, raw_conn, clips, monkeypatch
):
    monkeypatch.setattr(queue_module.shutil, "disk_usage", lambda path: type(
        "Usage", (), {"free": 0}
    )())

    ffmpeg_calls = []
    original_run_ffmpeg = remux_module.run_ffmpeg

    def spying_run_ffmpeg(*args, **kwargs):
        ffmpeg_calls.append(args)
        return original_run_ffmpeg(*args, **kwargs)

    monkeypatch.setattr(remux_module, "run_ffmpeg", spying_run_ffmpeg)

    clip = clips["needs_reencode"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="No Space", slug="no-space"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_failed():
        row = await raw_conn.fetchrow("SELECT status FROM titles WHERE name = $1", "No Space")
        return row is not None and row["status"] == "failed"

    await _wait_until(is_failed)

    title = await raw_conn.fetchrow(
        "SELECT failure_reason FROM titles WHERE name = $1", "No Space"
    )
    assert title["failure_reason"] == "reason.storage_full"
    assert ffmpeg_calls == []


async def test_healthz_stays_responsive_during_the_failure_cleanup_pair(
    client, clips, storage_spy, monkeypatch
):
    def failing_put(path, key, content_type="video/mp4"):
        raise RuntimeError("upload exploded")

    def slow_abort(key):
        time.sleep(1)
        storage_spy["abort"].append(key)

    def slow_delete(key):
        time.sleep(1)
        storage_spy["delete"].append(key)

    monkeypatch.setattr(queue_module, "put_object_from_file", failing_put)
    monkeypatch.setattr(queue_module, "abort_dangling_multipart", slow_abort)
    monkeypatch.setattr(queue_module, "delete_object", slow_delete)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Slow Cleanup", slug="slow-cleanup"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    await asyncio.sleep(0.3)

    t0 = time.monotonic()
    health = await client.get("/healthz")
    elapsed = time.monotonic() - t0

    assert health.status_code == 200
    assert elapsed < 0.5


# --- startup reconciliation --------------------------------------------


async def _seed_preparing_title(conn, *, name: str, slug: str, started: bool) -> tuple:
    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"

    from orchestrator.db import queries as queries_module

    title_id = await queries_module.create_title_with_edition(
        conn,
        name,
        slug,
        edition_id,
        object_key,
        status="preparing",
        source_path="already-correct.mp4",
        audio_stream_index=1,
        audio_channels=2,
    )
    if started:
        await queries_module.mark_title_started(conn, title_id)
    return title_id, edition_id, object_key


async def test_startup_sweep_marks_a_started_row_interrupted_and_cleans_up(
    raw_conn, storage_spy, env
):
    from orchestrator.db import queries as queries_module
    from orchestrator.main import _sweep_preparing_titles

    title_id, edition_id, object_key = await _seed_preparing_title(
        raw_conn, name="Interrupted Row", slug="interrupted-row", started=True
    )

    class _SingleAcquireCtx:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *exc_info):
            return False

    class _SinglePoolLike:
        def __init__(self, conn):
            self._conn = conn

        def acquire(self):
            return _SingleAcquireCtx(self._conn)

    add_queue = AddQueue()
    await _sweep_preparing_titles(_SinglePoolLike(raw_conn), add_queue)

    row = await queries_module.resolve_slug(raw_conn, "interrupted-row")
    assert row["status"] == "failed"

    title = await raw_conn.fetchrow(
        "SELECT failure_reason FROM titles WHERE id = $1", title_id
    )
    assert title["failure_reason"] == "failed.reason.interrupted"
    assert object_key in storage_spy["abort"]
    assert object_key in storage_spy["delete"]
    assert add_queue._queue.empty()


async def test_startup_sweep_re_enqueues_a_never_started_row_without_touching_status(
    raw_conn, env
):
    from orchestrator.main import _sweep_preparing_titles

    title_id, edition_id, object_key = await _seed_preparing_title(
        raw_conn, name="Queued Row", slug="queued-row", started=False
    )

    class _SingleAcquireCtx:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *exc_info):
            return False

    class _SinglePoolLike:
        def __init__(self, conn):
            self._conn = conn

        def acquire(self):
            return _SingleAcquireCtx(self._conn)

    add_queue = AddQueue()
    await _sweep_preparing_titles(_SinglePoolLike(raw_conn), add_queue)

    row = await raw_conn.fetchrow("SELECT status FROM titles WHERE id = $1", title_id)
    assert row["status"] == "preparing"

    assert not add_queue._queue.empty()
    job = add_queue._queue.get_nowait()
    assert job.title_id == title_id
    assert job.object_key == object_key


async def test_a_source_path_that_escapes_the_share_fails_the_row_instead_of_stranding_it(
    raw_conn, storage_spy, env
):
    """A rejected path is resolved after the row is already 'preparing' and
    started, so it has to fail the row like any other error -- an exception
    escaping process_add leaves "Working..." on screen forever and keeps
    counting against every later add's queue position.
    """
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Escaping Source",
        "escaping-source",
        edition_id,
        object_key,
        status="preparing",
        source_path="../outside-the-share.mp4",
        audio_stream_index=1,
        audio_channels=2,
    )

    class _SingleAcquireCtx:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *exc_info):
            return False

    class _SinglePoolLike:
        def __init__(self, conn):
            self._conn = conn

        def acquire(self):
            return _SingleAcquireCtx(self._conn)

    pool = _SinglePoolLike(raw_conn)
    job_row = await queries_module.job_row(raw_conn, title_id)
    await queue_module.process_add(pool, AddJob.from_row(title_id, job_row))

    title = await raw_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title_id
    )
    assert title["status"] == "failed"
    assert title["failure_reason"] == "reason.unreadable"


async def test_the_recorded_size_is_the_uploaded_object_not_the_source(
    client, raw_conn, clips, storage_spy, monkeypatch
):
    """`editions.size_bytes` is a catalog fact about the stored object, and
    the library row shows it as the video's size. In the remux branch the
    uploaded file is ffmpeg's output -- a different container layout, and
    materially different once the audio has been re-encoded -- so recording
    the source's size makes the figure wrong for every remuxed edition.
    """
    import os

    sizes = {}

    def sizing_put(path, key, content_type="video/mp4"):
        sizes[key] = os.stat(path).st_size
        storage_spy["put"].append((path, key, content_type))

    monkeypatch.setattr(queue_module, "put_object_from_file", sizing_put)

    clip = clips["needs_reencode"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Sized Clip", slug="sized-clip"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_active():
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Sized Clip"
        )
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)

    edition = await raw_conn.fetchrow(
        """
        SELECT e.object_key, e.size_bytes FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        WHERE t.name = $1
        """,
        "Sized Clip",
    )
    uploaded_size = sizes[edition["object_key"]]

    assert edition["size_bytes"] == uploaded_size
    # And the bug is real on this clip rather than hidden by a coincidence:
    # the remuxed artifact is genuinely a different size from the source.
    assert uploaded_size != clip["path"].stat().st_size
