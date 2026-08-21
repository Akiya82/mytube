"""Raw SQL over asyncpg for the catalog schema (titles/slug_history/seasons/
episodes/editions from migrations/versions/0001_initial_catalog.py).

Every function takes an already-open `asyncpg.Connection` (or a connection
acquired from a pool) as its first argument -- pool lifecycle is
`orchestrator.db.create_pool`'s job, not this module's.
"""
import asyncpg


async def resolve_slug(conn: asyncpg.Connection, slug: str) -> asyncpg.Record | None:
    """Resolve a slug to its title + default edition, via slug_history.

    For a given slug text there may be more than one history row (D-10 reuse:
    a tombstoned title's row plus a new title's row for the same text) --
    the newest `created_at` wins (verdict B3). This one rule covers both
    rename (single row, still resolves after rename -- ADDR-05) and reuse
    (two rows, the newer one wins, the older stays as history) without a
    special case for either.
    """
    return await conn.fetchrow(
        """
        SELECT
            t.id AS title_id,
            t.status AS status,
            ep.default_edition_id AS default_edition_id
        FROM slug_history sh
        JOIN titles t ON t.id = sh.title_id
        LEFT JOIN LATERAL (
            SELECT e.default_edition_id
            FROM episodes e
            JOIN seasons s ON s.id = e.season_id
            WHERE s.title_id = t.id
            ORDER BY s.number, e.number
            LIMIT 1
        ) ep ON true
        WHERE sh.slug = $1
        ORDER BY sh.created_at DESC
        LIMIT 1
        """,
        slug,
    )


async def resolve_edition(conn: asyncpg.Connection, edition_id) -> asyncpg.Record | None:
    """Look up an edition by id, with its title's status -- used by
    GET /refresh/{edition_id} (D-04): nginx only knows edition_id, not the
    slug it was reached through, so re-signing has to key off the edition.
    """
    return await conn.fetchrow(
        """
        SELECT e.id AS edition_id, t.status AS status
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        WHERE e.id = $1
        """,
        edition_id,
    )


async def create_title_with_edition(
    conn: asyncpg.Connection,
    name: str,
    slug: str,
    edition_id,
    object_key: str,
):
    """One transaction: title, season #1, episode #1, edition, and the
    slug's first history row -- then point episode.default_edition_id at
    the new edition (D-09).

    The slug_history insert is delegated to `reuse_slug`: mechanically it is
    the exact same statement whether `slug` is brand new or is a tombstoned
    slug being reused for this new title (D-10) -- the composite
    (slug, title_id) primary key makes both cases the same plain INSERT,
    since this call always uses a freshly created title_id. "Reuse" is a
    resolve-time property (newest created_at wins), not a code branch here.
    """
    async with conn.transaction():
        title_id = await conn.fetchval(
            "INSERT INTO titles (name) VALUES ($1) RETURNING id", name
        )
        season_id = await conn.fetchval(
            "INSERT INTO seasons (title_id) VALUES ($1) RETURNING id", title_id
        )
        episode_id = await conn.fetchval(
            "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
        )
        await conn.execute(
            "INSERT INTO editions (id, episode_id, object_key) VALUES ($1, $2, $3)",
            edition_id,
            episode_id,
            object_key,
        )
        await conn.execute(
            "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
            edition_id,
            episode_id,
        )
        await reuse_slug(conn, slug, title_id)
    return title_id


async def reuse_slug(conn: asyncpg.Connection, slug: str, new_title_id) -> None:
    """Insert a fresh current slug_history row for `new_title_id`, without
    touching any existing rows for this slug text (D-10 tombstone reuse,
    verdict B3): the composite (slug, title_id) primary key means a
    different title_id never conflicts with a tombstoned title's row for
    the same slug -- the old row survives as history, and the new row's
    created_at makes resolve_slug pick it.
    """
    # clock_timestamp(), not now() -- see the created_at column comment in
    # migrations/versions/0001_initial_catalog.py: now() is frozen for the
    # whole transaction, which would tie two slug_history rows inserted in
    # one transaction and break "newest wins".
    await conn.execute(
        """
        INSERT INTO slug_history (slug, title_id, is_current, created_at)
        VALUES ($1, $2, true, clock_timestamp())
        """,
        slug,
        new_title_id,
    )


async def rename_slug(conn: asyncpg.Connection, old_slug: str, new_slug: str):
    """Give a title a new current slug; the old slug's row stays in history
    forever (ADDR-05) -- only `is_current` moves.

    Renaming back to a slug the title held before (A -> B -> A) hits the
    composite primary key on the second rename: the (A, title_id) row
    already exists with is_current=false, so the insert becomes an upsert
    that flips it back to current with a fresh created_at -- still exactly
    two history rows, and "newest created_at wins" keeps giving the right
    answer for both slugs.
    """
    async with conn.transaction():
        title_id = await conn.fetchval(
            "SELECT title_id FROM slug_history WHERE slug = $1 AND is_current",
            old_slug,
        )
        if title_id is None:
            raise LookupError(f"slug {old_slug!r} not found or not current")

        await conn.execute(
            "UPDATE slug_history SET is_current = false WHERE title_id = $1 AND is_current",
            title_id,
        )
        await conn.execute(
            """
            INSERT INTO slug_history (slug, title_id, is_current, created_at)
            VALUES ($1, $2, true, clock_timestamp())
            ON CONFLICT (slug, title_id)
            DO UPDATE SET is_current = true, created_at = clock_timestamp()
            """,
            new_slug,
            title_id,
        )
    return title_id


async def mark_for_delete(conn: asyncpg.Connection, title_id, grace_seconds: int) -> None:
    """Flip a title to pending_delete with delete_at = now() + grace_seconds
    (D-13). GET /v/{slug} for this title starts returning 410 immediately
    -- the grace window only delays the reaper's physical delete_object.
    """
    await conn.execute(
        """
        UPDATE titles
        SET status = 'pending_delete', delete_at = now() + make_interval(secs => $2)
        WHERE id = $1
        """,
        title_id,
        grace_seconds,
    )


async def due_for_reaping(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Titles whose delete_at has passed, with every edition's object_key --
    what the reaper needs to call delete_object on before finish_delete."""
    return await conn.fetch(
        """
        SELECT t.id AS title_id, array_agg(e.object_key) AS object_keys
        FROM titles t
        JOIN seasons s ON s.title_id = t.id
        JOIN episodes ep ON ep.season_id = s.id
        JOIN editions e ON e.episode_id = ep.id
        WHERE t.status = 'pending_delete' AND t.delete_at <= now()
        GROUP BY t.id
        """
    )


async def finish_delete(conn: asyncpg.Connection, title_id) -> None:
    """Mark a title deleted once the reaper has removed its objects. Its
    slug_history rows are untouched -- the tombstone that makes /v/{slug}
    keep answering 410 instead of turning into an unexplained 404 (D-10).
    """
    await conn.execute("UPDATE titles SET status = 'deleted' WHERE id = $1", title_id)


async def taken_slugs(conn: asyncpg.Connection) -> set[str]:
    """Every slug that has ever been used -- current or tombstoned -- for
    pick_unique_slug's collision check."""
    rows = await conn.fetch("SELECT DISTINCT slug FROM slug_history")
    return {row["slug"] for row in rows}
