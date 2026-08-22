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
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")

    from orchestrator.config import Settings

    return Settings()


@pytest.fixture
def share_root(tmp_path):
    """A stand-in mount root: two folders, three files, one file named with
    markup. This machine has no CIFS client and
    `orchestrator.media.share` cannot tell the difference -- that is the
    point. Reused by this plan's tests and by later plans.
    """
    root = tmp_path / "share"
    root.mkdir()
    (root / "Movies").mkdir()
    (root / "Zzz Folder").mkdir()
    (root / "a-video.mp4").write_bytes(b"stub video bytes")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    # Markup-flavored name proving list_dir sanitizes nothing (the template
    # escapes). The literal `<script>...</script>` form used by the route-
    # level escaping test in test_share.py cannot be created as a real file
    # on this host: NTFS rejects `<`, `>` and `"` in filenames outright
    # (`[Errno 22] Invalid argument`), unlike the CIFS/NFS shares this
    # module targets in production. `&` and `'` are legal here and exercise
    # the same unescaped-passthrough behavior at the listing layer; the
    # route-level HTML-escaping test builds its dangerous filename directly
    # (see test_share.py), bypassing the filesystem instead of fighting it.
    (root / "Director's Cut & Extra (2024).mkv").write_bytes(b"stub video bytes")
    return root


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
        "MYTUBE_PUBLIC_BASE_URL": "https://mytube.example",
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
