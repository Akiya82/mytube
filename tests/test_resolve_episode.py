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
import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.resolve import EDITION_PREPARING_STATUS

from conftest import ADMIN_FQDN, WORKER_TOKEN

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _resolve_app_env(settings):
    """Pulls in conftest's `settings` fixture for its env-var side effect
    (get_settings() otherwise raises) and clears the @lru_cache both before
    and after, the same shape every other module driving the real
    application through httpx already uses.
    """
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _normalize_new_statuses_after_test(raw_conn):
    """The identical cleanup test_queries.py's own fixture of the same name
    performs, needed for the identical reason: this file's own tests leave
    titles in 'preparing' behind (the preparing-status HTTP tests above all
    do), and migration 0002's own downgrade re-adds the narrower three-
    value titles_status_check -- a 'preparing' row still present when that
    downgrade runs violates it outright. test_slug_history.py's full
    round-trip test (downgrade base, upgrade head) shares this same
    session-scoped Postgres and runs the full chain through 0002, so
    without this, its result would depend on which of this file's tests
    ran first.
    """
    yield
    await raw_conn.execute(
        "UPDATE titles SET status = 'active' WHERE status IN ('preparing', 'failed')"
    )


@pytest.fixture
async def http_client(app_pool):
    """The real application, wired to the real migrated Postgres via
    `app_pool` -- no db.queries monkeypatching. `/v/` and `/refresh/` are
    host-exempt (orchestrator.auth.HOST_EXEMPT_PREFIXES), so no session and
    no matching admin hostname is needed to reach them.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


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
    afterwards claims its own row (create_title_with_edition's own
    `number` argument, added once this schema exists) and would prove
    nothing about the backfill this migration itself performs.

    Seeded by raw SQL rather than through create_title_with_edition: that
    function now writes its own current episode_number_history row on
    every call, which the 0005 schema this test downgrades to has no table
    for at all -- this is exactly the pre-migration row shape the real
    function itself used to produce before this phase existed.
    """
    _run_alembic(migrated_db_url, "downgrade", "0005")

    conn = await asyncpg.connect(migrated_db_url)
    try:
        title_id = await conn.fetchval(
            "INSERT INTO titles (name) VALUES ($1) RETURNING id", "Pre-Migration Title"
        )
        season_id = await conn.fetchval(
            "INSERT INTO seasons (title_id) VALUES ($1) RETURNING id", title_id
        )
        episode_id = await conn.fetchval(
            "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
        )
        edition_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO editions (id, episode_id, object_key) VALUES ($1, $2, $3)",
            edition_id,
            episode_id,
            "premigration.mp4",
        )
        await conn.execute(
            "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
            edition_id,
            episode_id,
        )
        await conn.execute(
            "INSERT INTO slug_history (slug, title_id) VALUES ($1, $2)",
            _slug("premigration"),
            title_id,
        )
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

    # create_title_with_edition already wrote this episode's own current
    # episode_number_history row (its own `number` claim) -- nothing more
    # to seed there.
    moved_slug = _slug("moved-address")
    await raw_conn.execute(
        "INSERT INTO slug_history (slug, title_id, episode_id, is_current) VALUES ($1, $2, $3, false)",
        moved_slug,
        other_title_id,
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


# --- Task 2: the queries the rest of the phase reads and writes through ---


async def _season_id_for_title(conn, title_id) -> uuid.UUID:
    return await conn.fetchval("SELECT id FROM seasons WHERE title_id = $1", title_id)


async def _add_episode(
    conn,
    title_id,
    season_id,
    number: int,
    *,
    edition_status: str = "active",
    edition_slug: str | None = None,
) -> tuple:
    """A second (or later) episode of an existing title, added directly by
    SQL -- no folder-add flow exists yet to drive this through, so the test
    setup builds the row shape a later plan's own add path will produce.
    """
    edition_id = uuid.uuid4()
    episode_id = await conn.fetchval(
        "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
    )
    await conn.execute(
        """
        INSERT INTO editions (id, episode_id, object_key, status, slug)
        VALUES ($1, $2, $3, $4, $5)
        """,
        edition_id,
        episode_id,
        f"{edition_id}.mp4",
        edition_status,
        edition_slug,
    )
    await conn.execute(
        "UPDATE episodes SET default_edition_id = $1 WHERE id = $2", edition_id, episode_id
    )
    await queries.claim_episode_number(conn, title_id, episode_id, number)
    return episode_id, edition_id


async def _seed_series(conn, tag: str, episode_count: int = 3) -> tuple:
    """A title with `episode_count` episodes numbered 1..N, each with one
    active edition. Episode 1 is the one create_title_with_edition itself
    makes; the rest are added directly via `_add_episode`.
    """
    edition_id = uuid.uuid4()
    slug = _slug(tag)
    title_id = await queries.create_title_with_edition(
        conn, f"Series {tag}", slug, edition_id, f"{edition_id}.mp4"
    )
    season_id = await _season_id_for_title(conn, title_id)
    episodes = {1: await _episode_id_for_title(conn, title_id)}
    for n in range(2, episode_count + 1):
        episode_id, _ = await _add_episode(conn, title_id, season_id, n)
        episodes[n] = episode_id
    return title_id, slug, episodes


async def test_resolve_episode_slug_resolves_each_of_three_episodes_by_number(raw_conn):
    _title_id, slug, episodes = await _seed_series(raw_conn, "three-eps", episode_count=3)

    for number, episode_id in episodes.items():
        row = await queries.resolve_episode_slug(raw_conn, slug, number)
        assert row is not None
        assert row["episode_id"] == episode_id
        assert row["status"] == "active"
        assert row["edition_id"] is not None


async def test_resolve_episode_slug_resolves_every_leading_zero_form_of_its_number(raw_conn):
    """The number arrives at the query already parsed to an integer -- see
    orchestrator.resolve for where every leading-zero string form of a
    number collapses onto the identical int before this function ever
    runs. This proves that once collapsed, every form reaches the same
    episode.
    """
    _title_id, slug, episodes = await _seed_series(raw_conn, "leading-zero", episode_count=1)
    episode_id = episodes[1]

    for form in ("1", "01", "001", "0001"):
        row = await queries.resolve_episode_slug(raw_conn, slug, int(form))
        assert row is not None
        assert row["episode_id"] == episode_id


async def test_resolve_episode_slug_unknown_number_resolves_to_nothing(raw_conn):
    _title_id, slug, _episodes = await _seed_series(raw_conn, "unknown-number", episode_count=1)
    row = await queries.resolve_episode_slug(raw_conn, slug, 999)
    assert row is None


async def test_resolve_episode_slug_resolves_a_named_edition_and_an_unknown_edition_resolves_to_nothing(
    raw_conn,
):
    _title_id, slug, episodes = await _seed_series(raw_conn, "edition-slug", episode_count=1)
    episode_id = episodes[1]

    alt_edition_id = uuid.uuid4()
    await raw_conn.execute(
        """
        INSERT INTO editions (id, episode_id, object_key, status, slug)
        VALUES ($1, $2, $3, 'active', $4)
        """,
        alt_edition_id,
        episode_id,
        f"{alt_edition_id}.mp4",
        "dub-en",
    )

    row = await queries.resolve_episode_slug(raw_conn, slug, 1, edition_slug="dub-en")
    assert row is not None
    assert row["edition_id"] == alt_edition_id

    row_unknown = await queries.resolve_episode_slug(
        raw_conn, slug, 1, edition_slug="no-such-edition"
    )
    assert row_unknown is not None
    assert row_unknown["edition_id"] is None


async def test_resolve_title_edition_slug_resolves_an_edition_of_the_lowest_episode(raw_conn):
    edition_id = uuid.uuid4()
    slug = _slug("film")
    title_id = await queries.create_title_with_edition(
        raw_conn, "A Film", slug, edition_id, f"{edition_id}.mp4"
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)
    alt_edition_id = uuid.uuid4()
    await raw_conn.execute(
        """
        INSERT INTO editions (id, episode_id, object_key, status, slug)
        VALUES ($1, $2, $3, 'active', $4)
        """,
        alt_edition_id,
        episode_id,
        f"{alt_edition_id}.mp4",
        "directors-cut",
    )

    row = await queries.resolve_title_edition_slug(raw_conn, slug, "directors-cut")
    assert row is not None
    assert row["episode_id"] == episode_id
    assert row["edition_id"] == alt_edition_id

    row_unknown = await queries.resolve_title_edition_slug(raw_conn, slug, "no-such-edition")
    assert row_unknown is None


async def test_resolve_episode_slug_reports_different_statuses_for_a_preparing_and_an_active_episode(
    raw_conn,
):
    edition_id = uuid.uuid4()
    slug = _slug("mixed-readiness")
    title_id = await queries.create_title_with_edition(
        raw_conn, "Mixed Readiness", slug, edition_id, f"{edition_id}.mp4"
    )
    season_id = await _season_id_for_title(raw_conn, title_id)
    await _add_episode(raw_conn, title_id, season_id, 2, edition_status="preparing")

    row_active = await queries.resolve_episode_slug(raw_conn, slug, 1)
    row_preparing = await queries.resolve_episode_slug(raw_conn, slug, 2)
    assert row_active["edition_status"] == "active"
    assert row_preparing["edition_status"] == "preparing"


async def test_claim_episode_number_renumber_keeps_the_old_number_until_a_second_episode_claims_it(
    raw_conn,
):
    title_id, slug, episodes = await _seed_series(raw_conn, "renumber", episode_count=2)
    episode_1, episode_2 = episodes[1], episodes[2]

    await queries.claim_episode_number(raw_conn, title_id, episode_1, 5)

    row_new = await queries.resolve_episode_slug(raw_conn, slug, 5)
    assert row_new["episode_id"] == episode_1
    row_old = await queries.resolve_episode_slug(raw_conn, slug, 1)
    assert row_old["episode_id"] == episode_1

    await queries.claim_episode_number(raw_conn, title_id, episode_2, 1)
    row_reclaimed = await queries.resolve_episode_slug(raw_conn, slug, 1)
    assert row_reclaimed["episode_id"] == episode_2


async def test_taken_episode_numbers_and_episode_for_number_read_the_current_history(raw_conn):
    title_id, _slug_value, episodes = await _seed_series(raw_conn, "number-history", episode_count=2)
    episode_1, episode_2 = episodes[1], episodes[2]

    assert await queries.taken_episode_numbers(raw_conn, title_id) == {1, 2}

    row = await queries.episode_for_number(raw_conn, title_id, 1)
    assert row["episode_id"] == episode_1
    assert row["is_current"] is True

    # After a renumber, the old number's history row is no longer current
    # but still resolves -- exactly the "in use now" vs. "used to be this
    # episode's number" distinction episode_for_number exists to answer.
    await queries.claim_episode_number(raw_conn, title_id, episode_1, 9)
    row_after = await queries.episode_for_number(raw_conn, title_id, 1)
    assert row_after["episode_id"] == episode_1
    assert row_after["is_current"] is False
    assert await queries.taken_episode_numbers(raw_conn, title_id) == {9, 2}


async def test_resolve_slug_with_an_episode_pointer_resolves_to_that_episode_not_the_lowest(
    raw_conn,
):
    title_id, _series_slug, episodes = await _seed_series(
        raw_conn, "pointer-series", episode_count=2
    )
    episode_2_id = episodes[2]

    moved_slug = _slug("moved-pointer")
    await raw_conn.execute(
        "INSERT INTO slug_history (slug, title_id, episode_id, is_current) "
        "VALUES ($1, $2, $3, false)",
        moved_slug,
        title_id,
        episode_2_id,
    )

    row = await queries.resolve_slug(raw_conn, moved_slug)
    assert row is not None
    assert row["episode_id"] == episode_2_id


async def test_resolve_slug_with_a_null_pointer_still_resolves_to_the_lowest_episode(raw_conn):
    _title_id, series_slug, episodes = await _seed_series(raw_conn, "pointer-null", episode_count=2)
    episode_1_id = episodes[1]

    row = await queries.resolve_slug(raw_conn, series_slug)
    assert row is not None
    assert row["episode_id"] == episode_1_id


async def test_a_title_created_with_no_number_argument_resolves_at_number_one(raw_conn):
    edition_id = uuid.uuid4()
    slug = _slug("created-default-number")
    await queries.create_title_with_edition(
        raw_conn, "Created Default Number", slug, edition_id, f"{edition_id}.mp4"
    )

    row = await queries.resolve_episode_slug(raw_conn, slug, 1)
    assert row is not None
    assert row["status"] == "active"


async def test_a_title_created_with_an_explicit_number_resolves_at_that_number(raw_conn):
    edition_id = uuid.uuid4()
    slug = _slug("created-explicit-number")
    await queries.create_title_with_edition(
        raw_conn, "Created Explicit Number", slug, edition_id, f"{edition_id}.mp4", number=7
    )

    row = await queries.resolve_episode_slug(raw_conn, slug, 7)
    assert row is not None

    row_absent = await queries.resolve_episode_slug(raw_conn, slug, 1)
    assert row_absent is None


async def test_resolve_edition_reports_episode_and_edition_status_across_preparing_deleted_and_active(
    raw_conn,
):
    edition_id = uuid.uuid4()
    slug = _slug("resolve-edition-statuses")
    title_id = await queries.create_title_with_edition(
        raw_conn, "Resolve Edition Statuses", slug, edition_id, f"{edition_id}.mp4"
    )

    row_active = await queries.resolve_edition(raw_conn, edition_id)
    assert row_active["episode_status"] == "active"
    assert row_active["edition_status"] == "active"

    season_id = await _season_id_for_title(raw_conn, title_id)
    _preparing_episode_id, preparing_edition_id = await _add_episode(
        raw_conn, title_id, season_id, 2, edition_status="preparing"
    )
    row_preparing = await queries.resolve_edition(raw_conn, preparing_edition_id)
    assert row_preparing["episode_status"] == "active"
    assert row_preparing["edition_status"] == "preparing"

    deleted_episode_id, deleted_edition_id = await _add_episode(
        raw_conn, title_id, season_id, 3, edition_status="active"
    )
    await raw_conn.execute(
        "UPDATE episodes SET status = 'deleted' WHERE id = $1", deleted_episode_id
    )
    row_deleted = await queries.resolve_edition(raw_conn, deleted_edition_id)
    assert row_deleted["episode_status"] == "deleted"


# --- Task 3: the two new public addresses, the preparing stub, and the ---
# --- edition-slug rule, driven through the real application            ---


async def test_http_numbered_address_redirects_with_a_signed_location(raw_conn, http_client):
    _title_id, slug, episodes = await _seed_series(raw_conn, "http-numbered", episode_count=1)

    resp = await http_client.get(f"/v/{slug}/1", follow_redirects=False)

    assert resp.status_code == 302
    assert "/blob/" in resp.headers["location"]
    assert "sig=" in resp.headers["location"]


async def test_http_leading_zero_forms_reach_the_same_episode(raw_conn, http_client):
    _title_id, slug, episodes = await _seed_series(raw_conn, "http-leading-zero", episode_count=1)

    locations = set()
    for form in ("1", "01", "001"):
        resp = await http_client.get(f"/v/{slug}/{form}", follow_redirects=False)
        assert resp.status_code == 302
        locations.add(resp.headers["location"].split("?")[0])

    assert len(locations) == 1


async def test_http_non_numeric_segment_reaches_the_film_edition_branch(raw_conn, http_client):
    edition_id = uuid.uuid4()
    slug = _slug("http-film")
    title_id = await queries.create_title_with_edition(
        raw_conn, "HTTP Film", slug, edition_id, f"{edition_id}.mp4"
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)
    alt_edition_id = uuid.uuid4()
    await raw_conn.execute(
        """
        INSERT INTO editions (id, episode_id, object_key, status, slug)
        VALUES ($1, $2, $3, 'active', $4)
        """,
        alt_edition_id,
        episode_id,
        f"{alt_edition_id}.mp4",
        "directors-cut",
    )

    resp = await http_client.get(f"/v/{slug}/directors-cut", follow_redirects=False)
    assert resp.status_code == 302
    assert f"/blob/{alt_edition_id}.mp4" in resp.headers["location"]


async def test_http_three_segment_address_reaches_the_edition_of_that_episode(
    raw_conn, http_client
):
    _title_id, slug, episodes = await _seed_series(
        raw_conn, "http-episode-edition", episode_count=1
    )
    episode_id = episodes[1]
    alt_edition_id = uuid.uuid4()
    await raw_conn.execute(
        """
        INSERT INTO editions (id, episode_id, object_key, status, slug)
        VALUES ($1, $2, $3, 'active', $4)
        """,
        alt_edition_id,
        episode_id,
        f"{alt_edition_id}.mp4",
        "dub-en",
    )

    resp = await http_client.get(f"/v/{slug}/1/dub-en", follow_redirects=False)
    assert resp.status_code == 302
    assert f"/blob/{alt_edition_id}.mp4" in resp.headers["location"]


async def test_http_preparing_answers_the_preparing_status_from_all_four_routes(
    raw_conn, http_client
):
    edition_id = uuid.uuid4()
    slug = _slug("http-preparing")
    title_id = await queries.create_title_with_edition(
        raw_conn, "HTTP Preparing", slug, edition_id, f"{edition_id}.mp4", status="preparing"
    )
    await raw_conn.execute("UPDATE editions SET status = 'preparing' WHERE id = $1", edition_id)

    bare_resp = await http_client.get(f"/v/{slug}", follow_redirects=False)
    numbered_resp = await http_client.get(f"/v/{slug}/1", follow_redirects=False)
    refresh_resp = await http_client.get(f"/refresh/{edition_id}", follow_redirects=False)

    assert bare_resp.status_code == EDITION_PREPARING_STATUS
    assert numbered_resp.status_code == EDITION_PREPARING_STATUS
    assert refresh_resp.status_code == EDITION_PREPARING_STATUS
    for resp in (bare_resp, numbered_resp, refresh_resp):
        assert resp.status_code != 410


async def test_http_deleted_episode_answers_gone_from_all_four_routes(raw_conn, http_client):
    edition_id = uuid.uuid4()
    slug = _slug("http-deleted-episode")
    title_id = await queries.create_title_with_edition(
        raw_conn, "HTTP Deleted Episode", slug, edition_id, f"{edition_id}.mp4"
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)
    await raw_conn.execute("UPDATE episodes SET status = 'deleted' WHERE id = $1", episode_id)

    bare_resp = await http_client.get(f"/v/{slug}", follow_redirects=False)
    numbered_resp = await http_client.get(f"/v/{slug}/1", follow_redirects=False)
    refresh_resp = await http_client.get(f"/refresh/{edition_id}", follow_redirects=False)

    assert bare_resp.status_code == 410
    assert numbered_resp.status_code == 410
    assert refresh_resp.status_code == 410


async def test_http_unknown_number_answers_not_found(raw_conn, http_client):
    _title_id, slug, _episodes = await _seed_series(
        raw_conn, "http-unknown-number", episode_count=1
    )

    resp = await http_client.get(f"/v/{slug}/999", follow_redirects=False)
    assert resp.status_code == 404


async def test_http_absurdly_long_numeric_segment_answers_not_found_not_raising(
    raw_conn, http_client
):
    _title_id, slug, _episodes = await _seed_series(
        raw_conn, "http-absurd-segment", episode_count=1
    )

    resp = await http_client.get(f"/v/{slug}/{'9' * 40}", follow_redirects=False)
    assert resp.status_code == 404


async def test_http_a_title_created_after_the_migration_answers_at_its_numbered_address(
    raw_conn, http_client
):
    edition_id = uuid.uuid4()
    slug = _slug("http-created-after")
    await queries.create_title_with_edition(
        raw_conn, "HTTP Created After Migration", slug, edition_id, f"{edition_id}.mp4"
    )

    bare_resp = await http_client.get(f"/v/{slug}", follow_redirects=False)
    numbered_resp = await http_client.get(f"/v/{slug}/01", follow_redirects=False)

    assert bare_resp.status_code == 302
    assert numbered_resp.status_code == 302
    assert (
        bare_resp.headers["location"].split("?")[0]
        == numbered_resp.headers["location"].split("?")[0]
    )


async def test_http_bare_address_with_an_episode_pointer_plays_that_episode(
    raw_conn, http_client
):
    title_id, _series_slug, episodes = await _seed_series(
        raw_conn, "http-pointer-series", episode_count=2
    )
    episode_2_id = episodes[2]
    episode_2_edition = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_2_id
    )

    moved_slug = _slug("http-moved-pointer")
    await raw_conn.execute(
        "INSERT INTO slug_history (slug, title_id, episode_id, is_current) "
        "VALUES ($1, $2, $3, false)",
        moved_slug,
        title_id,
        episode_2_id,
    )

    resp = await http_client.get(f"/v/{moved_slug}", follow_redirects=False)
    assert resp.status_code == 302
    assert f"/blob/{episode_2_edition}.mp4" in resp.headers["location"]

    # Also answers preparing correctly when that specific episode is not
    # ready -- the whole point of the pointer is that it is this episode's
    # own readiness that decides, not the series' first episode.
    await raw_conn.execute(
        "UPDATE editions SET status = 'preparing' WHERE id = $1", episode_2_edition
    )
    resp_preparing = await http_client.get(f"/v/{moved_slug}", follow_redirects=False)
    assert resp_preparing.status_code == EDITION_PREPARING_STATUS


async def test_http_refresh_of_a_preparing_and_a_deleted_edition(raw_conn, http_client):
    _title_id, slug, episodes = await _seed_series(
        raw_conn, "http-refresh-statuses", episode_count=2
    )
    episode_1_edition = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episodes[1]
    )
    episode_2_edition = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episodes[2]
    )
    await raw_conn.execute(
        "UPDATE editions SET status = 'preparing' WHERE id = $1", episode_1_edition
    )
    await raw_conn.execute("UPDATE episodes SET status = 'deleted' WHERE id = $1", episodes[2])

    preparing_resp = await http_client.get(
        f"/refresh/{episode_1_edition}", follow_redirects=False
    )
    deleted_resp = await http_client.get(f"/refresh/{episode_2_edition}", follow_redirects=False)

    assert preparing_resp.status_code == EDITION_PREPARING_STATUS
    assert deleted_resp.status_code == 410
    assert "location" not in preparing_resp.headers
    assert "location" not in deleted_resp.headers


async def test_http_a_title_with_one_ready_and_one_preparing_episode_serves_both(
    raw_conn, http_client
):
    _title_id, slug, episodes = await _seed_series(
        raw_conn, "http-mixed-readiness", episode_count=1
    )
    season_id = await _season_id_for_title(raw_conn, _title_id)
    await _add_episode(raw_conn, _title_id, season_id, 2, edition_status="preparing")

    ready_resp = await http_client.get(f"/v/{slug}/1", follow_redirects=False)
    preparing_resp = await http_client.get(f"/v/{slug}/2", follow_redirects=False)

    assert ready_resp.status_code == 302
    assert preparing_resp.status_code == EDITION_PREPARING_STATUS
