"""repack-layout: which stored files it rewrites, when it waits, and a real
rewrite end to end with storage and the catalog replaced by local stand-ins.
"""

import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.cli import repack

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)
EDITION = "11111111-2222-3333-4444-555555555555"


def _probe(types, chapters=0, attached=()):
    return {
        "streams": [
            {"index": i, "codec_type": t, "disposition": {"attached_pic": int(i in attached)}}
            for i, t in enumerate(types)
        ],
        "chapters": [{}] * chapters,
    }


def test_classify_tells_done_from_to_do_from_left_alone():
    assert repack.classify(_probe(["audio", "video"])) == repack.ALREADY
    assert repack.classify(_probe(["video", "audio", "data"], chapters=6)) == repack.REPACK
    assert repack.classify(_probe(["video", "audio"])) == repack.REPACK
    assert repack.classify(_probe(["audio", "video"], chapters=2)) == repack.REPACK
    assert "cover picture" in repack.classify(_probe(["video", "audio", "video"], attached=(2,)))
    assert "2 audio" in repack.classify(_probe(["video", "audio", "audio"]))


def test_edge_log_window_finds_the_start_and_the_last_fetch(tmp_path):
    log = tmp_path / "blob_access.log"
    log.write_text(
        f"2026-10-22T18:00:00+00:00 /blob/{EDITION}.mp4 - HIT 206 1 0\n"
        "2026-10-22T18:30:00+00:00 /blob/other.mp4 - HIT 206 1 0\n"
        f"2026-10-22T19:10:00+00:00 /blob/{EDITION}.mp4 bytes=0-1 MISS 206 1 0\n"
        f"2026-10-22T19:20:00+00:00 /blob/{EDITION}.mp4x - HIT 206 1 0\n"
    )
    start, last = repack.edge_log_window(log, f"/blob/{EDITION}.mp4")
    assert start == datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)
    assert last == datetime(2026, 10, 22, 19, 10, tzinfo=timezone.utc)
    assert repack.edge_log_window(log, "/blob/never.mp4")[1] is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, None),
        ({"log_start": NOW - timedelta(minutes=30)}, "too recent"),
        ({"last_fetch": NOW - timedelta(minutes=60)}, "watched at 19:00"),
        ({"last_fetch": NOW - timedelta(minutes=80)}, None),
        ({"playlist_building": True}, "playlist"),
        ({"free_bytes": 2_999}, "scratch"),
    ],
)
def test_deferral_reason(changes, expected):
    args = dict(now=NOW, log_start=NOW - timedelta(hours=3), last_fetch=None,
                playlist_building=False, free_bytes=3_000, size_bytes=1_000)
    reason = repack.deferral_reason(**{**args, **changes})
    if expected is None:
        assert reason is None
    else:
        assert expected in reason


# --- a real rewrite, storage and catalog replaced -----------------------

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


@pytest.fixture
def stand(tmp_path, monkeypatch, settings):
    """A video-first file with chapters as the stored object, an edge log
    that last saw it three hours ago, and a catalog where it is live.
    """
    chapters = tmp_path / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=A\n")
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    stored = bucket / f"{EDITION}.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=2:s=64x64:r=24",
         "-f", "lavfi", "-i", "sine=d=2", "-i", str(chapters), "-map", "0:v", "-map", "1:a",
         "-map_chapters", "2", "-c:v", "mpeg4", "-c:a", "aac", "-movflags", "+faststart",
         str(stored)],
        check=True,
    )
    log = tmp_path / "blob_access.log"
    three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    log.write_text(f"{three_hours_ago} /blob/{EDITION}.mp4 - HIT 206 1 0\n")
    work = tmp_path / "work"
    work.mkdir()

    state = {"live": True, "size": None, "deleted": [], "uploads": 0}

    class Client:
        def generate_presigned_url(self, _op, Params, ExpiresIn):
            return str(bucket / Params["Key"])

    async def edition_is_live(_conn, _id):
        return state["live"]

    async def playlist_build_in_progress(_conn):
        return False

    async def set_edition_size(_conn, _id, size):
        state["size"] = size

    def put_object_from_file(path, key):
        state["uploads"] += 1
        shutil.copyfile(path, bucket / key)
        if state.get("reap_during_upload"):
            state["live"] = None

    monkeypatch.setattr(repack, "get_s3_client", lambda: Client())
    monkeypatch.setattr(repack, "download_object_to_file",
                        lambda key, dest: shutil.copyfile(bucket / key, dest))
    monkeypatch.setattr(repack, "head_object",
                        lambda key: {"ContentLength": (bucket / key).stat().st_size})
    monkeypatch.setattr(repack, "put_object_from_file", put_object_from_file)
    monkeypatch.setattr(repack, "delete_object", lambda key: state["deleted"].append(key))
    monkeypatch.setattr(repack.queries, "edition_is_live", edition_is_live)
    monkeypatch.setattr(repack.queries, "playlist_build_in_progress", playlist_build_in_progress)
    monkeypatch.setattr(repack.queries, "set_edition_size", set_edition_size)
    row = {"edition_id": EDITION, "object_key": f"{EDITION}.mp4", "edition_slug": None,
           "title_name": "Test", "season_number": 1, "episode_number": 1}
    return row, work, log, stored, state


@needs_ffmpeg
async def test_a_stored_file_is_rewritten_in_place_and_then_left_alone(stand):
    row, work, log, stored, state = stand

    outcome = await repack._repack_one(None, row, work, log)

    assert outcome == (repack.REPACKED, "")
    assert repack.classify(repack.probe_layout(str(stored))) == repack.ALREADY
    assert state["size"] == stored.stat().st_size
    assert list(work.iterdir()) == []
    assert await repack._repack_one(None, row, work, log) == (repack.ALREADY, "")


@needs_ffmpeg
async def test_a_file_watched_within_the_hour_is_deferred_untouched(stand):
    row, work, log, stored, state = stand
    before = stored.read_bytes()
    log.write_text(log.read_text() + f"{datetime.now(timezone.utc).isoformat()} "
                   f"/blob/{EDITION}.mp4 bytes=0-1 HIT 206 1 0\n")

    outcome, reason = await repack._repack_one(None, row, work, log)

    assert outcome == repack.DEFERRED and "watched at" in reason
    assert stored.read_bytes() == before and state["uploads"] == 0


@needs_ffmpeg
async def test_an_edition_reaped_during_the_upload_takes_its_new_object_with_it(stand):
    row, work, log, stored, state = stand
    state["reap_during_upload"] = True

    outcome, _ = await repack._repack_one(None, row, work, log)

    assert outcome == repack.SKIPPED
    assert state["deleted"] == [f"{EDITION}.mp4"]


# --- the catalog queries, against a real database ------------------------


@pytest.mark.integration
async def test_the_catalog_offers_live_editions_newest_first_and_tracks_their_fate(db_conn):
    import uuid

    from orchestrator.db import queries

    older, newer = uuid.uuid4(), uuid.uuid4()
    older_title = await queries.create_title_with_edition(
        db_conn, "Older", "older", older, f"{older}.mp4"
    )
    await queries.create_title_with_edition(db_conn, "Newer", "newer", newer, f"{newer}.mp4")
    await db_conn.execute(
        "UPDATE editions SET created_at = now() - interval '1 day' WHERE id = $1", older
    )

    rows = [r for r in await queries.editions_to_repack(db_conn)
            if r["edition_id"] in (newer, older)]
    assert [r["edition_id"] for r in rows] == [newer, older]
    assert rows[0]["title_name"] == "Newer" and rows[0]["episode_number"] == 1
    assert await queries.edition_is_live(db_conn, older) is True
    assert await queries.playlist_build_in_progress(db_conn) is False

    await queries.set_edition_size(db_conn, older, 1234)
    assert await db_conn.fetchval("SELECT size_bytes FROM editions WHERE id = $1", older) == 1234

    await queries.mark_for_delete(db_conn, older_title, 3600)
    assert older not in [r["edition_id"] for r in await queries.editions_to_repack(db_conn)]
    assert await queries.edition_is_live(db_conn, older) is False
    assert await queries.edition_is_live(db_conn, uuid.uuid4()) is None
