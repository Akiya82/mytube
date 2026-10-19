"""The reaper's playlist sweep: reclaiming what a deleted playlist made,
and what a rebuild left behind.

No database and no object store -- the queries and `delete_object` are
stubbed at the module boundary, so what is under test is the sweep's own
decisions: how many objects it removes, when it is allowed to record the
work as done, and whether a failure in one of the three sweeps stops the
other two.
"""

import asyncio

import pytest

from orchestrator import reaper
from orchestrator.config import get_settings
from orchestrator.db import queries


@pytest.fixture(autouse=True)
def _env(settings):
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


class _AcquireCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


class _PoolLike:
    def acquire(self):
        return _AcquireCtx()


@pytest.fixture
def sweep(monkeypatch):
    """The sweep's whole environment, recorded. `state` is what the test
    arranges; `calls` is what the sweep did.
    """
    state = {"due": [], "keys": {}, "reclaimable": [], "delete_fails": set()}
    calls = {
        "deleted_objects": [],
        "finished": [],
        "dropped_rows": [],
        "reclaim_age": [],
    }

    async def due_for_reaping_playlists(conn):
        return [{"playlist_id": pid} for pid in state["due"]]

    async def playlist_artifact_keys(conn, playlist_id):
        return list(state["keys"].get(playlist_id, []))

    async def finish_delete_playlist(conn, playlist_id):
        calls["finished"].append(playlist_id)

    async def due_for_reclaim_artifacts(conn, older_than_seconds):
        calls["reclaim_age"].append(older_than_seconds)
        return list(state["reclaimable"])

    async def delete_playlist_artifact(conn, artifact_id):
        calls["dropped_rows"].append(artifact_id)

    for name, fake in (
        ("due_for_reaping_playlists", due_for_reaping_playlists),
        ("playlist_artifact_keys", playlist_artifact_keys),
        ("finish_delete_playlist", finish_delete_playlist),
        ("due_for_reclaim_artifacts", due_for_reclaim_artifacts),
        ("delete_playlist_artifact", delete_playlist_artifact),
    ):
        monkeypatch.setattr(queries, name, fake, raising=False)

    def delete_object(key):
        if key in state["delete_fails"]:
            raise RuntimeError("storage is down")
        calls["deleted_objects"].append(key)

    monkeypatch.setattr(reaper, "delete_object", delete_object)
    return state, calls


async def test_every_artifact_goes_not_just_the_first(sweep):
    """A mechanism that produced fifty objects must not be scored on one:
    finishing the row after a single delete records the work as done with
    forty-nine objects still occupying the bucket.
    """
    state, calls = sweep
    keys = [f"segment-{n}.mp4" for n in range(50)]
    state["due"] = ["playlist-a"]
    state["keys"] = {"playlist-a": keys}

    await reaper.reap_playlists_once(_PoolLike())

    assert calls["deleted_objects"] == keys
    assert calls["finished"] == ["playlist-a"]


async def test_a_playlist_that_never_built_anything_is_finished_with_no_storage_call(
    sweep,
):
    state, calls = sweep
    state["due"] = ["never-built"]
    state["keys"] = {}

    await reaper.reap_playlists_once(_PoolLike())

    assert calls["deleted_objects"] == []
    assert calls["finished"] == ["never-built"]


async def test_a_storage_failure_leaves_the_row_unfinished_for_the_next_cycle(sweep):
    """Recording a deletion as done with bytes still in the bucket leaves
    an object nothing will ever point at or clean up again.
    """
    state, calls = sweep
    state["due"] = ["playlist-a"]
    state["keys"] = {"playlist-a": ["ok.mp4", "refused.mp4"]}
    state["delete_fails"] = {"refused.mp4"}

    await reaper.reap_playlists_once(_PoolLike())

    assert calls["finished"] == []
    assert "ok.mp4" in calls["deleted_objects"]


async def test_a_superseded_artifact_is_reclaimed_once_no_signature_can_still_reach_it(
    sweep,
):
    """The age is not arbitrary: an address handed out before the rebuild
    stays valid for exactly the signing lifetime.
    """
    state, calls = sweep
    state["reclaimable"] = [{"id": "row-1", "object_key": "old-generation.mp4"}]

    await reaper.reap_playlists_once(_PoolLike())

    assert calls["reclaim_age"] == [get_settings().sign_ttl_seconds]
    assert calls["deleted_objects"] == ["old-generation.mp4"]
    assert calls["dropped_rows"] == ["row-1"]


async def test_a_superseded_artifact_whose_object_refuses_to_go_keeps_its_row(sweep):
    """Dropping the row first would leave an object nothing can name --
    the row is the only record of the key.
    """
    state, calls = sweep
    state["reclaimable"] = [{"id": "row-1", "object_key": "stuck.mp4"}]
    state["delete_fails"] = {"stuck.mp4"}

    await reaper.reap_playlists_once(_PoolLike())

    assert calls["dropped_rows"] == []


async def test_one_sweep_failing_does_not_stop_the_other_two(monkeypatch):
    """Three independent boundaries in the loop body, not two: a storage
    outage taking out the title sweep must not also stop playlists being
    reclaimed or expired sessions being swept.
    """
    ran = []

    async def failing(pool):
        ran.append("titles")
        raise RuntimeError("storage is down")

    async def playlists(pool):
        ran.append("playlists")

    async def sessions(pool):
        ran.append("sessions")

    class _Stop(Exception):
        pass

    async def stop_after_one_cycle(seconds):
        raise _Stop

    monkeypatch.setattr(reaper, "reap_once", failing)
    monkeypatch.setattr(reaper, "reap_playlists_once", playlists)
    monkeypatch.setattr(reaper, "sweep_sessions_once", sessions)
    monkeypatch.setattr(asyncio, "sleep", stop_after_one_cycle)

    with pytest.raises(_Stop):
        await reaper.run_reaper(_PoolLike())

    assert ran == ["titles", "playlists", "sessions"]
