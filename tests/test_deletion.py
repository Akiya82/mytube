"""Integration tests for the CLI `remove` command and the reaper
(orchestrator/reaper.py), plus (plan 10) the six per-episode action routes
in orchestrator/web/routes_episodes.py, called directly against a real
connection through a minimal Request/pool stand-in rather than a full
authenticated HTTP client -- the same "exercise the real function against
a real Postgres, skip the transport" trade this file's own reaper tests
already make. Storage is mocked -- no real S3 delete.
"""
import uuid

import pytest

from orchestrator.cli import __main__ as cli
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator import reaper
from orchestrator.web import routes_episodes

from conftest import ADMIN_FQDN, WORKER_TOKEN

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cli_env(migrated_db_url, monkeypatch):
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
def deleted_objects(monkeypatch):
    deleted = []

    def fake_delete_object(key):
        deleted.append(key)

    monkeypatch.setattr(reaper, "delete_object", fake_delete_object)
    return deleted


@pytest.fixture
def episode_route_deleted_objects(monkeypatch):
    """The per-episode action routes import `delete_object` into
    `orchestrator.web.routes_episodes`'s own namespace (`from
    orchestrator.storage import delete_object`) rather than reaching it
    through `reaper` -- a `from x import y` binds its own reference, so
    patching `reaper.delete_object` above never touches this one.
    """
    deleted = []

    def fake_delete_object(key):
        deleted.append(key)

    monkeypatch.setattr(routes_episodes, "delete_object", fake_delete_object)
    return deleted


async def _create_title(conn, name: str, slug: str) -> tuple[uuid.UUID, str]:
    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    title_id = await queries.create_title_with_edition(conn, name, slug, edition_id, object_key)
    return title_id, object_key


async def _create_two_episode_series(
    conn, name: str, slug: str, *, second_status: str = "active"
) -> tuple:
    """A two-episode title: episode 1 with an active edition (created by
    `create_title_with_edition`'s own default, unchanged), episode 2 with
    an edition in `second_status` -- the shape every episode-scoped action
    test below acts on episode 2 while asserting episode 1 (the sibling)
    is untouched.
    """
    edition1 = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        conn, name, slug, edition1, f"{edition1}.mp4"
    )
    ep1 = (await queries.episode_by_number(conn, title_id, 1))["episode_id"]
    edition2 = uuid.uuid4()
    await queries.attach_episode_to_title(
        conn, title_id, 2, edition2, f"{edition2}.mp4", status=second_status
    )
    ep2 = (await queries.episode_by_number(conn, title_id, 2))["episode_id"]
    return title_id, ep1, ep2, edition2


class _FakeAppState:
    def __init__(self, pool):
        self.db_pool = pool


class _FakeApp:
    def __init__(self, pool):
        self.state = _FakeAppState(pool)


class _FakeRequest:
    """The one attribute every route in routes_episodes.py reads off its
    own `request` argument (`request.app.state.db_pool`) -- a real
    `fastapi.Request` carries far more than this, but nothing else these
    routes touch.
    """

    def __init__(self, conn):
        self.app = _FakeApp(_SinglePoolLike(conn))


async def test_remove_marks_pending_delete_and_slug_resolves_non_active_immediately(raw_conn):
    title_id, _ = await _create_title(raw_conn, "Deletion Wording", "deletion-wording-slug")

    await cli._remove("deletion-wording-slug")

    row = await queries.resolve_slug(raw_conn, "deletion-wording-slug")
    assert row["status"] == "pending_delete"


async def test_remove_unknown_slug_fails(capsys):
    import typer

    with pytest.raises(typer.Exit):
        await cli._remove("does-not-exist-anywhere")

    err = capsys.readouterr().err
    assert "not found" in err


async def test_reaper_deletes_object_and_finishes_delete_after_grace_expires(
    raw_conn, deleted_objects
):
    title_id, object_key = await _create_title(raw_conn, "Reaper Target", "reaper-target-slug")
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=0)

    row = await queries.resolve_slug(raw_conn, "reaper-target-slug")
    assert row["status"] == "pending_delete"

    await reaper.reap_once(_SinglePoolLike(raw_conn))

    assert object_key in deleted_objects

    row_after = await queries.resolve_slug(raw_conn, "reaper-target-slug")
    assert row_after["status"] == "deleted"

    # D-10: the slug_history row survives the reap -- tombstone, not a hole.
    history = await raw_conn.fetch(
        "SELECT title_id FROM slug_history WHERE slug = $1", "reaper-target-slug"
    )
    assert len(history) == 1
    assert history[0]["title_id"] == title_id


async def test_reaper_deletes_poster_key_alongside_video_key(raw_conn, deleted_objects):
    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    poster_key = f"{edition_id}.jpg"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Reaper Poster Target", "reaper-poster-target-slug", edition_id, object_key
    )
    await queries.set_edition_poster(raw_conn, edition_id, poster_key)
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=0)

    await reaper.reap_once(_SinglePoolLike(raw_conn))

    assert object_key in deleted_objects
    assert poster_key in deleted_objects

    row_after = await queries.resolve_slug(raw_conn, "reaper-poster-target-slug")
    assert row_after["status"] == "deleted"


async def test_reaper_does_not_touch_titles_before_grace_expires(raw_conn, deleted_objects):
    title_id, object_key = await _create_title(
        raw_conn, "Reaper Not Yet", "reaper-not-yet-slug"
    )
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=3600)

    await reaper.reap_once(_SinglePoolLike(raw_conn))

    assert object_key not in deleted_objects
    row = await queries.resolve_slug(raw_conn, "reaper-not-yet-slug")
    assert row["status"] == "pending_delete"


async def test_reaper_retries_next_cycle_when_an_object_delete_fails(raw_conn, monkeypatch):
    """A storage outage during a reap must not end with the title recorded
    as deleted and the bytes still in the bucket: nothing would ever retry,
    and the object becomes an orphan no audit can attribute.
    """
    title_id, object_key = await _create_title(
        raw_conn, "Reaper Outage", "reaper-outage-slug"
    )
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=0)

    def failing_delete(key):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(reaper, "delete_object", failing_delete)
    await reaper.reap_once(_SinglePoolLike(raw_conn))

    row = await queries.resolve_slug(raw_conn, "reaper-outage-slug")
    assert row["status"] == "pending_delete"

    deleted = []
    monkeypatch.setattr(reaper, "delete_object", deleted.append)
    await reaper.reap_once(_SinglePoolLike(raw_conn))

    assert object_key in deleted
    row_after = await queries.resolve_slug(raw_conn, "reaper-outage-slug")
    assert row_after["status"] == "deleted"


# --- Plan 10: the six per-episode action routes, each scoped to one
# episode, leaving its sibling untouched -------------------------------


async def test_episode_delete_starts_countdown_leaving_sibling_untouched(raw_conn):
    _title_id, ep1, ep2, _edition2 = await _create_two_episode_series(
        raw_conn, "Episode Delete", "episode-delete-slug"
    )

    resp = await routes_episodes.delete_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    target = await queries.episode_for_action(raw_conn, ep2)
    assert target["episode_status"] == "pending_delete"
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["episode_status"] == "active"


async def test_episode_undo_cancels_the_countdown_leaving_sibling_untouched(raw_conn):
    _title_id, ep1, ep2, _edition2 = await _create_two_episode_series(
        raw_conn, "Episode Undo", "episode-undo-slug"
    )
    await queries.mark_episode_for_delete(raw_conn, ep2, grace_seconds=3600)

    resp = await routes_episodes.undo_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    target = await queries.episode_for_action(raw_conn, ep2)
    assert target["episode_status"] == "active"
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["episode_status"] == "active"


async def test_episode_purge_deletes_immediately_leaving_sibling_untouched(
    raw_conn, deleted_objects
):
    _title_id, ep1, ep2, edition2 = await _create_two_episode_series(
        raw_conn, "Episode Purge", "episode-purge-slug"
    )
    await queries.mark_episode_for_delete(raw_conn, ep2, grace_seconds=3600)

    resp = await routes_episodes.purge_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    assert f"{edition2}.mp4" in deleted_objects
    target = await queries.episode_for_action(raw_conn, ep2)
    # ever active (attach_episode_to_title's own default status="active"),
    # so purge tombstones rather than drops -- see finish_delete_episode.
    assert target["episode_status"] == "deleted"
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["episode_status"] == "active"


async def test_episode_cancel_drops_a_never_active_episode_leaving_sibling_untouched(
    raw_conn, episode_route_deleted_objects
):
    title_id, ep1, ep2, edition2 = await _create_two_episode_series(
        raw_conn, "Episode Cancel", "episode-cancel-slug", second_status="preparing"
    )
    await queries.enqueue_job(
        raw_conn, kind="remux", title_id=title_id, edition_id=edition2, source_path="a.mkv"
    )

    resp = await routes_episodes.cancel_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    assert f"{edition2}.mp4" in episode_route_deleted_objects
    dropped = await raw_conn.fetchrow("SELECT id FROM episodes WHERE id = $1", ep2)
    assert dropped is None
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["episode_status"] == "active"


async def test_episode_cancel_reaches_only_that_episodes_own_job(
    raw_conn, episode_route_deleted_objects
):
    """A queued job for episode 1's own edition must never be touched by a
    cancel aimed at episode 2 (D-164) -- `job_for_edition` scopes strictly
    to the edition id it is given.
    """
    title_id, ep1, ep2, edition2 = await _create_two_episode_series(
        raw_conn, "Episode Cancel Scope", "episode-cancel-scope-slug", second_status="preparing"
    )
    ep1_row = await queries.episode_for_action(raw_conn, ep1)
    sibling_job_id = await queries.enqueue_job(
        raw_conn,
        kind="remux",
        title_id=title_id,
        edition_id=ep1_row["default_edition_id"],
        source_path="sibling.mkv",
    )
    await queries.enqueue_job(
        raw_conn, kind="remux", title_id=title_id, edition_id=edition2, source_path="a.mkv"
    )

    resp = await routes_episodes.cancel_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    sibling_job = await queries.job_by_id(raw_conn, sibling_job_id)
    assert sibling_job["status"] == "queued"


async def test_episode_retry_requeues_only_that_episodes_job_leaving_sibling_untouched(raw_conn):
    title_id, ep1, ep2, edition2 = await _create_two_episode_series(
        raw_conn, "Episode Retry", "episode-retry-slug", second_status="preparing"
    )
    await queries.mark_edition_failed(raw_conn, edition2, "failed.reason.upload")
    old_job_id = await queries.enqueue_job(
        raw_conn, kind="remux", title_id=title_id, edition_id=edition2, source_path="a.mkv"
    )
    await raw_conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = now() WHERE id = $1", old_job_id
    )

    resp = await routes_episodes.retry_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    target = await queries.episode_for_action(raw_conn, ep2)
    assert target["edition_status"] == "preparing"
    live_job = await queries.job_for_edition(raw_conn, edition2)
    assert live_job is not None and live_job["status"] == "queued"
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["edition_status"] == "active"


async def test_episode_dismiss_removes_only_that_failed_episode_leaving_sibling_untouched(
    raw_conn, episode_route_deleted_objects
):
    _title_id, ep1, ep2, edition2 = await _create_two_episode_series(
        raw_conn, "Episode Dismiss", "episode-dismiss-slug", second_status="preparing"
    )
    await queries.mark_edition_failed(raw_conn, edition2, "failed.reason.upload")

    resp = await routes_episodes.dismiss_episode_route(str(ep2), _FakeRequest(raw_conn))

    assert resp.status_code == 302
    assert f"{edition2}.mp4" in episode_route_deleted_objects
    row = await raw_conn.fetchrow("SELECT id FROM episodes WHERE id = $1", ep2)
    assert row is None
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["episode_status"] == "active"


async def test_episode_dismiss_deletes_objects_before_rows(raw_conn, monkeypatch):
    """T-02-08, one level down: a storage failure must leave the failed
    episode exactly as it was, never a row already gone with bytes an
    audit can no longer attribute to anything -- the same ordering
    routes_titles.dismiss_route already guarantees for a whole title.
    """
    _title_id, _ep1, ep2, edition2 = await _create_two_episode_series(
        raw_conn, "Episode Objects Before Rows", "episode-objects-before-rows-slug",
        second_status="preparing",
    )
    await queries.mark_edition_failed(raw_conn, edition2, "failed.reason.upload")

    def failing_delete(key):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(routes_episodes, "delete_object", failing_delete)

    with pytest.raises(RuntimeError):
        await routes_episodes.dismiss_episode_route(str(ep2), _FakeRequest(raw_conn))

    row = await queries.episode_for_action(raw_conn, ep2)
    assert row is not None
    assert row["edition_status"] == "failed"


# --- Plan 10: the reaper's own sweep, extended to episodes -----------------


async def test_episode_reaper_deletes_object_and_finishes_delete_after_grace_expires(
    raw_conn, deleted_objects
):
    edition_id = uuid.uuid4()
    slug = "episode-reaper-target-slug"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Episode Reaper Target", slug, edition_id, f"{edition_id}.mp4"
    )
    episode_id = (await queries.episode_by_number(raw_conn, title_id, 1))["episode_id"]
    await queries.mark_episode_for_delete(raw_conn, episode_id, grace_seconds=0)

    await reaper.reap_once(_SinglePoolLike(raw_conn))

    assert f"{edition_id}.mp4" in deleted_objects
    row = await queries.episode_for_action(raw_conn, episode_id)
    assert row["episode_status"] == "deleted"


async def test_episode_reaper_drops_a_never_active_episode_instead_of_tombstoning_it(
    raw_conn, deleted_objects
):
    _title_id, ep1, ep2, _edition2 = await _create_two_episode_series(
        raw_conn,
        "Episode Reaper Never Active",
        "episode-reaper-never-active-slug",
        second_status="preparing",
    )
    await queries.mark_episode_for_delete(raw_conn, ep2, grace_seconds=0)

    await reaper.reap_once(_SinglePoolLike(raw_conn))

    row = await raw_conn.fetchrow("SELECT id FROM episodes WHERE id = $1", ep2)
    assert row is None
    sibling = await queries.episode_for_action(raw_conn, ep1)
    assert sibling["episode_status"] == "active"


async def test_episode_reaper_retries_next_cycle_when_an_object_delete_fails(
    raw_conn, monkeypatch
):
    edition_id = uuid.uuid4()
    slug = "episode-reaper-outage-slug"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Episode Reaper Outage", slug, edition_id, f"{edition_id}.mp4"
    )
    episode_id = (await queries.episode_by_number(raw_conn, title_id, 1))["episode_id"]
    await queries.mark_episode_for_delete(raw_conn, episode_id, grace_seconds=0)

    def failing_delete(key):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(reaper, "delete_object", failing_delete)
    await reaper.reap_once(_SinglePoolLike(raw_conn))

    row = await queries.episode_for_action(raw_conn, episode_id)
    assert row["episode_status"] == "pending_delete"

    deleted = []
    monkeypatch.setattr(reaper, "delete_object", deleted.append)
    await reaper.reap_once(_SinglePoolLike(raw_conn))

    assert f"{edition_id}.mp4" in deleted
    row_after = await queries.episode_for_action(raw_conn, episode_id)
    assert row_after["episode_status"] == "deleted"


class _SingleAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class _SinglePoolLike:
    """Wraps a single already-open connection behind the `.acquire()`
    interface reaper.reap_once expects from an asyncpg.Pool -- avoids
    spinning up a second real pool just for this test.
    """

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _SingleAcquireCtx(self._conn)
