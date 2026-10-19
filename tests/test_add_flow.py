"""End-to-end coverage of preparing -> active and preparing -> failed,
against a real Postgres (testcontainers) and real
ffmpeg/ffprobe on small synthetic clips built once per module -- never
hand-typed JSON.

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
from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN
from httpx import ASGITransport

from mytube_media import remux as remux_module
from mytube_media.compat import audio_tracks
from mytube_media.compat import probe as ffprobe_probe
from orchestrator.config import get_settings
from orchestrator.main import app
from orchestrator.media import queue as queue_module
from orchestrator.media.add import AudioChoiceRefused, parse_audio_choice
from orchestrator.media.queue import AddJob

pytestmark = pytest.mark.integration


def _run_ffmpeg(args):
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """Two small, real H.264 clips, one second long -- one already fully
    compliant (AAC, faststart), one whose audio needs re-encoding (mp3).
    Built once per module with the real local ffmpeg/ffprobe, the same
    "capture from a real run, never hand-type" discipline the
    compatibility fixtures already follow.
    """
    root = tmp_path_factory.mktemp("clips")
    already_correct = root / "already-correct.mp4"
    needs_reencode = root / "needs-reencode.mp4"

    common = [
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-pix_fmt",
        "yuv420p",
    ]
    _run_ffmpeg(
        [
            *common,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(already_correct),
        ]
    )
    _run_ffmpeg(
        [
            *common,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(needs_reencode),
        ]
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
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(share_root))
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield {"share_root": share_root, "work_dir": work_dir}
    get_settings.cache_clear()


@pytest.fixture
def storage_spy(monkeypatch):
    """Every storage call the addition consumer makes, recorded instead
    of reaching a real bucket -- patched at the name the caller actually
    imported, not at `orchestrator.storage` itself (a `from x import y`
    binds its own reference; patching the origin module leaves the
    caller's copy untouched).

    Only `orchestrator.media.queue` needs this any more: the startup
    sweep this fixture used to patch a second copy for
    (`orchestrator.main`) is gone -- restart recovery no longer
    makes its own storage calls at all, it just lets a stale heartbeat
    put the row back in the queue for this same consumer to pick up
    again.
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
    return calls


@pytest.fixture
async def client(env, storage_spy, signed_in):
    """Runs the real lifespan (real Postgres pool, the real addition and
    assembly consumers, the real reaper) against an in-memory ASGI
    transport -- the only way to exercise the "the row is written, the
    response returns, and a background task does the rest" shape for
    real.

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


def _post_data(
    clip: dict, *, name: str, slug: str, confirm_reuse: str = "false"
) -> dict:
    return {
        "name": name,
        "slug": slug,
        "audio_choice": f"embedded:{clip['audio_index']}",
        "confirm_reuse": confirm_reuse,
    }


# --- happy path -------------------------------------------------------


async def test_post_add_returns_redirect_with_preparing_row_and_null_started_at(
    client, raw_conn, clips, monkeypatch
):
    """The claim under test -- "the row is written and the response
    returns before the background work does anything" -- is a real
    design property, but asserting it by racing a wall-clock
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
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Drain Clip"
        )
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)
    elapsed = time.monotonic() - start
    assert elapsed < 60, "the control-clip-sized add must land in under a minute"

    title = await raw_conn.fetchrow(
        "SELECT id, started_at FROM titles WHERE name = $1", "Drain Clip"
    )
    assert title["started_at"] is not None

    # Exactly one season, one episode, one edition for the film --
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
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Poster OK"
        )
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
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Poster Fail"
        )
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


# --- parse_audio_choice -----------------------------------------------


def test_parse_audio_choice_embedded_returns_the_absolute_stream_index():
    assert parse_audio_choice("embedded:2") == ("embedded", 2, None)


def test_parse_audio_choice_external_returns_the_share_relative_path():
    assert parse_audio_choice("external:RUS Sound/AniStar/dub_05.mka") == (
        "external",
        None,
        "RUS Sound/AniStar/dub_05.mka",
    )


def test_parse_audio_choice_refuses_an_absolute_unix_path():
    with pytest.raises(AudioChoiceRefused):
        parse_audio_choice("external:/etc/passwd")


def test_parse_audio_choice_refuses_a_windows_drive_absolute_path():
    with pytest.raises(AudioChoiceRefused):
        parse_audio_choice("external:C:/dub.mka")


def test_parse_audio_choice_refuses_a_value_with_no_separator():
    with pytest.raises(AudioChoiceRefused):
        parse_audio_choice("2")


def test_parse_audio_choice_refuses_a_non_numeric_embedded_index():
    with pytest.raises(AudioChoiceRefused):
        parse_audio_choice("embedded:not-a-number")


def test_parse_audio_choice_refuses_an_empty_external_path():
    with pytest.raises(AudioChoiceRefused):
        parse_audio_choice("external:")


def test_parse_audio_choice_never_touches_the_filesystem(monkeypatch):
    """`parse_audio_choice` shapes the value only -- containment and
    existence are the caller's job. Proven by making every
    filesystem entry point explode and confirming a normal call still
    answers normally.
    """
    import os

    def _boom(*args, **kwargs):
        raise AssertionError("parse_audio_choice touched the filesystem")

    monkeypatch.setattr("builtins.open", _boom)
    monkeypatch.setattr(os, "stat", _boom)
    monkeypatch.setattr(os.path, "exists", _boom)

    assert parse_audio_choice("embedded:2") == ("embedded", 2, None)
    assert parse_audio_choice("external:Subs/dub.mka") == (
        "external",
        None,
        "Subs/dub.mka",
    )


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
            "audio_choice": "embedded:999",
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
    # banner, and never blocks it.
    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Dup Source B", slug="dup-source-b"),
        follow_redirects=False,
    )
    assert r2.status_code == 302


# --- attaching an episode, adding an edition --------------------------


@pytest.fixture
def gated_worker(monkeypatch):
    """Freeze the addition consumer before it touches anything, the same
    determinism `test_post_add_returns_redirect_with_preparing_row_and_
    null_started_at` needs and for the identical reason: these tests
    assert on the *rows the route itself wrote*, not on what the
    consumer does with them afterwards, and asyncio can otherwise run the
    consumer to completion during the same `await client.post(...)`.
    """
    worker_may_proceed = asyncio.Event()
    real_process_add = queue_module.process_add

    async def gated_process_add(pool, job):
        await worker_may_proceed.wait()
        await real_process_add(pool, job)

    monkeypatch.setattr(queue_module, "process_add", gated_process_add)
    yield
    worker_may_proceed.set()


async def _episode_row(conn, title_id, number):
    return await conn.fetchrow(
        """
        SELECT ep.id AS episode_id, ep.default_edition_id AS default_edition_id
        FROM episode_number_history enh
        JOIN episodes ep ON ep.id = enh.episode_id
        WHERE enh.title_id = $1 AND enh.number = $2 AND enh.is_current
        """,
        title_id,
        number,
    )


async def test_attach_branch_creates_a_new_episode_under_the_existing_title(
    client, raw_conn, clips, gated_worker
):
    clip = clips["already_correct"]
    r1 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Attach Target Show", slug="attach-target-show"),
        follow_redirects=False,
    )
    assert r1.status_code == 302
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "Attach Target Show"
    )
    episode_count_before = await raw_conn.fetchval(
        "SELECT count(*) FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    assert episode_count_before == 1

    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_title_id": str(title_id),
            "episode_number": "5",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 302

    episode_count_after = await raw_conn.fetchval(
        "SELECT count(*) FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    assert episode_count_after == 2
    row = await _episode_row(raw_conn, title_id, 5)
    assert row is not None
    assert row["default_edition_id"] is not None
    # No second title was created for the attach branch.
    title_count = await raw_conn.fetchval(
        "SELECT count(*) FROM titles WHERE name = 'Attach Target Show'"
    )
    assert title_count == 1


async def test_attach_branch_refuses_an_already_taken_number(
    client, raw_conn, clips, gated_worker
):
    clip = clips["already_correct"]
    r1 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Taken Number Show", slug="taken-number-show"),
        follow_redirects=False,
    )
    assert r1.status_code == 302
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "Taken Number Show"
    )
    episode_count_before = await raw_conn.fetchval(
        "SELECT count(*) FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )

    # Episode 1 already exists (create_title_with_edition's own default).
    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_title_id": str(title_id),
            "episode_number": "1",
        },
        follow_redirects=False,
    )

    assert r2.status_code == 400
    episode_count_after = await raw_conn.fetchval(
        "SELECT count(*) FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    assert episode_count_after == episode_count_before


async def test_attach_branch_refuses_a_title_that_does_not_exist(
    client, raw_conn, clips
):
    clip = clips["already_correct"]
    before = await raw_conn.fetchval("SELECT count(*) FROM titles")

    resp = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_title_id": str(uuid.uuid4()),
            "episode_number": "1",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    after = await raw_conn.fetchval("SELECT count(*) FROM titles")
    assert after == before


async def test_edition_branch_creates_a_new_edition_without_touching_the_default(
    client, raw_conn, clips, gated_worker
):
    clip = clips["already_correct"]
    r1 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Edition Target Show", slug="edition-target-show"),
        follow_redirects=False,
    )
    assert r1.status_code == 302
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "Edition Target Show"
    )
    episode_row = await raw_conn.fetchrow(
        "SELECT ep.id AS episode_id, ep.default_edition_id AS default_edition_id "
        "FROM episodes ep JOIN seasons s ON s.id = ep.season_id WHERE s.title_id = $1",
        title_id,
    )
    original_default = episode_row["default_edition_id"]

    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_episode_id": str(episode_row["episode_id"]),
            "edition_slug": "dub-ru",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 302

    editions = await raw_conn.fetch(
        "SELECT id, slug FROM editions WHERE episode_id = $1", episode_row["episode_id"]
    )
    assert len(editions) == 2
    assert {e["slug"] for e in editions} == {None, "dub-ru"}

    default_after = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1",
        episode_row["episode_id"],
    )
    assert default_after == original_default


async def test_edition_branch_with_make_default_stores_the_request_on_the_job(
    client, raw_conn, clips, gated_worker
):
    clip = clips["already_correct"]
    r1 = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Make Default Show", slug="make-default-show"),
        follow_redirects=False,
    )
    assert r1.status_code == 302
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "Make Default Show"
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )

    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_episode_id": str(episode_id),
            "edition_slug": "dub-ru",
            "make_default": "1",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 302

    job = await raw_conn.fetchrow(
        "SELECT make_default_when_ready FROM jobs WHERE title_id = $1 "
        "ORDER BY created_at DESC LIMIT 1",
        title_id,
    )
    assert job["make_default_when_ready"] is True
    # Stored, not applied: the pointer has not moved yet.
    default_still = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    editions = await raw_conn.fetch(
        "SELECT id, slug FROM editions WHERE episode_id = $1", episode_id
    )
    new_edition_id = next(e["id"] for e in editions if e["slug"] == "dub-ru")
    assert default_still != new_edition_id


async def test_edition_branch_refuses_an_already_taken_slug(
    client, raw_conn, clips, gated_worker
):
    clip = clips["already_correct"]
    await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Taken Slug Show", slug="taken-slug-show"),
        follow_redirects=False,
    )
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "Taken Slug Show"
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    r2 = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_episode_id": str(episode_id),
            "edition_slug": "dub-ru",
        },
        follow_redirects=False,
    )
    assert r2.status_code == 302
    count_before = await raw_conn.fetchval(
        "SELECT count(*) FROM editions WHERE episode_id = $1", episode_id
    )

    r3 = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_episode_id": str(episode_id),
            "edition_slug": "dub-ru",
        },
        follow_redirects=False,
    )

    assert r3.status_code == 400
    count_after = await raw_conn.fetchval(
        "SELECT count(*) FROM editions WHERE episode_id = $1", episode_id
    )
    assert count_after == count_before


async def test_edition_branch_refuses_an_all_digit_slug(
    client, raw_conn, clips, gated_worker
):
    clip = clips["already_correct"]
    await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="All Digit Slug Show", slug="all-digit-slug-show"),
        follow_redirects=False,
    )
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "All Digit Slug Show"
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )

    resp = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_episode_id": str(episode_id),
            "edition_slug": "720",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    count = await raw_conn.fetchval(
        "SELECT count(*) FROM editions WHERE episode_id = $1", episode_id
    )
    assert count == 1


async def test_edition_branch_refuses_an_episode_that_does_not_exist(
    client, raw_conn, clips
):
    clip = clips["already_correct"]
    before = await raw_conn.fetchval("SELECT count(*) FROM editions")

    resp = await client.post(
        f"/add/{clip['path'].name}",
        data={
            **_post_data(clip, name="ignored", slug="ignored"),
            "target_episode_id": str(uuid.uuid4()),
            "edition_slug": "dub-ru",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    after = await raw_conn.fetchval("SELECT count(*) FROM editions")
    assert after == before


# --- a job's terminal write lands on the edition, the title aggregates --


async def _title_status(conn, title_id) -> str:
    return await conn.fetchval("SELECT status FROM titles WHERE id = $1", title_id)


async def _edition_status(conn, edition_id) -> str:
    return await conn.fetchval("SELECT status FROM editions WHERE id = $1", edition_id)


async def test_single_file_add_still_ends_active_through_the_aggregate_rule(raw_conn):
    """The one-episode path is unchanged:
    for a title with exactly one episode, `sync_title_status` reduces to
    the identical unconditional `mark_title_active` write this used to
    be, since the two facts are the same by construction.
    """
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Single File Aggregate Show",
        "single-file-aggregate-show",
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )

    await queries_module.mark_edition_active(raw_conn, edition_id)
    await queries_module.sync_title_status(raw_conn, title_id)

    assert await _title_status(raw_conn, title_id) == "active"


async def test_aggregate_status_title_is_active_from_its_first_ready_episode(raw_conn):
    """A sibling episode still 'preparing' does not hold the title back
    once one of its episodes is ready -- and does not flip it to
    'failed' either."""
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Aggregate Ready Show",
        "aggregate-ready-show",
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    sibling_edition_id = uuid.uuid4()
    await queries_module.attach_episode_to_title(
        raw_conn,
        title_id,
        2,
        sibling_edition_id,
        f"{sibling_edition_id}.mp4",
        status="preparing",
    )

    await queries_module.mark_edition_active(raw_conn, edition_id)
    await queries_module.sync_title_status(raw_conn, title_id)

    assert await _title_status(raw_conn, title_id) == "active"
    # The still-preparing sibling is untouched.
    assert await _edition_status(raw_conn, sibling_edition_id) == "preparing"


async def test_sibling_edition_failure_does_not_flip_an_already_active_title(raw_conn):
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Sibling Failure Show",
        "sibling-failure-show",
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    sibling_edition_id = uuid.uuid4()
    await queries_module.attach_episode_to_title(
        raw_conn,
        title_id,
        2,
        sibling_edition_id,
        f"{sibling_edition_id}.mp4",
        status="preparing",
    )
    await queries_module.mark_edition_active(raw_conn, edition_id)
    await queries_module.sync_title_status(raw_conn, title_id)
    assert await _title_status(raw_conn, title_id) == "active"

    await queries_module.mark_edition_failed(
        raw_conn, sibling_edition_id, "failed.reason.unknown"
    )
    await queries_module.sync_title_status(
        raw_conn, title_id, reason_key="failed.reason.unknown"
    )

    assert await _title_status(raw_conn, title_id) == "active"
    assert await _edition_status(raw_conn, sibling_edition_id) == "failed"


async def test_aggregate_status_title_fails_only_once_every_episode_has_failed(
    raw_conn,
):
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "All Failed Show",
        "all-failed-show",
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    sibling_edition_id = uuid.uuid4()
    await queries_module.attach_episode_to_title(
        raw_conn,
        title_id,
        2,
        sibling_edition_id,
        f"{sibling_edition_id}.mp4",
        status="preparing",
    )

    await queries_module.mark_edition_failed(
        raw_conn, edition_id, "failed.reason.unknown"
    )
    await queries_module.sync_title_status(
        raw_conn, title_id, reason_key="failed.reason.unknown"
    )
    # One down, one still preparing -- not failed yet.
    assert await _title_status(raw_conn, title_id) == "preparing"

    await queries_module.mark_edition_failed(
        raw_conn, sibling_edition_id, "failed.reason.unknown"
    )
    await queries_module.sync_title_status(
        raw_conn, title_id, reason_key="failed.reason.unknown"
    )

    assert await _title_status(raw_conn, title_id) == "failed"


async def test_default_pointer_moves_for_a_ready_edition_that_asked_for_it(raw_conn):
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Default Pointer Ready Show",
        "default-pointer-ready-show",
        edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    new_edition_id = uuid.uuid4()
    await queries_module.add_edition_to_episode(
        raw_conn,
        episode_id,
        "dub-ru",
        new_edition_id,
        f"{new_edition_id}.mp4",
        status="preparing",
    )

    await queries_module.mark_edition_active(raw_conn, new_edition_id)
    await queries_module.set_default_edition(raw_conn, new_edition_id)

    default_now = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    assert default_now == new_edition_id


async def test_default_pointer_does_not_move_for_a_failed_edition(raw_conn):
    """The route stores the request on the job; the terminal write only
    ever calls `set_default_edition` on the *done* path -- a
    failed edition's own request is simply never acted on.
    """
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Default Pointer Failed Show",
        "default-pointer-failed-show",
        edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    new_edition_id = uuid.uuid4()
    await queries_module.add_edition_to_episode(
        raw_conn,
        episode_id,
        "dub-ru",
        new_edition_id,
        f"{new_edition_id}.mp4",
        status="preparing",
    )

    # The job failed -- mark_edition_failed runs, set_default_edition never
    # does (this is the terminal helper's own branch, exercised directly
    # here rather than through a real converter failure).
    await queries_module.mark_edition_failed(
        raw_conn, new_edition_id, "failed.reason.unknown"
    )

    default_still = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    assert default_still == edition_id


async def test_cancelled_single_file_add_leaves_both_levels_consistent(
    client, raw_conn, clips, monkeypatch
):
    """The existing cancellation path (a title whose address never went
    live is hard-deleted with no tombstone) is unaffected by the
    readiness changes. `claim_job` itself is stubbed out for
    the duration of this test -- unlike gating `process_add`, this keeps
    the job genuinely 'queued' (never claimed at all) while the request
    is cancelled, deterministic rather than a race against the real
    consumer.
    """

    async def _never_claims(conn, *, kind, worker_name):
        return None

    monkeypatch.setattr(queue_module.queries, "claim_job", _never_claims)

    resp = await client.post(
        f"/add/{clips['already_correct']['path'].name}",
        data=_post_data(
            clips["already_correct"],
            name="Cancel Consistency Show",
            slug="cancel-consistency-show",
        ),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    title_id = await raw_conn.fetchval(
        "SELECT id FROM titles WHERE name = $1", "Cancel Consistency Show"
    )
    from orchestrator.db import queries as queries_module

    job = await queries_module.job_for_title(raw_conn, title_id)
    result = await queries_module.request_job_cancel(raw_conn, job["id"])
    if result is not None and result["status"] == "cancelled":
        try:
            await queries_module.delete_never_active_title(raw_conn, title_id)
        except (LookupError, ValueError):
            pass

    title_row = await raw_conn.fetchrow("SELECT id FROM titles WHERE id = $1", title_id)
    episode_row = await raw_conn.fetchrow(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    # Both levels agree: neither the title nor its one episode survives a
    # cancellation that never went live.
    assert title_row is None
    assert episode_row is None


# --- plan_edition_slug / suggest_edition_slug -------------------------


def test_suggest_edition_slug_returns_dub_prefixed_slug_for_a_differing_language():
    from orchestrator.media.add import suggest_edition_slug

    assert (
        suggest_edition_slug(
            existing={"audio_language": "ja"}, candidate={"audio_language": "ru"}
        )
        == "dub-ru"
    )


def test_suggest_edition_slug_appends_the_studio_for_a_second_dub_of_the_same_language():
    from orchestrator.media.add import suggest_edition_slug

    slug = suggest_edition_slug(
        existing={"audio_language": "ja"},
        candidate={"audio_language": "ru", "audio_studio_folder": "Dublyazhnaya"},
        taken_audio_languages=frozenset({"ru"}),
    )
    assert slug == "dub-ru-dublyazhnaya"


def test_suggest_edition_slug_returns_resolution_for_a_differing_resolution():
    from orchestrator.media.add import suggest_edition_slug

    slug = suggest_edition_slug(
        existing={"width": 1920, "height": 1080},
        candidate={"width": 1280, "height": 720},
    )
    assert slug == "720p"


def test_suggest_edition_slug_returns_sub_prefixed_slug_for_a_differing_subtitle():
    from orchestrator.media.add import suggest_edition_slug

    slug = suggest_edition_slug(
        existing={"subtitle_language": None},
        candidate={"subtitle_language": "ru"},
    )
    assert slug == "sub-ru"


def test_suggest_edition_slug_appends_signs_for_a_signs_only_subtitle():
    from orchestrator.media.add import suggest_edition_slug

    slug = suggest_edition_slug(
        existing={"subtitle_language": None},
        candidate={"subtitle_language": "ru", "subtitle_signs_only": True},
    )
    assert slug == "sub-ru-signs"


def test_suggest_edition_slug_returns_the_codec_for_a_conversion_against_an_as_is_default():
    from orchestrator.media.add import suggest_edition_slug

    slug = suggest_edition_slug(
        existing={"is_transcode": False},
        candidate={"is_transcode": True, "video_codec": "h264"},
    )
    assert slug == "h264"


def test_suggest_edition_slug_returns_empty_string_when_nothing_differs():
    from orchestrator.media.add import suggest_edition_slug

    assert suggest_edition_slug(existing={"audio_language": "ja"}, candidate={}) == ""


async def test_plan_edition_slug_refuses_an_all_digit_candidate(raw_conn, clips):
    from orchestrator.db import queries as queries_module
    from orchestrator.media.add import plan_edition_slug
    from orchestrator.slugs import EditionSlugRefused

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Plan Edition Slug Show",
        "plan-edition-slug-show",
        edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )

    with pytest.raises(EditionSlugRefused):
        await plan_edition_slug(raw_conn, episode_id, "720")


async def test_plan_edition_slug_refuses_a_slug_already_on_the_episode(raw_conn):
    from orchestrator.db import queries as queries_module
    from orchestrator.media.add import EditionSlugTaken, plan_edition_slug

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Plan Edition Taken Show",
        "plan-edition-taken-show",
        edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await raw_conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )
    second_edition_id = uuid.uuid4()
    await queries_module.add_edition_to_episode(
        raw_conn, episode_id, "dub-ru", second_edition_id, f"{second_edition_id}.mp4"
    )

    with pytest.raises(EditionSlugTaken):
        await plan_edition_slug(raw_conn, episode_id, "dub-ru")


# --- concurrency ------------------------------------------------------
#
# See `test_two_queued_jobs_run_one_after_the_other_not_at_once`, below
# the durable-queue helpers, for what replaced the old in-memory-queue
# version of this test.


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

    # Give the consumer a moment to poll, claim the row and reach the
    # (now slow) upload call -- comfortably more than
    # queue.CONSUMER_POLL_INTERVAL_SECONDS' own worst case (a job
    # inserted the instant after a poll finds nothing empty-handed waits
    # a full interval for the next one).
    await asyncio.sleep(1.0)

    t0 = time.monotonic()
    health = await client.get("/healthz")
    elapsed = time.monotonic() - t0

    assert health.status_code == 200
    assert elapsed < 0.5


async def test_more_than_one_heartbeat_covers_an_upload_that_outlasts_the_interval(
    client, clips, storage_spy, monkeypatch, raw_conn
):
    """A transfer that runs longer than one heartbeat interval is
    still heartbeating throughout it, not just once before it starts --
    without this, the reaper's own stale sweep can requeue the job out
    from under an upload that is still healthily in flight.
    """
    heartbeat_stages = []
    real_heartbeat_job = queue_module.queries.heartbeat_job

    async def spy_heartbeat_job(conn, job_id, **kwargs):
        heartbeat_stages.append(kwargs.get("stage"))
        return await real_heartbeat_job(conn, job_id, **kwargs)

    monkeypatch.setattr(queue_module.queries, "heartbeat_job", spy_heartbeat_job)
    monkeypatch.setattr(queue_module, "HEARTBEAT_WHILE_INTERVAL_SECONDS", 0.05)

    def slow_put(path, key, content_type="video/mp4"):
        time.sleep(0.3)
        storage_spy["put"].append((path, key, content_type))

    monkeypatch.setattr(queue_module, "put_object_from_file", slow_put)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Heartbeating Upload", slug="heartbeating-upload"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    async def is_active():
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "Heartbeating Upload"
        )
        return row is not None and row["status"] == "active"

    await _wait_until(is_active)

    uploading = [stage for stage in heartbeat_stages if stage == "uploading"]
    assert len(uploading) > 1


# --- failure paths ----------------------------------------------------


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

    monkeypatch.setattr(
        queue_module.queries, "set_edition_technical", failing_set_technical
    )

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


async def test_a_superseded_terminal_write_leaves_the_title_untouched(
    client, raw_conn, clips, storage_spy, monkeypatch
):
    """The add consumer's half: when the terminal write reports
    the attempt was superseded (a late report against a job the reaper
    has already requeued or finished under a different attempt), the
    consumer must not flip the title active, write the edition's
    technical detail or its poster -- against a title another attempt may
    now own.
    """
    active_calls = []
    real_mark_active = queue_module.queries.mark_title_active

    async def spy_mark_active(*args, **kwargs):
        active_calls.append((args, kwargs))
        return await real_mark_active(*args, **kwargs)

    async def superseded_finish_job(*args, **kwargs):
        return False

    monkeypatch.setattr(queue_module.queries, "mark_title_active", spy_mark_active)
    monkeypatch.setattr(queue_module.queries, "finish_job", superseded_finish_job)

    done_event = asyncio.Event()
    real_process_add = queue_module.process_add

    async def gated_process_add(pool, job):
        await real_process_add(pool, job)
        done_event.set()

    monkeypatch.setattr(queue_module, "process_add", gated_process_add)

    clip = clips["already_correct"]
    resp = await client.post(
        f"/add/{clip['path'].name}",
        data=_post_data(clip, name="Superseded Done", slug="superseded-done"),
        follow_redirects=False,
    )
    assert resp.status_code == 302

    await asyncio.wait_for(done_event.wait(), timeout=30.0)

    assert active_calls == [], "a superseded write must never flip the title active"
    row = await raw_conn.fetchrow(
        "SELECT status FROM titles WHERE name = $1", "Superseded Done"
    )
    assert row["status"] == "preparing"


async def test_insufficient_free_space_fails_before_any_ffmpeg_starts(
    client, raw_conn, clips, monkeypatch
):
    monkeypatch.setattr(
        queue_module.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 0})()
    )

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
        row = await raw_conn.fetchrow(
            "SELECT status FROM titles WHERE name = $1", "No Space"
        )
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

    # Same margin as test_healthz_stays_responsive_while_upload_is_slow --
    # comfortably more than one full poll interval.
    await asyncio.sleep(1.0)

    t0 = time.monotonic()
    health = await client.get("/healthz")
    elapsed = time.monotonic() - t0

    assert health.status_code == 200
    assert elapsed < 0.5


# --- restart recovery, now the reaper's job ---------------------------
#
# There is no startup sweep to test in this file any more:
# `orchestrator.main._sweep_preparing_titles` is gone, along with the
# in-memory `AddQueue` it re-enqueued onto. Restart recovery for a job
# whose heartbeat goes stale is `orchestrator.reaper.sweep_stale_jobs`'s
# job now, exercised end to end (real two-connection claim races
# included) by tests/test_jobs_queue.py and tests/test_reaper_playlists.py
# -- unrelated to anything this module still owns.


class _SingleAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class _SinglePoolLike:
    """The `.acquire()` shape `process_add`/`run_add_consumer` ask an
    `asyncpg.Pool` for, wrapping one already-open `raw_conn` -- used by
    every test below that drives `process_add` or `run_add_consumer`
    directly rather than through the full `client` fixture's real
    lifespan-managed pool.
    """

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _SingleAcquireCtx(self._conn)


async def _claim_one(conn, *, kind: str = "remux") -> AddJob:
    """Claim the oldest queued `remux` job for the fixed test worker
    identity and build the `AddJob` `process_add` expects -- the same two
    calls `run_add_consumer` itself makes, spelled out here so a test can
    drive `process_add` on a specific row without racing a live consumer
    loop for it.
    """
    from orchestrator.db import queries as queries_module

    job_row = await queries_module.claim_job(
        conn, kind=kind, worker_name=queue_module.ADD_CONSUMER_WORKER_NAME
    )
    assert job_row is not None, "nothing was queued to claim"
    edition_row = await queries_module.edition_for_remux(conn, job_row["edition_id"])
    return AddJob.from_row(job_row, edition_row)


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
    await queries_module.enqueue_job(
        raw_conn,
        kind="remux",
        title_id=title_id,
        edition_id=edition_id,
        source_path="../outside-the-share.mp4",
        audio_stream_index=1,
    )

    pool = _SinglePoolLike(raw_conn)
    job = await _claim_one(raw_conn)
    await queue_module.process_add(pool, job)

    title = await raw_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title_id
    )
    assert title["status"] == "failed"
    assert title["failure_reason"] == "reason.unreadable"

    job_after = await raw_conn.fetchrow(
        "SELECT status FROM jobs WHERE id = $1", job.job_id
    )
    assert job_after["status"] == "failed"


async def test_two_queued_jobs_run_one_after_the_other_not_at_once(
    raw_conn, env, monkeypatch
):
    """Concurrency of one is now a property of `run_add_consumer`'s own
    claiming loop rather than of an `asyncio.Queue` with one reader --
    this is the durable-queue equivalent of the old
    `test_concurrency_of_one_second_job_waits_for_the_first`.
    """
    from orchestrator.db import queries as queries_module

    order: list[tuple[str, object]] = []

    async def fake_process_add(pool, job):
        order.append(("start", job.title_id))
        await asyncio.sleep(0.2)
        order.append(("end", job.title_id))

    monkeypatch.setattr(queue_module, "process_add", fake_process_add)

    async def _seed(name: str, slug: str):
        edition_id = uuid.uuid4()
        title_id = await queries_module.create_title_with_edition(
            raw_conn,
            name,
            slug,
            edition_id,
            f"{edition_id}.mp4",
            status="preparing",
            source_path="already-correct.mp4",
            audio_stream_index=1,
            audio_channels=2,
        )
        await queries_module.enqueue_job(
            raw_conn,
            kind="remux",
            title_id=title_id,
            edition_id=edition_id,
            source_path="already-correct.mp4",
            audio_stream_index=1,
        )
        return title_id

    title1 = await _seed("Concurrency First", "concurrency-first")
    await asyncio.sleep(0.01)  # a distinct created_at, so claim order is pinned
    title2 = await _seed("Concurrency Second", "concurrency-second")

    pool = _SinglePoolLike(raw_conn)
    worker = asyncio.create_task(
        queue_module.run_add_consumer(pool, poll_interval=0.02)
    )
    try:
        await asyncio.sleep(0.05)
        assert order == [("start", title1)], "the second job must still be waiting"

        await asyncio.sleep(0.3)
        assert ("end", title1) in order
        assert ("start", title2) in order
        assert order.index(("end", title1)) < order.index(("start", title2))

        await asyncio.sleep(0.3)
        assert ("end", title2) in order
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


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


# --- a restart mid-addition, without a live dev stack to restart -------


async def test_a_restart_mid_remux_leaves_the_row_in_flight_rather_than_failed(
    raw_conn, env
):
    """The property this plan exists to change, proven against the
    real durable table rather than a real process restart -- this project
    no longer runs a local dev stand to kill and bring back (`.claude
    /CLAUDE.md`: the workstation stand is retired, the service only runs
    in the owner's k3s cluster). What a restart actually does to a
    process is stop it from writing anything further; that is reproduced
    directly here by claiming a job, marking its title started (the point
    a real consumer would have reached before a mid-remux crash), and then
    never calling `process_add` again -- exactly what a killed process
    leaves behind.
    """
    from orchestrator import reaper as reaper_module
    from orchestrator.db import queries as queries_module

    edition_id = uuid.uuid4()
    title_id = await queries_module.create_title_with_edition(
        raw_conn,
        "Restart Mid Remux",
        "restart-mid-remux",
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
        source_path="already-correct.mp4",
        audio_stream_index=1,
        audio_channels=2,
    )
    await queries_module.enqueue_job(
        raw_conn,
        kind="remux",
        title_id=title_id,
        edition_id=edition_id,
        source_path="already-correct.mp4",
        audio_stream_index=1,
    )

    job = await _claim_one(raw_conn)
    await queries_module.mark_title_started(raw_conn, title_id)

    # The process dies here -- no heartbeat, no finish, nothing else is
    # ever written for this attempt. Simulated by backdating the row's
    # own heartbeat past the reaper's stale threshold rather than by
    # waiting JOB_STALE_SECONDS (180s) in real time.
    await raw_conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
        job.job_id,
    )

    # The property under test: nothing about a stuck heartbeat marks the
    # title failed. It is still exactly where the operator last saw it,
    # "Working...".
    title_before_sweep = await raw_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title_id
    )
    assert title_before_sweep["status"] == "preparing"
    assert title_before_sweep["failure_reason"] is None

    class _AcquireCtx:
        async def __aenter__(self):
            return raw_conn

        async def __aexit__(self, *exc_info):
            return False

    class _PoolLike:
        def acquire(self):
            return _AcquireCtx()

    await reaper_module.sweep_stale_jobs(_PoolLike())

    job_after_sweep = await raw_conn.fetchrow(
        "SELECT status, stage, worker_name, heartbeat_at, attempts FROM jobs WHERE id = $1",
        job.job_id,
    )
    assert job_after_sweep["status"] == "queued"
    assert job_after_sweep["stage"] == "queued"
    assert job_after_sweep["worker_name"] is None
    assert job_after_sweep["heartbeat_at"] is None
    assert job_after_sweep["attempts"] == 1, (
        "the attempt cap's own memory survives a requeue"
    )

    # Still not failed -- the sweep never touches titles at all, only jobs.
    title_after_sweep = await raw_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title_id
    )
    assert title_after_sweep["status"] == "preparing"
    assert title_after_sweep["failure_reason"] is None

    # And the row really is back in flight, not merely requeued on paper:
    # a fresh claim picks it up as a second attempt.
    second_job = await _claim_one(raw_conn)
    assert second_job.job_id == job.job_id
    assert second_job.attempt == 2
