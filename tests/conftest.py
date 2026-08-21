import os
import re
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

SIGNING_SECRET = "test-secret-do-not-use-in-prod"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def signing_secret() -> str:
    """Deterministic secret for tests — never the real MYTUBE_SECURE_LINK_SECRET."""
    return SIGNING_SECRET


@pytest.fixture
def settings(monkeypatch):
    """Settings() built from env vars supplied entirely by this fixture."""
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")

    from orchestrator.config import Settings

    return Settings()


@pytest.fixture(scope="session")
def postgres_url():
    """Session-scoped Postgres via testcontainers. Only used by integration tests.

    Plan 04 needs this fixture for migration/model tests; created here (Wave 0)
    per 01-VALIDATION.md's contract even though nothing in this plan consumes it yet.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_db_url(postgres_url) -> str:
    """`postgres_url` (testcontainers gives it back with a `+psycopg2` driver
    suffix) with plan 04's catalog schema applied, driver-less so asyncpg
    accepts it directly.

    Runs `alembic upgrade head` as a *subprocess* rather than calling
    `alembic.command.upgrade` in-process: `migrations/env.py` resolves the
    database URL through `orchestrator.config.get_settings()`, which is
    `@lru_cache`d -- an earlier test in the same session that already built
    a `Settings()` (e.g. via the `settings` fixture above) would pin the
    wrong DB URL for every later `get_settings()` call in this same
    process. A subprocess gets a clean cache for free.
    """
    bare_url = re.sub(r"^postgresql\+\w+://", "postgresql://", postgres_url)
    env = {
        **os.environ,
        "MYTUBE_DATABASE_URL": bare_url,
        "MYTUBE_SECURE_LINK_SECRET": SIGNING_SECRET,
        "MYTUBE_S3_ENDPOINT": "http://storage:8333",
        "MYTUBE_S3_KEY": "test-key",
        "MYTUBE_S3_SECRET": "test-secret",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return bare_url


@pytest.fixture
async def db_conn(migrated_db_url):
    """One asyncpg connection per test, wrapped in a transaction that is
    always rolled back at teardown -- full isolation between tests that all
    share one session-scoped testcontainers Postgres, without truncating
    tables between them.
    """
    conn = await asyncpg.connect(migrated_db_url)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
async def raw_conn(migrated_db_url):
    """A plain, non-transactional connection to the same migrated Postgres.

    The CLI (orchestrator/cli/__main__.py) opens its own `asyncpg.connect()`
    per invocation and commits for real -- `db_conn`'s rollback-at-teardown
    wrapper would be invisible to that separate connection. Tests that need
    to set up or inspect state the CLI-under-test can see use this instead.
    Rows created through it are real commits and outlive the test (the
    session-scoped Postgres is shared across this whole test run) -- tests
    using it pick unique slugs/names per test to avoid collisions.
    """
    conn = await asyncpg.connect(migrated_db_url)
    try:
        yield conn
    finally:
        await conn.close()
