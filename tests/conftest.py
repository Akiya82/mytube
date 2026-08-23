import os
import re
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import pytest

SIGNING_SECRET = "test-secret-do-not-use-in-prod"
REPO_ROOT = Path(__file__).resolve().parent.parent

# The admin interface's own hostname, forward-compatible with the field
# `MYTUBE_ADMIN_FQDN` will become once phase 02.1 makes it required.
# Exported so test modules can build a matching `X-Forwarded-Host` header
# without retyping the string.
ADMIN_FQDN = "admin.mytube.example"


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
    # Forward-compatible: this field does not exist on Settings yet, and
    # pydantic-settings ignores an unknown MYTUBE_ variable, so the suite
    # stays green on this edit alone. It stops being a no-op the moment
    # plan 04 makes the field required.
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)

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
        "MYTUBE_ADMIN_FQDN": ADMIN_FQDN,
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


@pytest.fixture
def operator_present(monkeypatch):
    """Stamps `app.state.operator_exists = True`, restored afterwards.

    Every gated unit test that expects a redirect to the login form (rather
    than to the create-password page) requests this fixture. It
    deliberately does not stub the session lookup: with no cookie on the
    request, `session_operator_id` returns `None` before it ever reaches a
    pool, so a no-session test stays a real no-session test.
    """
    from orchestrator.main import app

    monkeypatch.setattr(app.state, "operator_exists", True, raising=False)
    yield


@pytest.fixture
def no_operator(monkeypatch):
    """Stamps the first-run state onto `app.state`: no operator, a known
    setup token, and a fresh recheck timestamp.

    The timestamp matters: the flag's cache re-queries the database once
    its recheck interval has elapsed, and a fixture that left the
    timestamp at its default would send a unit test with no pool into that
    query.
    """
    from orchestrator.main import app

    monkeypatch.setattr(app.state, "operator_exists", False, raising=False)
    monkeypatch.setattr(
        app.state, "operator_exists_checked_at", time.monotonic(), raising=False
    )
    monkeypatch.setattr(app.state, "setup_token", "test-setup-token", raising=False)
    yield


@pytest.fixture
async def app_pool(migrated_db_url, monkeypatch):
    """A real asyncpg pool against `migrated_db_url`, assigned to
    `orchestrator.main.app.state.db_pool`.

    Without this the integration half cannot run at all: the application
    reaches Postgres only through `app.state.db_pool`, and
    `httpx.ASGITransport` never runs the lifespan that would otherwise
    create one. `db_conn`/`raw_conn` are connections, not pools -- nothing
    in this suite has needed the application itself to talk to a database
    before now; the existing integration tests call query functions
    directly. Every test here that signs in, signs out, or watches a
    session window slide goes through the application, so every one of
    them needs this.

    `min_size=1, max_size=5`: the default is ten connections per pool, and
    this fixture is created fresh per test, so ten would multiply quickly
    against the one testcontainers Postgres this whole session shares.
    """
    from orchestrator.main import app

    pool = await asyncpg.create_pool(migrated_db_url, min_size=1, max_size=5)
    monkeypatch.setattr(app.state, "db_pool", pool, raising=False)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def auth_tables(raw_conn):
    """Truncates `sessions` and `operator_credential` before and after the
    test.

    Both tables are global singletons -- one operator row, one credential
    per database by construction -- and this suite shares one
    session-scoped Postgres across every module. Rows committed by one
    test are preconditions for the next without this, so the outcome of
    five tests would depend on the order pytest happened to collect them.

    Uses `raw_conn`, not `db_conn`: inside a transaction `now()` is the
    transaction's start time, so anything that depends on time passing --
    the sliding window first of all -- cannot be observed there however
    correct the query is. These tests commit for real and clean up after
    themselves instead.
    """
    await raw_conn.execute("TRUNCATE sessions, operator_credential CASCADE")
    yield
    await raw_conn.execute("TRUNCATE sessions, operator_credential CASCADE")
