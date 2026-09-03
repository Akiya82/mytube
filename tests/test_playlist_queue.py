"""The playlist build worker's failure and cleanup branches.

No database, no bucket, no ffmpeg: the pool is a stub whose `.acquire()`
hands out an object nothing looks at, the three catalog calls the worker
makes are replaced at the module boundary, and every storage and
subprocess call is a fake that records what it was asked to do. What is
under test is the worker's own decisions -- what it refuses to start,
what it deletes when a step fails, and what it declines to publish.

These are the branches that only run when something has already gone
wrong, which is exactly when nobody is watching. The happy path is
covered live by `tools/smoke_playlist.sh`, and the pure assembly layer
has its own module in `test_playlist_build.py`; what was missing was
everything in between.

**Nothing here asserts a value this file just stubbed.** A test that
makes `set_playlist_built` return False and then checks the worker saw
False proves only that Python assigns variables. Each test asserts what
the worker *did* with the answer: which call it made next, with which
arguments, and which call it did not make at all.
"""
import asyncio
import contextlib
import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from mytube_media import compat, remux
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media import add as add_module
from orchestrator.media import playlist_queue as worker
from orchestrator.media.playlist_queue import (
    REASON_KEY_ASSEMBLY,
    REASON_KEY_CHECKS,
    REASON_KEY_EMPTY,
    REASON_KEY_STORAGE_FULL,
    PlaylistBuildJob,
    PlaylistBuildQueue,
    process_playlist_build,
)

from conftest import ADMIN_FQDN

MEGABYTE = 1024 * 1024

_DiskUsage = namedtuple("_DiskUsage", "total used free")


class _AcquireCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


class _PoolLike:
    """The `.acquire()` shape the worker asks an `asyncpg.Pool` for, and
    nothing else -- every query it would run is stubbed above the
    connection.
    """

    def acquire(self):
        return _AcquireCtx()


def _member(position, *, name=None, key=None, size=MEGABYTE, dropped=False, **over):
    """One row as `queries.playlist_build_inputs` returns it: the stored
    object, its size, the title's name and the seven recorded stream
    parameters the joinability check reads.
    """
    row = {
        "position": position,
        "edition_id": f"edition-{position}",
        "object_key": key if key is not None else f"member-{position}.mp4",
        "size_bytes": size,
        "duration_seconds": 8.0,
        "title_name": name or f"Clip {position}",
        "fps": 24.0,
        "video_profile": "High",
        "video_level": 41,
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "audio_channel_layout": "mono",
        "dropped": dropped,
    }
    row.update(over)
    return row


def _readable_probe(duration=8.0):
    return {"streams": [{"codec_type": "video"}], "format": {"duration": str(duration)}}


NO_VIDEO_STREAM = {"streams": [{"codec_type": "audio"}], "format": {"duration": "8.0"}}
NO_USABLE_DURATION = {"streams": [{"codec_type": "video"}], "format": {}}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """The worker's whole environment, recorded.

    `state` is what a test arranges before the build runs; `calls` is what
    the build did. `state["members"]` may be a list or a callable taking
    the number of reads already made, so a test can change the membership
    between two builds the way pressing save twice does.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()

    state = {
        "work_dir": work_dir,
        "members": [_member(1), _member(2)],
        "free_bytes": 1 << 40,
        "swapped": True,
        # Probe results by call order; anything not named here reads back
        # as a decodable eight-second clip.
        "probes": {},
        "ffmpeg_error": None,
        "record_failure_error": None,
    }
    calls = {
        "events": [],
        "inputs_read": [],
        "downloaded": [],
        "ffmpeg": [],
        "probed": [],
        "uploaded": [],
        "aborted": [],
        "deleted": [],
        "built": [],
        "failed": [],
    }

    async def playlist_build_inputs(conn, playlist_id):
        read_index = len(calls["inputs_read"])
        calls["inputs_read"].append(playlist_id)
        calls["events"].append(("read", playlist_id))
        members = state["members"]
        return list(members(read_index) if callable(members) else members)

    async def set_playlist_built(conn, playlist_id, mode, artifacts):
        calls["built"].append((playlist_id, mode, [dict(a) for a in artifacts]))
        calls["events"].append(("built", playlist_id))
        return state["swapped"]

    async def set_playlist_build_failed(conn, playlist_id, reason_key, reason_params):
        calls["failed"].append((playlist_id, reason_key, reason_params))
        calls["events"].append(("failed", playlist_id))
        if state["record_failure_error"] is not None:
            raise state["record_failure_error"]

    for name, fake in (
        ("playlist_build_inputs", playlist_build_inputs),
        ("set_playlist_built", set_playlist_built),
        ("set_playlist_build_failed", set_playlist_build_failed),
    ):
        monkeypatch.setattr(queries, name, fake, raising=False)

    def fake_disk_usage(path):
        return _DiskUsage(total=1 << 40, used=0, free=state["free_bytes"])

    monkeypatch.setattr(shutil, "disk_usage", fake_disk_usage)

    def fake_download(key, destination):
        calls["downloaded"].append((key, destination))
        Path(destination).write_bytes(b"stub member bytes")

    def fake_run_ffmpeg(argv, *, timeout):
        calls["ffmpeg"].append((list(argv), timeout))
        if state["ffmpeg_error"] is not None:
            raise state["ffmpeg_error"]
        Path(argv[-1]).write_bytes(b"stub segment bytes")

    def fake_probe(path):
        index = len(calls["probed"])
        calls["probed"].append(str(path))
        override = state["probes"].get(index)
        return _readable_probe() if override is None else override

    def fake_put(path, key, content_type):
        calls["uploaded"].append((path, key, content_type))

    def fake_abort(key):
        calls["aborted"].append(key)

    def fake_delete(key):
        calls["deleted"].append(key)

    monkeypatch.setattr(worker, "download_object_to_file", fake_download)
    monkeypatch.setattr(worker, "put_object_from_file", fake_put)
    monkeypatch.setattr(worker, "abort_dangling_multipart", fake_abort)
    monkeypatch.setattr(worker, "delete_object", fake_delete)
    monkeypatch.setattr(remux, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(compat, "probe", fake_probe)

    yield state, calls
    get_settings.cache_clear()


def _uploaded_keys(calls):
    return [key for _path, key, _content_type in calls["uploaded"]]


def _reasons(calls):
    return [reason for _id, reason, _params in calls["failed"]]


async def _run(playlist_id="playlist-a"):
    await process_playlist_build(_PoolLike(), PlaylistBuildJob(playlist_id=playlist_id))


# --- a playlist deleted while its build was running -------------------


async def test_a_playlist_deleted_mid_build_leaves_none_of_its_objects_behind(build):
    """The swap is refused because the row is gone, so no artifact row
    will ever name the segments this build uploaded and no reclamation
    sweep can find them by one. The worker is the only thing that still
    knows their keys; returning without deleting them leaves them in the
    bucket forever.
    """
    state, calls = build
    state["swapped"] = False

    await _run()

    keys = _uploaded_keys(calls)
    assert len(keys) == 2, "both members reached the bucket before the swap was refused"
    assert calls["aborted"] == keys
    assert calls["deleted"] == keys


async def test_a_deleted_playlist_is_not_marked_failed_and_the_swap_is_not_retried(build):
    """Nothing went wrong -- the operator deleted the playlist. Writing a
    refusal onto a row the reaper has already finished would resurrect a
    deleted playlist as a failed one.
    """
    state, calls = build
    state["swapped"] = False

    await _run()

    assert calls["failed"] == []
    assert len(calls["built"]) == 1


# --- not enough room to build ----------------------------------------


async def test_a_build_scratch_cannot_hold_is_refused_before_a_byte_moves(build):
    """A refusal after the download is worth nothing: the disk is full
    either way by then, and this stack has run out of space twice.
    """
    state, calls = build
    state["members"] = [_member(1, size=100 * MEGABYTE)]
    state["free_bytes"] = 1

    await _run()

    assert calls["downloaded"] == []
    assert calls["ffmpeg"] == []
    assert calls["uploaded"] == []
    assert calls["built"] == []
    assert _reasons(calls) == [REASON_KEY_STORAGE_FULL]


async def test_the_room_a_build_demands_is_its_own_figure_not_the_add_paths(build):
    """Every member is pulled into scratch and the output is written
    beside them, so the add path's arithmetic -- calibrated for a source
    that stays on the read-only share and never enters scratch -- passes a
    disk that cannot hold this build. ffmpeg then dies part way through a
    multi-gigabyte write, which is what the precheck exists to prevent.

    Pinned from both sides: the add path's figure must not be enough, and
    this build's own figure must be.
    """
    state, calls = build
    total = 100 * MEGABYTE
    state["members"] = [_member(1, size=total)]

    state["free_bytes"] = add_module.required_free_bytes(total, already_correct=False)
    await _run()
    assert calls["downloaded"] == [], (
        "the add path's figure fits on a disk this build would overrun"
    )
    assert _reasons(calls) == [REASON_KEY_STORAGE_FULL]

    calls["failed"].clear()
    state["free_bytes"] = 3 * total
    await _run()
    assert len(calls["uploaded"]) == 1, "a disk that does fit the build is not refused"
    assert calls["failed"] == []


# --- the produced file failing its own check --------------------------


@pytest.mark.parametrize(
    "broken_probe",
    [NO_VIDEO_STREAM, NO_USABLE_DURATION],
    ids=["no-video", "no-duration"],
)
async def test_a_segment_failing_its_check_publishes_nothing(build, broken_probe):
    """ffmpeg exits zero over a result that will not play, which is the
    whole reason a produced file is checked rather than trusted. Nothing
    may be pointed at it, so the previous generation keeps playing.
    """
    state, calls = build
    state["probes"] = {1: broken_probe}

    await _run()

    assert calls["built"] == [], "no swap was recorded, so nothing points at this build"
    assert _reasons(calls) == [REASON_KEY_CHECKS]


async def test_a_segment_failing_its_check_takes_the_earlier_uploads_with_it(build):
    """The first member was already in the bucket when the second one
    failed its check. Nothing will ever name it.
    """
    state, calls = build
    state["probes"] = {1: NO_VIDEO_STREAM}

    await _run()

    keys = _uploaded_keys(calls)
    assert len(keys) == 1
    assert calls["aborted"] == keys
    assert calls["deleted"] == keys


async def test_ffmpeg_failing_is_reported_as_an_assembly_failure_not_a_check_one(build):
    """The two send the operator to different places: one means the clip
    that came out is wrong, the other means it never came out.
    """
    state, calls = build
    state["ffmpeg_error"] = remux.RemuxError("ffmpeg exited 1")

    await _run()

    assert _reasons(calls) == [REASON_KEY_ASSEMBLY]
    assert calls["uploaded"] == []


async def test_a_failed_build_leaves_nothing_in_scratch(build):
    """A build that failed half way still pulled members onto local disk.
    Leaving them there is how the next build finds no room.
    """
    state, calls = build
    state["ffmpeg_error"] = remux.RemuxError("ffmpeg exited 1")

    await _run()

    assert list(state["work_dir"].iterdir()) == []


# --- what a build refuses to start on ---------------------------------


async def test_a_playlist_whose_clips_were_all_deleted_downloads_nothing(build):
    state, calls = build
    state["members"] = [_member(1, dropped=True), _member(2, dropped=True)]

    await _run()

    assert calls["downloaded"] == []
    assert _reasons(calls) == [REASON_KEY_EMPTY]


async def test_a_deleted_clip_drops_out_and_the_rest_are_still_assembled(build):
    """The promise of a link that keeps working: a member's title being
    deleted removes that member, not the playlist.
    """
    state, calls = build
    state["members"] = [_member(1), _member(2, dropped=True), _member(3)]

    await _run()

    assert [key for key, _dest in calls["downloaded"]] == [
        "member-1.mp4",
        "member-3.mp4",
    ]
    _id, mode, artifacts = calls["built"][0]
    assert mode == "hls"
    assert [a["position"] for a in artifacts] == [1, 3]


# --- rebuilds, and the generation a rebuild replaces ------------------


async def test_a_rebuild_writes_fresh_keys_and_deletes_nothing(build):
    """A viewer holding an address signed before the rebuild must still
    get bytes. Overwriting rewrites what someone is part way through
    reading, and deleting at swap time hands them the stub clip in the
    middle of the evening this product exists for -- the superseded
    generation is left for the reclamation sweep to take once no signature
    can still reach it.
    """
    state, calls = build

    await _run()
    first = set(_uploaded_keys(calls))
    await _run()
    second = set(_uploaded_keys(calls)) - first

    assert len(first) == 2 and len(second) == 2
    assert first.isdisjoint(second), "a rebuild never reuses a live object's key"
    assert not any("playlist-a" in key for key in first | second), (
        "the key is fresh per build, never the playlist's own stable identifier"
    )
    assert calls["deleted"] == [], "the superseded generation stays in the bucket"
    assert calls["aborted"] == []


# --- the queue around it ----------------------------------------------


@contextlib.asynccontextmanager
async def _running(queue, pool):
    task = asyncio.create_task(queue.run(pool))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def test_two_saves_in_a_row_build_one_after_the_other_from_a_fresh_read(build):
    """A job carries a playlist identifier and nothing else, so what gets
    built is the membership in the catalog when the build starts, not the
    one that was on screen when save was pressed. Two saves in a row are
    ordinary; a job carrying a snapshot from the first would build the
    wrong list.
    """
    state, calls = build
    state["members"] = lambda read_index: (
        [_member(1)] if read_index == 0 else [_member(1), _member(2)]
    )

    queue = PlaylistBuildQueue()
    async with _running(queue, _PoolLike()):
        await queue.enqueue(PlaylistBuildJob(playlist_id="playlist-a"))
        await queue.enqueue(PlaylistBuildJob(playlist_id="playlist-a"))
        await _until(lambda: len(calls["built"]) == 2)

    assert [kind for kind, _id in calls["events"]] == [
        "read",
        "built",
        "read",
        "built",
    ], "the second build reads after the first one swapped, never alongside it"
    assert len(calls["built"][0][2]) == 1
    assert len(calls["built"][1][2]) == 2, "the second build saw the added member"


async def test_one_build_blowing_up_does_not_stop_the_next(build):
    """Per-job isolation: a failure the build's own handler cannot absorb
    -- here the catalog write that records the refusal -- must not take
    the worker down with it and strand every later save.
    """
    state, calls = build
    state["ffmpeg_error"] = remux.RemuxError("ffmpeg exited 1")
    state["record_failure_error"] = RuntimeError("the catalog write exploded")

    queue = PlaylistBuildQueue()
    async with _running(queue, _PoolLike()):
        await queue.enqueue(PlaylistBuildJob(playlist_id="doomed"))
        await _until(lambda: len(calls["failed"]) == 1)

        state["ffmpeg_error"] = None
        state["record_failure_error"] = None
        await queue.enqueue(PlaylistBuildJob(playlist_id="playlist-b"))
        await _until(lambda: len(calls["built"]) == 1)

    assert calls["built"][0][0] == "playlist-b"
