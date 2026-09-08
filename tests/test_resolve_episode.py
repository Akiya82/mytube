"""Integration tests for the sixth migration and the address forms it turns
on -- schema, queries and the public routes, all against a real migrated
Postgres (testcontainers, via conftest.py's fixtures). Marked `integration`
for the whole module, same precedent as test_slug_history.py and
test_queries.py: every test here either runs a real alembic migration or
needs a real connection to observe cross-row behaviour a connection double
cannot exhibit.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from orchestrator.db import queries

from conftest import ADMIN_FQDN, WORKER_TOKEN

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.integration


def _alembic_env(migrated_db_url: str) -> dict:
    return {
        **os.environ,
        "MYTUBE_DATABASE_URL": migrated_db_url,
        "MYTUBE_SECURE_LINK_SECRET": "test-secret-do-not-use-in-prod",
        "MYTUBE_S3_ENDPOINT": "http://storage:8333",
        "MYTUBE_S3_KEY": "test-key",
        "MYTUBE_S3_SECRET": "test-secret",
        "MYTUBE_PUBLIC_BASE_URL": "https://mytube.example",
        "MYTUBE_ADMIN_FQDN": ADMIN_FQDN,
        "MYTUBE_WORKER_TOKEN": WORKER_TOKEN,
    }


def _run_alembic(migrated_db_url: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=_alembic_env(migrated_db_url),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


async def _episode_id_for_title(conn, title_id) -> uuid.UUID:
    return await conn.fetchval(
        """
        SELECT ep.id FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        WHERE s.title_id = $1
        """,
        title_id,
    )


def _slug(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


# --- Task 1: the sixth migration, with its backfill ------------------------


async def test_migration_upgrade_succeeds_and_reports_the_new_revision(migrated_db_url):
    _run_alembic(migrated_db_url, "downgrade", "0005")
    _run_alembic(migrated_db_url, "upgrade", "head")
    current = _run_alembic(migrated_db_url, "current")
    assert "0006" in current


async def test_migration_backfill_writes_one_current_history_row_for_a_pre_existing_episode(
    migrated_db_url,
):
    """The seeding has to happen before the upgrade -- a title created
    afterwards claims its own row (a later task's own change to
    create_title_with_edition) and would prove nothing about the backfill
    this migration itself performs.
    """
    _run_alembic(migrated_db_url, "downgrade", "0005")

    conn = await asyncpg.connect(migrated_db_url)
    try:
        title_id = await queries.create_title_with_edition(
            conn,
            "Pre-Migration Title",
            _slug("premigration"),
            uuid.uuid4(),
            "premigration.mp4",
        )
        episode_id = await _episode_id_for_title(conn, title_id)
        title_created_at = await conn.fetchval(
            "SELECT created_at FROM titles WHERE id = $1", title_id
        )
    finally:
        await conn.close()

    _run_alembic(migrated_db_url, "upgrade", "head")

    conn = await asyncpg.connect(migrated_db_url)
    try:
        rows = await conn.fetch(
            "SELECT number, episode_id, is_current FROM episode_number_history "
            "WHERE title_id = $1",
            title_id,
        )
        assert len(rows) == 1
        assert rows[0]["episode_id"] == episode_id
        assert rows[0]["number"] == 1
        assert rows[0]["is_current"] is True

        changed_at = await conn.fetchval(
            "SELECT changed_at FROM titles WHERE id = $1", title_id
        )
        assert changed_at == title_created_at
    finally:
        await conn.close()


async def test_migration_downgrade_runs_cleanly_back_to_the_previous_revision(
    migrated_db_url,
):
    _run_alembic(migrated_db_url, "downgrade", "0005")
    current = _run_alembic(migrated_db_url, "current")
    assert "0005" in current
    # Restore head -- every test module sharing this session-scoped
    # Postgres after this one expects the full schema to be in place.
    _run_alembic(migrated_db_url, "upgrade", "head")


async def test_migration_dismiss_a_failed_title_completes_after_the_upgrade(raw_conn):
    """delete_failed_title runs DELETE FROM episodes before DELETE FROM
    slug_history -- without slug_history.episode_id's ON DELETE SET NULL
    and episode_number_history.episode_id's ON DELETE CASCADE, this raises
    a foreign-key violation the moment this migration lands.

    The moved-standalone-address shape this schema exists for: a slug
    belonging to an unrelated, unaffected title but pointing at the doomed
    title's own episode. Its own row is untouched by
    `DELETE FROM slug_history WHERE title_id = $1` (a different title_id),
    so it must survive the dismiss outright -- with its pointer nulled by
    the cascade rather than left dangling at a row that no longer exists.
    """
    other_edition_id = uuid.uuid4()
    other_title_id = await queries.create_title_with_edition(
        raw_conn,
        "Unrelated Active Title",
        _slug("unrelated-active"),
        other_edition_id,
        f"{other_edition_id}.mp4",
    )

    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn,
        "Doomed Failed With History",
        _slug("dismiss-failed"),
        edition_id,
        f"{edition_id}.mp4",
        status="failed",
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)

    moved_slug = _slug("moved-address")
    await raw_conn.execute(
        "INSERT INTO slug_history (slug, title_id, episode_id, is_current) VALUES ($1, $2, $3, false)",
        moved_slug,
        other_title_id,
        episode_id,
    )
    await raw_conn.execute(
        "INSERT INTO episode_number_history (title_id, number, episode_id) "
        "VALUES ($1, 1, $2)",
        title_id,
        episode_id,
    )

    await queries.delete_failed_title(raw_conn, title_id)

    remaining_history = await raw_conn.fetch(
        "SELECT * FROM episode_number_history WHERE episode_id = $1", episode_id
    )
    assert remaining_history == []

    surviving_slug = await raw_conn.fetchrow(
        "SELECT episode_id FROM slug_history WHERE slug = $1", moved_slug
    )
    assert surviving_slug is not None
    assert surviving_slug["episode_id"] is None


async def test_migration_cancel_a_never_active_title_completes_after_the_upgrade(raw_conn):
    other_edition_id = uuid.uuid4()
    other_title_id = await queries.create_title_with_edition(
        raw_conn,
        "Unrelated Active Title Two",
        _slug("unrelated-active-two"),
        other_edition_id,
        f"{other_edition_id}.mp4",
    )

    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn,
        "Doomed Preparing With History",
        _slug("cancel-preparing"),
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)

    moved_slug = _slug("moved-address-cancel")
    await raw_conn.execute(
        "INSERT INTO slug_history (slug, title_id, episode_id, is_current) VALUES ($1, $2, $3, false)",
        moved_slug,
        other_title_id,
        episode_id,
    )
    await raw_conn.execute(
        "INSERT INTO episode_number_history (title_id, number, episode_id) "
        "VALUES ($1, 1, $2)",
        title_id,
        episode_id,
    )

    await queries.delete_never_active_title(raw_conn, title_id)

    remaining_history = await raw_conn.fetch(
        "SELECT * FROM episode_number_history WHERE episode_id = $1", episode_id
    )
    assert remaining_history == []

    surviving_slug = await raw_conn.fetchrow(
        "SELECT episode_id FROM slug_history WHERE slug = $1", moved_slug
    )
    assert surviving_slug is not None
    assert surviving_slug["episode_id"] is None
