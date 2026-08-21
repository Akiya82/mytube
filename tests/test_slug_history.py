"""Integration tests against a real, freshly-migrated Postgres (testcontainers,
via the `db_conn` fixture in conftest.py). Marked `integration` -- these need
Docker.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from orchestrator.db import queries

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.integration


async def _create_title(conn, name: str, slug: str) -> uuid.UUID:
    edition_id = uuid.uuid4()
    return await queries.create_title_with_edition(
        conn, name, slug, edition_id, f"{edition_id}.mp4"
    )


async def test_migration_applies_and_creates_expected_tables(db_conn):
    tables = await db_conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    names = {row["tablename"] for row in tables}
    assert {"titles", "slug_history", "seasons", "episodes", "editions"} <= names


async def test_resolve_slug_returns_title_and_default_edition(db_conn):
    title_id = await _create_title(db_conn, "Атака Титанов", "ataka-titanov")

    row = await queries.resolve_slug(db_conn, "ataka-titanov")

    assert row is not None
    assert row["title_id"] == title_id
    assert row["status"] == "active"
    assert row["default_edition_id"] is not None


async def test_resolve_slug_never_existed_returns_none(db_conn):
    row = await queries.resolve_slug(db_conn, "does-not-exist")
    assert row is None


async def test_rename_pitfall_1_old_address_keeps_resolving(db_conn):
    """Pitfall 1, verbatim: create a title, rename it, hit the OLD address.
    If it doesn't resolve, the rake is in the schema, not the UI.
    """
    title_id = await _create_title(db_conn, "Original Name", "slug-a")

    await queries.rename_slug(db_conn, "slug-a", "slug-b")

    old = await queries.resolve_slug(db_conn, "slug-a")
    new = await queries.resolve_slug(db_conn, "slug-b")

    assert old is not None
    assert new is not None
    assert old["title_id"] == title_id
    assert new["title_id"] == title_id
    assert old["title_id"] == new["title_id"]


async def test_resolve_deleted_title_returns_non_active_status(db_conn):
    title_id = await _create_title(db_conn, "To Be Deleted", "slug-doomed")
    await queries.mark_for_delete(db_conn, title_id, grace_seconds=0)
    await queries.finish_delete(db_conn, title_id)

    row = await queries.resolve_slug(db_conn, "slug-doomed")

    assert row is not None
    assert row["status"] == "deleted"


async def test_reuse_slug_after_tombstone_new_title_wins_old_row_survives(db_conn):
    """D-10 / verdict B3: slug X belonged to deleted title A. A new title B
    is created *with* slug X (the CLI's `add --reuse-slug` path: title
    creation and slug assignment happen together, via
    create_title_with_edition -> reuse_slug) -> resolving X gives B, and
    A's history row for X is still there (a real second row, not an
    overwrite).
    """
    old_title_id = await _create_title(db_conn, "Old Title", "shared-slug")
    await queries.mark_for_delete(db_conn, old_title_id, grace_seconds=0)
    await queries.finish_delete(db_conn, old_title_id)

    new_title_id = await _create_title(db_conn, "New Title", "shared-slug")

    resolved = await queries.resolve_slug(db_conn, "shared-slug")
    assert resolved is not None
    assert resolved["title_id"] == new_title_id

    history_rows = await db_conn.fetch(
        "SELECT title_id FROM slug_history WHERE slug = $1", "shared-slug"
    )
    title_ids = {row["title_id"] for row in history_rows}
    assert title_ids == {old_title_id, new_title_id}


async def test_reuse_slug_direct_call_is_a_pure_insert_that_does_not_touch_other_rows(db_conn):
    """reuse_slug in isolation, on a title with no slug_history row yet at
    all (bypassing create_title_with_edition) -- proves it really is just
    "insert a current row for this title_id", nothing more.
    """
    old_title_id = await _create_title(db_conn, "Old Title", "direct-reuse-slug")
    await queries.mark_for_delete(db_conn, old_title_id, grace_seconds=0)
    await queries.finish_delete(db_conn, old_title_id)

    bare_title_id = await db_conn.fetchval(
        "INSERT INTO titles (name) VALUES ($1) RETURNING id", "Bare Title"
    )

    await queries.reuse_slug(db_conn, "direct-reuse-slug", bare_title_id)

    resolved = await queries.resolve_slug(db_conn, "direct-reuse-slug")
    assert resolved["title_id"] == bare_title_id

    rows = await db_conn.fetch(
        "SELECT title_id FROM slug_history WHERE slug = $1", "direct-reuse-slug"
    )
    assert {row["title_id"] for row in rows} == {old_title_id, bare_title_id}


async def test_rename_back_and_forth_keeps_two_rows_and_both_resolve(db_conn):
    title_id = await _create_title(db_conn, "Round Trip", "slug-x")

    await queries.rename_slug(db_conn, "slug-x", "slug-y")
    await queries.rename_slug(db_conn, "slug-y", "slug-x")

    rows = await db_conn.fetch(
        "SELECT slug, is_current FROM slug_history WHERE title_id = $1", title_id
    )
    assert len(rows) == 2

    resolved_x = await queries.resolve_slug(db_conn, "slug-x")
    resolved_y = await queries.resolve_slug(db_conn, "slug-y")
    assert resolved_x["title_id"] == title_id
    assert resolved_y["title_id"] == title_id

    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1
    assert current[0]["slug"] == "slug-x"


async def test_taken_slugs_includes_tombstones(db_conn):
    title_id = await _create_title(db_conn, "Tombstone Source", "tombstone-slug")
    await queries.mark_for_delete(db_conn, title_id, grace_seconds=0)
    await queries.finish_delete(db_conn, title_id)

    taken = await queries.taken_slugs(db_conn)
    assert "tombstone-slug" in taken


async def test_migration_downgrade_and_upgrade_round_trip(migrated_db_url):
    """`alembic upgrade head` already ran (session-scoped fixture) --
    `downgrade base` then `upgrade head` again proves both directions work
    on a real database, not just that upgrade doesn't error.
    """
    env = {
        **os.environ,
        "MYTUBE_DATABASE_URL": migrated_db_url,
        "MYTUBE_SECURE_LINK_SECRET": "test-secret-do-not-use-in-prod",
        "MYTUBE_S3_ENDPOINT": "http://storage:8333",
        "MYTUBE_S3_KEY": "test-key",
        "MYTUBE_S3_SECRET": "test-secret",
    }

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPO_ROOT, env=env, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env=env, check=True, capture_output=True, text=True,
    )
