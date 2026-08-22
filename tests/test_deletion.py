"""Integration tests for the CLI `remove` command and the reaper
(orchestrator/reaper.py). Storage is mocked -- no real S3 delete.
"""
import uuid

import pytest

from orchestrator.cli import __main__ as cli
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator import reaper

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cli_env(migrated_db_url, monkeypatch):
    monkeypatch.setenv("MYTUBE_DATABASE_URL", migrated_db_url)
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
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


async def _create_title(conn, name: str, slug: str) -> tuple[uuid.UUID, str]:
    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    title_id = await queries.create_title_with_edition(conn, name, slug, edition_id, object_key)
    return title_id, object_key


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
    assert "не найден" in err


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
