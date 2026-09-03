import functools
import os
import re
import subprocess
import sys
import time
import uuid
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

# The worker-facing bearer token, forward-compatible with the field
# `MYTUBE_WORKER_TOKEN` will become once a later plan makes it required.
# Exported so test modules can build a matching `Authorization` header
# without retyping the string.
WORKER_TOKEN = "test-worker-token-do-not-use-in-prod"


@functools.lru_cache(maxsize=1)
def _docker_daemon_reachable() -> bool:
    """Probed once per session and cached -- a probe per test would add a
    subprocess call to every one of them. `docker info` is used rather
    than a bare `docker ps` because it fails fast and unambiguously when
    no daemon is listening, without needing an image pulled first.
    """
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5, check=True
        )
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Makes the `integration` marker mean what this suite's own docstrings
    already claim it means: skipped, not errored, when the Docker daemon
    the testcontainers-backed fixtures need is unreachable. Before this
    hook, the marker was only registered in pyproject.toml -- nothing
    acted on it, so a run with no Docker daemon errored deep inside the
    `postgres_url` container fixture instead of skipping cleanly.
    """
    if _docker_daemon_reachable():
        return
    skip_no_docker = pytest.mark.skip(
        reason="Docker daemon unreachable -- integration tests skipped"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_no_docker)


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
    # Forward-compatible, same reasoning as MYTUBE_ADMIN_FQDN above: this
    # field does not exist on Settings yet, so this edit is a no-op today
    # and stops being one the moment a later plan makes the field required
    # with no default -- done here, in wave 1, so that edit does not turn
    # the entire suite red in a later wave.
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)

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
        "MYTUBE_WORKER_TOKEN": WORKER_TOKEN,
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


# The cookie every screen test that drives an admin route now carries,
# alongside the `signed_in` fixture below -- imported by test modules and
# passed to their own `httpx.AsyncClient(cookies=SESSION_COOKIE)`.
SESSION_COOKIE = {"mytube_session": "test-session-token"}


@pytest.fixture
def signed_in(operator_present, monkeypatch):
    """Stubs `orchestrator.auth.session_operator_id` to a fixed operator id,
    regardless of the cookie's actual value.

    Requests `operator_present` rather than setting the existence flag a
    second time. Deliberately does not perform a real login: these tests
    are about their own screens, the gate has its own tests in
    test_auth.py, and a test that had to sign in for real before checking
    a library row would be testing the login twice and the library once.
    The cookie is still required rather than waived (`SESSION_COOKIE`
    above), so each of these tests keeps saying "this screen needs a
    session" in its own source.
    """
    from orchestrator import auth

    fixed_operator_id = uuid.uuid4()

    async def _fake_session_operator_id(request):
        return fixed_operator_id, False

    monkeypatch.setattr(auth, "session_operator_id", _fake_session_operator_id)
    yield


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

    Also resets `operator_exists`/`operator_exists_checked_at` to the
    "never checked" state -- the same reason `db_pool` needs resetting.
    `operator_exists_cached`'s own caching means a real deployment's
    lifespan queries the table exactly once at startup and trusts that
    answer (or the negative one, for a bounded interval) from then on;
    `ASGITransport` never runs that lifespan, so without this reset the
    flag would carry whatever a previous test in this same session left
    behind -- or the `getattr` default of "an operator exists" -- instead
    of asking this test's own freshly migrated/truncated table.
    """
    from orchestrator.main import app

    pool = await asyncpg.create_pool(migrated_db_url, min_size=1, max_size=5)
    monkeypatch.setattr(app.state, "db_pool", pool, raising=False)
    monkeypatch.setattr(app.state, "operator_exists", False, raising=False)
    monkeypatch.setattr(app.state, "operator_exists_checked_at", 0.0, raising=False)
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


@pytest.fixture(autouse=True)
async def _cli_auth_operator_singleton_reset(request):
    """`tests/test_cli_auth.py` exercises the same true-singleton
    `operator_credential` table `auth_tables` above exists for (see
    migration 0003's `operator_credential_singleton` index -- one row,
    full stop, whatever the username is), through `raw_conn`, a real,
    non-rolled-back connection, but names a distinct username per test
    rather than requesting `auth_tables` itself. Without a reset, the row
    one test commits is a precondition the next test does not expect, and
    the singleton constraint refuses it -- CLI commands under test
    included, since `_create_operator` asks the same table.

    Scoped to that one module by checking `request.module` before
    touching any database fixture, rather than made unconditionally
    autouse -- otherwise every test in this session, including the ones
    with no interest in Postgres at all, would pay for spinning up the
    testcontainers instance this whole suite shares.
    """
    if not request.module.__name__.endswith("test_cli_auth"):
        yield
        return

    migrated_db_url = request.getfixturevalue("migrated_db_url")
    conn = await asyncpg.connect(migrated_db_url)
    try:
        await conn.execute("TRUNCATE sessions, operator_credential CASCADE")
        yield
        await conn.execute("TRUNCATE sessions, operator_credential CASCADE")
    finally:
        await conn.close()
