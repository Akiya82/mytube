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


# Constant key for the advisory lock create_title_with_edition takes as its
# first statement -- see that function's docstring for the race it closes
# and, more importantly, for what a caller must do for the lock to protect
# anything at all.
SLUG_CLAIM_LOCK_KEY = 747474747


async def create_title_with_edition(
    conn: asyncpg.Connection,
    name: str,
    slug: str,
    edition_id,
    object_key: str,
    *,
    status: str = "active",
    source_path: str | None = None,
    audio_stream_index: int | None = None,
    audio_channels: int | None = None,
):
    """One transaction: title, season #1, episode #1, edition, and the
    slug's first history row -- then point episode.default_edition_id at
    the new edition (D-09).

    `status` defaults to 'active' (the CLI's existing behaviour, unchanged).
    The web add flow (D-49) passes `status="preparing"` instead, so the row
    exists and the slug is reserved before any ffmpeg/upload work starts.
    `source_path` (D-48), `audio_stream_index` and `audio_channels` (the two
    facts a background job needs to rebuild itself from the catalog alone,
    see `job_row`) land straight on the edition row; all three are optional
    because the CLI path still has none of them.

    The slug_history insert is delegated to `reuse_slug`: mechanically it is
    the exact same statement whether `slug` is brand new or is a tombstoned
    slug being reused for this new title (D-10) -- the composite
    (slug, title_id) primary key makes both cases the same plain INSERT,
    since this call always uses a freshly created title_id. "Reuse" is a
    resolve-time property (newest created_at wins), not a code branch here.

    **Concurrent slug claims.** The first statement inside this transaction
    takes a transaction-scoped advisory lock on the constant key
    SLUG_CLAIM_LOCK_KEY. This is the whole fix for a real race:
    slug_history's primary key is (slug, title_id), so two different
    titles claiming the same slug text is legal by construction -- it is
    what tombstone reuse (D-10) depends on. Nothing in the schema stops
    two concurrent adds from both reading `taken_slugs()`, both picking the
    same free candidate, and both inserting: no error is raised,
    `resolve_slug` takes the newest row, and the first title silently
    becomes unreachable at the address it was given.

    This lock only protects what happens *after* it is acquired. For the
    protection to actually hold, a caller that plans a slug (`taken_slugs`
    + `pick_unique_slug`) must do so on a connection that has already
    opened a transaction and already taken this exact lock -- e.g. open
    `async with conn.transaction():`, take the SLUG_CLAIM_LOCK_KEY advisory
    lock first, then `taken = await taken_slugs(conn)`, `slug, similar =
    pick_unique_slug(candidate, taken)`, then
    `await create_title_with_edition(conn, ...)`. Under that pattern this
    function's own `async with conn.transaction():` nests as a harmless
    savepoint, and its own lock acquisition is a redundant, session-
    reentrant re-acquire of the lock the caller already holds -- Postgres
    advisory locks count re-acquisitions by the same session rather than
    deadlocking against itself. A caller that reads `taken_slugs` before
    opening any transaction gets no protection from this lock at all: the
    lock serialises the read-and-insert *pair*, not a slug computed earlier
    and handed in as a plain string.

    Do not remove this lock because it looks like it protects nothing in a
    single-operator manual test -- the race it closes needs two connections
    racing on purpose to show up (see test_queries.py), and by the time it
    shows up for real the wrong title has already gone dark.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SLUG_CLAIM_LOCK_KEY)
        title_id = await conn.fetchval(
            "INSERT INTO titles (name, status) VALUES ($1, $2) RETURNING id",
            name,
            status,
        )
        season_id = await conn.fetchval(
            "INSERT INTO seasons (title_id) VALUES ($1) RETURNING id", title_id
        )
        episode_id = await conn.fetchval(
            "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
        )
        await conn.execute(
            """
            INSERT INTO editions (
                id, episode_id, object_key, source_path,
                audio_stream_index, audio_channels
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            edition_id,
            episode_id,
            object_key,
            source_path,
            audio_stream_index,
            audio_channels,
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
    """Titles whose delete_at has passed, with every edition's object_key
    and poster_key -- what the reaper needs to call delete_object on before
    finish_delete. Poster keys included (D-72): a poster left behind when
    its video goes is exactly the orphan class this project has already
    accumulated once. `poster_keys` only ever holds the non-null ones, so
    callers never see a literal `NULL` entry to skip.
    """
    return await conn.fetch(
        """
        SELECT
            t.id AS title_id,
            array_agg(e.object_key) AS object_keys,
            array_agg(e.poster_key) FILTER (WHERE e.poster_key IS NOT NULL) AS poster_keys
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


async def mark_title_active(conn: asyncpg.Connection, title_id) -> None:
    """Flip a 'preparing' title to 'active' on a successful add (D-49) --
    clears failure_reason too, so the column never lies if this same title
    id is ever reused after an earlier failure.
    """
    await conn.execute(
        "UPDATE titles SET status = 'active', failure_reason = NULL WHERE id = $1",
        title_id,
    )


async def mark_title_failed(conn: asyncpg.Connection, title_id, reason_key: str) -> None:
    """Flip a title to 'failed' with `reason_key`, a translation catalog
    key (e.g. 'failed.reason.upload') -- never an English sentence. See the
    failure_reason column's comment in
    migrations/versions/0002_add_as_is.py for why: the interface is
    localized (D-66), and a sentence baked in at write time cannot be
    translated at render time.
    """
    await conn.execute(
        "UPDATE titles SET status = 'failed', failure_reason = $2 WHERE id = $1",
        title_id,
        reason_key,
    )


async def mark_title_started(conn: asyncpg.Connection, title_id) -> None:
    """Set started_at to clock_timestamp() the moment a worker actually
    dequeues this title -- called by the worker, never by the route. See
    the started_at column's comment in migrations/versions/0002_add_as_is.py
    for why "queued" and "started" have to be told apart.
    """
    await conn.execute(
        "UPDATE titles SET started_at = clock_timestamp() WHERE id = $1", title_id
    )


async def preparing_rows_at_startup(
    conn: asyncpg.Connection,
) -> tuple[list[asyncpg.Record], list[asyncpg.Record]]:
    """Every title still 'preparing' at process startup, split into
    (queued, started) on whether started_at is null.

    Replaces the single-verdict reconcile_interrupted_adds sketched in
    01-RESEARCH.md: a row that had started is genuinely interrupted
    mid-remux/upload (D-53) and should be marked failed; a row that had
    not started yet never began any work at all, and telling the operator
    it was "interrupted" would be a lie about a row that was only ever
    sitting in the queue -- it should simply be re-enqueued (using
    `job_row` to rebuild the job, since the in-process queue holds nothing
    across a restart).
    """
    rows = await conn.fetch(
        "SELECT id AS title_id, started_at FROM titles WHERE status = 'preparing'"
    )
    queued = [row for row in rows if row["started_at"] is None]
    started = [row for row in rows if row["started_at"] is not None]
    return queued, started


async def job_row(conn: asyncpg.Connection, title_id) -> asyncpg.Record | None:
    """The columns needed to rebuild an add job from the catalog alone --
    edition id, object key, source path, the operator's chosen audio
    stream index and channel count. Used by the startup re-enqueue
    (preparing_rows_at_startup's queued list) and by the add route, so
    both build a job from the same one place instead of two shapes that
    can drift apart.

    `poster_key` rides along too: it is not part of `AddJob`'s own shape
    (a job never needs to know its own not-yet-extracted poster key), but
    the startup sweep's cleanup of a genuinely interrupted row (D-53)
    needs both the video and the poster key from the exact same query --
    a second query for the poster alone would be one more place the two
    facts could read a different edition_id than each other.
    """
    return await conn.fetchrow(
        """
        SELECT
            e.id AS edition_id,
            e.object_key AS object_key,
            e.source_path AS source_path,
            e.audio_stream_index AS audio_stream_index,
            e.audio_channels AS audio_channels,
            e.poster_key AS poster_key
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        WHERE s.title_id = $1
        """,
        title_id,
    )


async def set_edition_technical(
    conn: asyncpg.Connection,
    edition_id,
    *,
    size_bytes: int,
    duration_seconds: float,
    width: int,
    height: int,
    audio_language: str | None,
    audio_title: str | None,
    audio_reencoded: bool,
) -> None:
    """Record the technical detail ffprobe already produced (D-60) -- the
    library row's size/duration/resolution/audio-track line, all of it
    known before a single byte is remuxed.
    """
    await conn.execute(
        """
        UPDATE editions
        SET size_bytes = $2, duration_seconds = $3, width = $4, height = $5,
            audio_language = $6, audio_title = $7, audio_reencoded = $8
        WHERE id = $1
        """,
        edition_id,
        size_bytes,
        duration_seconds,
        width,
        height,
        audio_language,
        audio_title,
        audio_reencoded,
    )


async def set_edition_poster(conn: asyncpg.Connection, edition_id, poster_key: str) -> None:
    """Point the edition at its extracted poster frame (D-72). Never called
    for a failed extraction -- the library row degrades to a placeholder
    when poster_key stays null, rather than a broken <img>.
    """
    await conn.execute(
        "UPDATE editions SET poster_key = $2 WHERE id = $1", edition_id, poster_key
    )


async def editions_by_source_paths(
    conn: asyncpg.Connection, paths: list[str]
) -> list[asyncpg.Record]:
    """Which of these share-relative paths already have an edition -- the
    fact both the share listing's "already added" chip and the add form's
    warning banner need (D-48). Callers pass every path in the folder
    being rendered and get back only the matches: one query per rendered
    folder, never one query per row.

    Each row carries the edition's source_path, the title's name and
    status, and the title's *current* slug (via slug_history) -- the
    listing links to /v/{slug}, not to the edition id.
    """
    if not paths:
        return []
    return await conn.fetch(
        """
        SELECT
            e.source_path AS source_path,
            t.id AS title_id,
            t.name AS title_name,
            t.status AS title_status,
            sh.slug AS slug
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
        WHERE e.source_path = ANY($1::text[])
        """,
        paths,
    )


async def count_preparing_ahead(conn: asyncpg.Connection, title_id) -> int:
    """How many 'preparing' titles were created before this one -- the
    queue position the "waiting, N ahead of you" line needs (D-49/D-50).
    Computed from the database, not the in-memory queue: the queue is
    empty again the instant the process restarts, and the row is not.
    """
    return await conn.fetchval(
        """
        SELECT count(*) FROM titles
        WHERE status = 'preparing'
          AND created_at < (SELECT created_at FROM titles WHERE id = $1)
        """,
        title_id,
    )


_LIBRARY_ROW_COLUMNS = """
    t.id AS title_id, t.name AS title_name, t.status AS title_status,
    t.failure_reason AS failure_reason, t.delete_at AS delete_at,
    t.started_at AS started_at, t.created_at AS created_at,
    sh.slug AS slug,
    e.id AS edition_id, e.size_bytes AS size_bytes,
    e.duration_seconds AS duration_seconds, e.width AS width, e.height AS height,
    e.audio_language AS audio_language, e.audio_title AS audio_title,
    e.audio_reencoded AS audio_reencoded, e.poster_key AS poster_key
"""

_LIBRARY_ROW_FROM = """
    FROM titles t
    LEFT JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
    LEFT JOIN seasons s ON s.title_id = t.id
    LEFT JOIN episodes ep ON ep.season_id = s.id
    LEFT JOIN editions e ON e.id = ep.default_edition_id
"""


async def library_rows(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Every non-deleted title with its current slug and its default
    edition's technical detail, newest first (D-60) -- the Library
    screen's whole data source in one query.
    """
    return await conn.fetch(
        f"SELECT {_LIBRARY_ROW_COLUMNS} {_LIBRARY_ROW_FROM} "
        "WHERE t.status != 'deleted' ORDER BY t.created_at DESC"
    )


async def library_row(conn: asyncpg.Connection, title_id) -> asyncpg.Record | None:
    """One Library row by title id, same shape as library_rows -- the
    status-poll fragment's data source (D-49's "the page polls"
    mechanism).
    """
    return await conn.fetchrow(
        f"SELECT {_LIBRARY_ROW_COLUMNS} {_LIBRARY_ROW_FROM} WHERE t.id = $1",
        title_id,
    )


async def cancel_delete(conn: asyncpg.Connection, title_id) -> None:
    """Undo mark_for_delete -- D-62's "Undo" action. Back to active,
    delete_at cleared; GET /v/{slug} starts resolving again immediately.
    """
    await conn.execute(
        "UPDATE titles SET status = 'active', delete_at = NULL WHERE id = $1",
        title_id,
    )


async def delete_failed_title(conn: asyncpg.Connection, title_id) -> list[str]:
    """Hard-delete every row for a title that is 'failed' -- edition,
    episode, season, title, and its slug_history rows, in FK-dependency
    order. Refuses (raises ValueError) for any other status: nothing was
    ever published for a failed add, so the slug is freed immediately
    rather than tombstoned, but the same call against a live title would
    destroy history that other people's links depend on.

    **Returns the object keys it is about to orphan** -- the video key and
    the poster key, when each is non-null -- collected before the rows go.
    This return value is not decoration: it is the only remaining way to
    find those objects. A failed add can leave a fully uploaded object
    behind (the upload succeeded and a later step did not), and the row
    holding its key is exactly what this function destroys. Return them
    and the caller can delete them first; return nothing and the object is
    unreachable forever, which is precisely the orphan class this phase
    exists to close.
    """
    async with conn.transaction():
        title = await conn.fetchrow(
            "SELECT status FROM titles WHERE id = $1 FOR UPDATE", title_id
        )
        if title is None:
            raise LookupError(f"title {title_id!r} not found")
        if title["status"] != "failed":
            raise ValueError(
                f"refusing to hard-delete title {title_id!r}: status is "
                f"{title['status']!r}, not 'failed' -- only a failed add "
                "was never published and is safe to erase outright"
            )

        editions = await conn.fetch(
            """
            SELECT e.object_key AS object_key, e.poster_key AS poster_key
            FROM editions e
            JOIN episodes ep ON ep.id = e.episode_id
            JOIN seasons s ON s.id = ep.season_id
            WHERE s.title_id = $1
            """,
            title_id,
        )
        keys: list[str] = []
        for row in editions:
            if row["object_key"]:
                keys.append(row["object_key"])
            if row["poster_key"]:
                keys.append(row["poster_key"])

        # episodes.default_edition_id -> editions.id must be cleared before
        # editions can be dropped -- the same FK cycle 0001's module
        # docstring explains for CREATE TABLE, now unwound for DELETE.
        await conn.execute(
            """
            UPDATE episodes SET default_edition_id = NULL
            WHERE season_id IN (SELECT id FROM seasons WHERE title_id = $1)
            """,
            title_id,
        )
        await conn.execute(
            """
            DELETE FROM editions WHERE episode_id IN (
                SELECT ep.id FROM episodes ep
                JOIN seasons s ON s.id = ep.season_id
                WHERE s.title_id = $1
            )
            """,
            title_id,
        )
        await conn.execute(
            """
            DELETE FROM episodes WHERE season_id IN (
                SELECT id FROM seasons WHERE title_id = $1
            )
            """,
            title_id,
        )
        await conn.execute("DELETE FROM seasons WHERE title_id = $1", title_id)
        await conn.execute("DELETE FROM slug_history WHERE title_id = $1", title_id)
        await conn.execute("DELETE FROM titles WHERE id = $1", title_id)

    return keys


async def referenced_objects(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Every object_key and every non-null poster_key in the catalog, each
    row carrying `key`, `kind` ('video' or 'poster'), `title_name`, `slug`
    (the title's current slug) and `title_status`.

    Not a bare set of keys: the reconciliation report has to name a
    missing object by the /v/{slug} address it breaks, because that is the
    thing the operator recognises and the thing they will go and test. A
    set of keys can say a file is gone; it cannot say which link is dead.
    The set form an audit also wants is one comprehension away from this,
    so the richer query is strictly the cheaper choice.
    """
    return await conn.fetch(
        """
        SELECT e.object_key AS key, 'video' AS kind,
               t.name AS title_name, sh.slug AS slug, t.status AS title_status
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        LEFT JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
        UNION ALL
        SELECT e.poster_key AS key, 'poster' AS kind,
               t.name AS title_name, sh.slug AS slug, t.status AS title_status
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        LEFT JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
        WHERE e.poster_key IS NOT NULL
        """
    )
