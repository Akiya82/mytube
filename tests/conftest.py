import pytest

SIGNING_SECRET = "test-secret-do-not-use-in-prod"


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
