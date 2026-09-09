"""Raw SQL over asyncpg for the catalog schema (titles/slug_history/seasons/
episodes/editions from migrations/versions/0001_initial_catalog.py).

Every function takes an already-open `asyncpg.Connection` (or a connection
acquired from a pool) as its first argument -- pool lifecycle is
`orchestrator.db.create_pool`'s job, not this module's.
"""
import json
import re

import asyncpg


async def resolve_slug(conn: asyncpg.Connection, slug: str) -> asyncpg.Record | None:
    """Resolve a slug to its title, its episode and its default edition,
    via slug_history.

    For a given slug text there may be more than one history row (D-10 reuse:
    a tombstoned title's row plus a new title's row for the same text) --
    the newest `created_at` wins (verdict B3). This one rule covers both
    rename (single row, still resolves after rename -- ADDR-05) and reuse
    (two rows, the newer one wins, the older stays as history) without a
    special case for either.

    **The episode-pointer branch.** When the winning history row carries a
    non-null `episode_id` (see migrations/versions/0006_series_and_editions
    .py for what writes it -- a slug that followed one standalone file into
    a series), the episode picked is that exact episode, not the title's
    lowest-numbered one. When it is null -- every row written before this
    phase, and every ordinary title slug since -- the original lowest-
    episode lateral runs unchanged, so this branch is a complete no-op for
    the whole catalog that predates it. Without it, the address handed out
    for a standalone episode would start playing episode one of whatever
    series its file later joined, silently, the moment this migration
    landed.

    **The "lowest episode" tiebreak reads `episode_number_history`, not
    the legacy `episodes.number` column** (plan 10 fix): every episode-
    creation path since migration 0006 (`attach_episode_to_title` and its
    plural/reparent twins) claims its number only in
    `episode_number_history`, leaving `episodes.number` at its schema
    default of 1 for every episode of every multi-episode title -- a tie
    Postgres breaks by whatever physical order it happens to scan rows in,
    not by the number an operator actually gave the episode. Falling back
    to the legacy column via `COALESCE` only when no current history row
    exists keeps this a no-op for the one caller that can still lack one
    (a hand-built test row bypassing `claim_episode_number` entirely).

    Returns the title's own status, the resolved episode's id and status,
    the episode's default edition id, and that edition's status --
    readiness now lives on the episode and the edition (D-157), so the
    title's status alone can no longer decide whether the address is gone,
    preparing, or ready; see orchestrator.resolve for the routes that read
    all four together.
    """
    return await conn.fetchrow(
        """
        SELECT
            t.id AS title_id,
            t.status AS status,
            ep.id AS episode_id,
            ep.status AS episode_status,
            ep.default_edition_id AS default_edition_id,
            de.status AS edition_status
        FROM slug_history sh
        JOIN titles t ON t.id = sh.title_id
        LEFT JOIN LATERAL (
            SELECT ep2.id, ep2.status, ep2.default_edition_id
            FROM episodes ep2
            WHERE ep2.id = COALESCE(
                sh.episode_id,
                (
                    SELECT e3.id
                    FROM episodes e3
                    JOIN seasons s3 ON s3.id = e3.season_id
                    LEFT JOIN episode_number_history enh3
                        ON enh3.episode_id = e3.id AND enh3.is_current
                    WHERE s3.title_id = t.id
                    ORDER BY s3.number, COALESCE(enh3.number, e3.number)
                    LIMIT 1
                )
            )
        ) ep ON true
        LEFT JOIN editions de ON de.id = ep.default_edition_id
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

    Also returns the episode's own status and the edition's own status
    (D-157): the refresh address re-signs whatever this edition currently
    is, and without these two a retry could sign a redirect to an object a
    per-episode delete has already removed, or to one a conversion has not
    finished writing yet -- see orchestrator.resolve.refresh_edition_route
    for where these three statuses get turned into an answer.
    """
    return await conn.fetchrow(
        """
        SELECT
            e.id AS edition_id, t.status AS status,
            ep.status AS episode_status, e.status AS edition_status
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
    stereo_layout: str | None = None,
    number: int = 1,
):
    """One transaction: title, season #1, episode #1, edition, and the
    slug's first history row -- then point episode.default_edition_id at
    the new edition (D-09).

    `number` (default 1, byte-for-byte the prior behaviour for every
    existing caller) claims this episode's number in the same transaction,
    through the identical claim_episode_number helper a renumber uses. This
    is the one function every creation path in the project goes through --
    the single-file add's new-title branch, the CLI's own `add`, and a
    folder screen's new-title move -- so a title created after this
    migration answers at its numbered address (GET /v/{slug}/{number})
    immediately, with no backfill and no operator action, the same promise
    the migration's own backfill makes for every title that predates it.

    `status` defaults to 'active' (the CLI's existing behaviour, unchanged).
    The web add flow (D-49) passes `status="preparing"` instead, so the row
    exists and the slug is reserved before any ffmpeg/upload work starts.
    `source_path` (D-48), `audio_stream_index` and `audio_channels` land
    straight on the edition row; all three are optional because the CLI
    path still has none of them. `source_path` and `audio_stream_index`
    also ride on the `remux` job row itself once one is enqueued
    (`enqueue_job`); `audio_channels` stays edition-side only, read back
    by `edition_for_remux` when the addition consumer claims that job.

    `stereo_layout` is stored here only for the as-is (remux) branch,
    which never gets a second chance to record it (D-139): a file that
    arrives already stereo-compatible takes this path and this is the one
    write that ever happens for it. A convert branch leaves this `None`
    here on purpose -- its own edition's layout is recorded once the
    conversion actually reports done, through
    `set_edition_conversion_detail`, so the two stay one write each rather
    than the as-is branch writing it twice.

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
                audio_stream_index, audio_channels, stereo_layout
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            edition_id,
            episode_id,
            object_key,
            source_path,
            audio_stream_index,
            audio_channels,
            stereo_layout,
        )
        await conn.execute(
            "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
            edition_id,
            episode_id,
        )
        await claim_episode_number(conn, title_id, episode_id, number)
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

    The interrupted-add reason the old startup sweep used to write here
    (D-118) has no caller left: that sweep is gone, and the durable job
    queue's own restart story needs no equivalent -- a restart just
    leaves the row's heartbeat to go stale, and the reaper's sweep
    requeues it instead. Its catalog key stays in both locale files
    (`orchestrator/locales/en.json`/`ru.json`) purely so a title that
    still carries it from before this change keeps rendering a real
    sentence instead of a raw key; nothing in this codebase writes it any
    more.
    """
    await conn.execute(
        "UPDATE titles SET status = 'failed', failure_reason = $2 WHERE id = $1",
        title_id,
        reason_key,
    )


async def requeue_title(conn: asyncpg.Connection, title_id) -> None:
    """Put a 'failed' title back to 'preparing' for Retry (D-130) --
    clears `failure_reason` and `started_at`, the same fields a fresh add
    starts with, so the row renders queued, indistinguishable from a
    first attempt. The caller enqueues the fresh job in the same
    transaction; this only ever flips the title's own two columns.
    """
    await conn.execute(
        "UPDATE titles SET status = 'preparing', failure_reason = NULL, "
        "started_at = NULL WHERE id = $1",
        title_id,
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
    fps: float | None = None,
    video_profile: str | None = None,
    video_level: int | None = None,
    pix_fmt: str | None = None,
    audio_codec: str | None = None,
    audio_sample_rate: int | None = None,
    audio_channel_layout: str | None = None,
) -> None:
    """Record the technical detail ffprobe already produced (D-60) -- the
    library row's size/duration/resolution/audio-track line, all of it
    known before a single byte is remuxed.

    The seven keyword-only parameters from `fps` on describe the STORED
    object, not the source file the caller probed -- the video stream is
    always copied so its four parameters carry over unchanged, but the
    audio does not, since the remux re-encodes it to one codec whenever
    the source is not already that codec. The caller does that
    reconciliation (see `mytube_media.compat.effective_audio_codec`)
    before calling this; a caller that passes the raw probe output here is
    passing the wrong thing. All seven default to `None` so a caller that
    predates this phase (or a row this phase's migration left null) keeps
    working unchanged.
    """
    await conn.execute(
        """
        UPDATE editions
        SET size_bytes = $2, duration_seconds = $3, width = $4, height = $5,
            audio_language = $6, audio_title = $7, audio_reencoded = $8,
            fps = $9, video_profile = $10, video_level = $11, pix_fmt = $12,
            audio_codec = $13, audio_sample_rate = $14, audio_channel_layout = $15
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
        fps,
        video_profile,
        video_level,
        pix_fmt,
        audio_codec,
        audio_sample_rate,
        audio_channel_layout,
    )


async def set_edition_poster(conn: asyncpg.Connection, edition_id, poster_key: str) -> None:
    """Point the edition at its extracted poster frame (D-72). Never called
    for a failed extraction -- the library row degrades to a placeholder
    when poster_key stays null, rather than a broken <img>.
    """
    await conn.execute(
        "UPDATE editions SET poster_key = $2 WHERE id = $1", edition_id, poster_key
    )


async def set_edition_subtitle_signs_only(
    conn: asyncpg.Connection, edition_id, signs_only: bool
) -> None:
    """Record D-186's signs-only/full attribute on the edition, resolved
    from the chosen subtitle's own folder tokens at add time -- the
    operator's choice is already known the moment the add route validates
    it, so this never waits for the conversion itself to finish. Called
    only when the route actually resolved one of the two words (`bool`,
    not `bool | None`): an unresolved folder leaves the column at its
    default `NULL` by simply never being called, since an unknown
    attribute and a known-false one are different facts (see the column's
    own migration comment).
    """
    await conn.execute(
        "UPDATE editions SET subtitle_burned_signs_only = $2 WHERE id = $1",
        edition_id,
        signs_only,
    )


async def editions_by_source_paths(
    conn: asyncpg.Connection, paths: list[str]
) -> list[asyncpg.Record]:
    """Which of these share-relative paths already have an edition -- the
    fact both the share listing's "already added" chip and the add form's
    warning banner need (D-48). Callers pass every path in the folder
    being rendered and get back only the matches: one query per rendered
    folder, never one query per row.

    Each row carries the edition's own id, its source_path, the title's
    name and status, and the title's *current* slug (via slug_history) --
    the listing links to /v/{slug}, not to the edition id -- except for
    `reparent_edition_to_episode` (plan 08), which needs the id itself to
    move the row.

    `edition_kind` (CONV-01/D-147) is the `kind` of the earliest `jobs` row
    ever created for this title -- `'remux'` for an as-is addition,
    `'transcode'` for a conversion, or `None` for a title this phase never
    queued a job for at all (every title the CLI creates directly, from
    before this phase existed).

    `episode_id` and `number` (D-176) are this exact edition's own episode
    and its current number, read the same way `episode_by_number`'s own
    lookup does -- what the add form's edition-offer banner needs to write
    `add.edition_offer.body`'s "episode {n} of {title}" sentence and to
    build the "Add as an edition" link that targets this episode.
    """
    if not paths:
        return []
    return await conn.fetch(
        """
        SELECT
            e.id AS edition_id,
            e.source_path AS source_path,
            t.id AS title_id,
            t.name AS title_name,
            t.status AS title_status,
            sh.slug AS slug,
            ep.id AS episode_id,
            enh.number AS number,
            (
                SELECT j.kind FROM jobs j
                WHERE j.title_id = t.id
                ORDER BY j.created_at
                LIMIT 1
            ) AS edition_kind
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
        LEFT JOIN episode_number_history enh
            ON enh.episode_id = ep.id AND enh.is_current
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
    e.audio_reencoded AS audio_reencoded, e.poster_key AS poster_key,
    e.source_path AS source_path,
    e.source_codec AS source_codec, e.subtitle_burned_language AS subtitle_burned_language,
    e.subtitle_burned_title AS subtitle_burned_title, e.stereo_layout AS stereo_layout,
    job.job_id AS job_id, job.job_kind AS job_kind, job.job_status AS job_status,
    job.job_db_stage AS job_db_stage, job.job_progress_pct AS job_progress_pct,
    job.job_eta_seconds AS job_eta_seconds, job.job_cancel_requested AS job_cancel_requested,
    job.job_queued_ahead AS job_queued_ahead, job.job_episode_number AS job_episode_number,
    agg.episode_count AS episode_count, agg.ready_count AS ready_count,
    agg.failed_count AS failed_count,
    wc.worker_name AS worker_name, wc.worker_last_seen_at AS worker_last_seen_at,
    wc.worker_status AS worker_status
"""

# One row per title, guaranteed by the join rather than by the caller
# happening to have written only one episode. Plain joins down to episodes
# multiply the title row once per episode: `library_rows` would render the
# same title twice and `library_row` (a fetchrow) would pick an arbitrary
# one of them. The schema has always allowed a second episode -- seasons
# and episodes are what those tables are for -- so the single-episode
# assumption belongs in the query, stated, rather than in nothing at all.
# Same LEFT JOIN LATERAL shape resolve_slug already uses, for the same
# reason and with the same ordering.
#
# `job` and `wc` (04-11): the live job for this title, if any, and the
# most recently seen converter, joined in here rather than read as a
# second query per row -- a library of thirty in-flight rows would
# otherwise cost thirty-one round trips on every four-second poll instead
# of one. `job_queued_ahead` is a correlated subquery scoped to the same
# kind (D-118: a playlist assembly must never appear ahead of a
# conversion in the position an operator is told), computed once per row
# rather than fetched with `queries.count_queued_ahead` per row for the
# same one-query reason. `wc` carries the single most recently seen
# converter globally -- there is only ever one operator's worker fleet,
# and every queued transcode row reads the same "is a converter even
# alive" fact, so one LATERAL with no correlation to `t` is cheaper than
# resolving it once per row and passing it down by hand.
#
# `agg` and `job.job_episode_number` (05-09/D-163): a series is legible
# while collapsed only if the title row itself carries how many episodes
# exist, how many are ready and how many failed, plus which episode the
# live job above belongs to -- one more LATERAL rather than a second
# round trip, the same reasoning `job`/`wc` already establish. Every
# non-deleted episode counts toward `episode_count` regardless of its own
# job state; `ready_count`/`failed_count` read each episode's own
# *default* edition, the same row `episodes_for_library` (below) exposes
# in full for the expanded list. A film (one episode) gets `episode_count
# = 1` here exactly like every other title -- `derive_series_ui`
# (routes_titles.py) is what decides a one-episode title renders
# unchanged (D-162), not this query.
_LIBRARY_ROW_FROM = """
    FROM titles t
    LEFT JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
    LEFT JOIN LATERAL (
        -- "lowest episode" tiebreak reads episode_number_history, not the
        -- legacy episodes.number column -- see resolve_slug's own
        -- docstring (plan 10 fix) for why the latter is always 1 for
        -- every episode of a multi-episode title.
        SELECT ep.default_edition_id
        FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        LEFT JOIN episode_number_history enh
            ON enh.episode_id = ep.id AND enh.is_current
        WHERE s.title_id = t.id
        ORDER BY s.number, COALESCE(enh.number, ep.number)
        LIMIT 1
    ) first_ep ON true
    LEFT JOIN editions e ON e.id = first_ep.default_edition_id
    LEFT JOIN LATERAL (
        SELECT j.id AS job_id, j.kind AS job_kind, j.status AS job_status,
               j.stage AS job_db_stage, j.progress_pct AS job_progress_pct,
               j.eta_seconds AS job_eta_seconds,
               j.cancel_requested AS job_cancel_requested,
               (
                   SELECT count(*) FROM jobs j2
                   WHERE j2.status = 'queued' AND j2.kind = j.kind
                     AND j2.created_at < j.created_at
               ) AS job_queued_ahead,
               (
                   SELECT enh.number FROM editions je
                   JOIN episode_number_history enh
                       ON enh.episode_id = je.episode_id AND enh.is_current
                   WHERE je.id = j.edition_id
               ) AS job_episode_number
        FROM jobs j
        WHERE j.title_id = t.id AND j.status IN ('queued', 'running')
        ORDER BY j.created_at DESC
        LIMIT 1
    ) job ON true
    LEFT JOIN LATERAL (
        SELECT
            count(*) AS episode_count,
            count(*) FILTER (WHERE de.status = 'active') AS ready_count,
            count(*) FILTER (WHERE de.status = 'failed') AS failed_count
        FROM episodes ep2
        JOIN seasons s2 ON s2.id = ep2.season_id
        LEFT JOIN editions de ON de.id = ep2.default_edition_id
        WHERE s2.title_id = t.id AND ep2.status != 'deleted'
    ) agg ON true
    LEFT JOIN LATERAL (
        SELECT worker_name AS worker_name, last_seen_at AS worker_last_seen_at,
               status AS worker_status
        FROM worker_checkins
        ORDER BY last_seen_at DESC
        LIMIT 1
    ) wc ON true
"""


async def library_rows(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Every non-deleted title with its current slug and its default
    edition's technical detail, newest first by `changed_at` (D-165) --
    the Library screen's whole data source in one query.

    Ordered by `titles.changed_at`, not `created_at`: a title rises to
    the top the moment an episode or an edition is added or turns ready
    (`touch_title_changed`, called by `mark_edition_active`/
    `mark_edition_failed`/`attach_episodes_to_title` and every other
    catalog write that changes what this row shows), so what the operator
    just worked on stays on top for as long as it keeps changing, exactly
    as a brand new title already did under the old creation-time
    ordering. Migration 0006's own backfill sets `changed_at = created_at`
    for every pre-existing title, so this ordering starts out identical
    to the one it replaces rather than reshuffling the library on deploy.
    """
    return await conn.fetch(
        f"SELECT {_LIBRARY_ROW_COLUMNS} {_LIBRARY_ROW_FROM} "
        "WHERE t.status != 'deleted' ORDER BY t.changed_at DESC"
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


async def episodes_for_library(
    conn: asyncpg.Connection, title_ids: list
) -> list[asyncpg.Record]:
    """Every episode of every title in `title_ids`, in number order, each
    carrying its own default edition's technical detail and its own live
    job -- the series row's expanded list (D-162/D-163), fetched in
    exactly one round trip regardless of how many titles or episodes are
    asked for.

    Callers pass every series title id the screen (or the one row it is
    polling) already has and get back one flat list, grouped by
    `title_id` in the caller's own code
    (`routes_titles._group_episodes_by_title`) rather than by a second
    query per title. This is the second and last query the Library screen
    makes per render, paired with `library_rows` -- and the second and
    last one the 4-second poll fragment makes per tick, paired with
    `library_row`: thirty in-flight episodes across a handful of
    converting seasons cost two queries on every tick either way, not one
    per episode and not one per title. Passing an empty list short-
    circuits before ever reaching the database, the same guard
    `editions_by_source_paths` already uses -- a screen with no series at
    all (every title a film) never makes this call's round trip at all.

    Each episode's own live job is looked up through *any* of its
    editions (`je.episode_id = ep.id`), not only its current default --
    an in-flight second edition of an already-ready episode still shows
    on that episode's own row rather than vanishing until it either wins
    or fails. `edition_count` is a plain per-episode count of its own
    editions, read for a later plan's own nested edition disclosure
    (D-164) -- unused by this plan's own row, carried now so that plan
    adds no second query either.
    """
    if not title_ids:
        return []
    return await conn.fetch(
        """
        SELECT
            s.title_id AS title_id,
            ep.id AS episode_id,
            enh.number AS number,
            ep.status AS episode_status,
            e.id AS edition_id,
            e.status AS edition_status,
            e.failure_reason AS edition_failure_reason,
            e.size_bytes AS size_bytes,
            e.duration_seconds AS duration_seconds,
            e.width AS width, e.height AS height,
            e.audio_language AS audio_language, e.audio_title AS audio_title,
            e.audio_reencoded AS audio_reencoded,
            e.source_codec AS source_codec,
            e.subtitle_burned_language AS subtitle_burned_language,
            e.subtitle_burned_title AS subtitle_burned_title,
            e.stereo_layout AS stereo_layout,
            (SELECT count(*) FROM editions e2 WHERE e2.episode_id = ep.id) AS edition_count,
            job.job_id AS job_id, job.job_kind AS job_kind, job.job_status AS job_status,
            job.job_db_stage AS job_db_stage, job.job_progress_pct AS job_progress_pct,
            job.job_eta_seconds AS job_eta_seconds,
            job.job_cancel_requested AS job_cancel_requested,
            job.job_queued_ahead AS job_queued_ahead,
            wc.worker_name AS worker_name, wc.worker_last_seen_at AS worker_last_seen_at,
            wc.worker_status AS worker_status
        FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        LEFT JOIN episode_number_history enh
            ON enh.episode_id = ep.id AND enh.is_current
        LEFT JOIN editions e ON e.id = ep.default_edition_id
        LEFT JOIN LATERAL (
            SELECT j.id AS job_id, j.kind AS job_kind, j.status AS job_status,
                   j.stage AS job_db_stage, j.progress_pct AS job_progress_pct,
                   j.eta_seconds AS job_eta_seconds,
                   j.cancel_requested AS job_cancel_requested,
                   (
                       SELECT count(*) FROM jobs j2
                       WHERE j2.status = 'queued' AND j2.kind = j.kind
                         AND j2.created_at < j.created_at
                   ) AS job_queued_ahead
            FROM jobs j
            JOIN editions je ON je.id = j.edition_id
            WHERE je.episode_id = ep.id AND j.status IN ('queued', 'running')
            ORDER BY j.created_at DESC
            LIMIT 1
        ) job ON true
        LEFT JOIN LATERAL (
            SELECT worker_name AS worker_name, last_seen_at AS worker_last_seen_at,
                   status AS worker_status
            FROM worker_checkins
            ORDER BY last_seen_at DESC
            LIMIT 1
        ) wc ON true
        WHERE s.title_id = ANY($1::uuid[]) AND ep.status != 'deleted'
        ORDER BY s.title_id, enh.number
        """,
        title_ids,
    )


async def cancel_delete(conn: asyncpg.Connection, title_id) -> None:
    """Undo mark_for_delete -- D-62's "Undo" action. Back to active,
    delete_at cleared; GET /v/{slug} starts resolving again immediately.
    """
    await conn.execute(
        "UPDATE titles SET status = 'active', delete_at = NULL WHERE id = $1",
        title_id,
    )


def _refuse_unless_failed(title) -> None:
    if title is None:
        raise LookupError("title not found")
    if title["status"] != "failed":
        raise ValueError(
            f"refusing to hard-delete title: status is {title['status']!r}, "
            "not 'failed' -- only a failed add was never published and is "
            "safe to erase outright"
        )


async def failed_title_object_keys(conn: asyncpg.Connection, title_id) -> list[str]:
    """The object keys (video, and poster when set) a 'failed' title's
    dismiss button is about to orphan -- read-only, deletes nothing.

    Split from `delete_failed_title` (below) precisely so the caller can
    delete these objects from storage *before* asking that function to
    destroy the rows that are the only record of the keys (T-02-08): a
    caller that read the keys, deleted the objects, and only then called
    `delete_failed_title` survives a crash between those two steps with
    the row still there to retry against; a caller that deleted the rows
    first and crashed before deleting the objects loses the keys forever.
    Refuses the same way `delete_failed_title` does, and for the same
    reason -- a caller must not learn a live title's object keys through
    the door meant for a title nothing ever published.
    """
    title = await conn.fetchrow("SELECT status FROM titles WHERE id = $1", title_id)
    _refuse_unless_failed(title)

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
    return keys


async def delete_failed_title(conn: asyncpg.Connection, title_id) -> None:
    """Hard-delete every row for a title that is 'failed' -- edition,
    episode, season, title, and its slug_history rows, in FK-dependency
    order. Refuses (raises ValueError) for any other status: nothing was
    ever published for a failed add, so the slug is freed immediately
    rather than tombstoned, but the same call against a live title would
    destroy history that other people's links depend on.

    **Call only after every key `failed_title_object_keys` returned for
    this same title has already been removed from storage** -- see that
    function's docstring for why the order is load-bearing, not
    stylistic. This function itself never touches storage (queries.py is
    not the module that talks to S3); it only ever removes catalog rows.
    """
    async with conn.transaction():
        title = await conn.fetchrow(
            "SELECT status FROM titles WHERE id = $1 FOR UPDATE", title_id
        )
        _refuse_unless_failed(title)

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


def _refuse_unless_never_active(title) -> None:
    """The cancellation hard-delete's own guard (T-04-32), mirroring
    `_refuse_unless_failed`'s shape with a wider condition: a title is
    'never active' when its status is 'preparing' or 'failed' -- the only
    two statuses reachable before a title's address has ever resolved.
    Nothing in this codebase ever moves a title backward out of 'active'
    into either of those (only forward, into 'pending_delete' or
    'deleted'), so the current status is a sound proxy for "was this
    address ever live" with no separate history column needed.

    A cancelled add is exactly this: its title was created 'preparing'
    and its job never finished, so it is refused by every other hard-
    delete guard in this module (`_refuse_unless_failed` demands
    'failed') and needs this one instead -- the same reasoning, a wider
    door.
    """
    if title is None:
        raise LookupError("title not found")
    if title["status"] not in ("preparing", "failed"):
        raise ValueError(
            f"refusing to hard-delete title: status is {title['status']!r}, "
            "not 'preparing' or 'failed' -- only a title whose address "
            "never went live may be erased outright"
        )


async def never_active_title_object_keys(conn: asyncpg.Connection, title_id) -> list[str]:
    """The object keys a cancelled title's hard delete is about to orphan
    -- read-only, deletes nothing. Same shape and same reason as
    `failed_title_object_keys` (T-02-08): read before anything is
    deleted, so a caller can remove the objects first and call
    `delete_never_active_title` only once they are gone.
    """
    title = await conn.fetchrow("SELECT status FROM titles WHERE id = $1", title_id)
    _refuse_unless_never_active(title)

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
    return keys


async def delete_never_active_title(conn: asyncpg.Connection, title_id) -> None:
    """Hard-delete every row for a title whose address never went live --
    a cancelled add (T-04-32). Identical shape to `delete_failed_title`
    (same FK-dependency order, same slug_history removal so the address
    is freed rather than tombstoned -- nothing ever played there, so
    there is nothing for a tombstone to keep honest), guarded by
    `_refuse_unless_never_active` instead of `_refuse_unless_failed`.

    **Call only after every key `never_active_title_object_keys` returned
    for this same title has already been removed from storage.**
    """
    async with conn.transaction():
        title = await conn.fetchrow(
            "SELECT status FROM titles WHERE id = $1 FOR UPDATE", title_id
        )
        _refuse_unless_never_active(title)

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


async def referenced_objects(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Every object_key and every non-null poster_key a *live* title still
    points at, plus every artifact a *live* playlist has built, each row
    carrying `key`, `kind` ('video', 'poster' or 'playlist'), `title_name`
    (a playlist's own name for the third kind), `slug` (the title's or
    playlist's current address) and `title_status`.

    Not a bare set of keys: the reconciliation report has to name a
    missing object by the /v/{slug} address it breaks, because that is the
    thing the operator recognises and the thing they will go and test. A
    set of keys can say a file is gone; it cannot say which link is dead.
    The set form an audit also wants is one comprehension away from this,
    so the richer query is strictly the cheaper choice.

    Titles already reaped are excluded, and the exclusion is load-bearing.
    A reaped title keeps its edition rows -- only `titles.status` flips --
    but the reaper removed the objects those rows name, on purpose, before
    flipping it. Counting them as referenced would make reconcile report
    every completed deletion as a broken address, permanently and
    cumulatively, until the one signal that means "a live address points
    at nothing" is noise the operator has learned to scroll past. A title
    still in `pending_delete` stays in the set: its objects are untouched
    while the deletion can still be undone, so a bucket missing them
    genuinely is a broken address.

    **A reaped episode is excluded the same way, one level down** (plan
    10). A per-episode delete tombstones the episode alone and leaves its
    title active -- `ep.status = 'deleted'` is the one new predicate on
    both edition branches below, added beside the title exclusion rather
    than as a second rule stated twice, closing the exact false positive
    the paragraph above describes for a whole title. An episode still in
    `pending_delete` stays in the set, for the identical reason a
    `pending_delete` title does: nothing has been removed from storage yet.
    The playlist branch names no episode at all and is untouched.

    The third branch is a playlist artifact -- `playlist_artifacts.
    object_key`, excluded only once its playlist is `'deleted'`, for the
    same reason as the two branches above. **Superseded artifacts are
    included on purpose**: they are still in the bucket while a viewer's
    signed address handed out before a rebuild can still reach them, and
    an audit calling one garbage would be telling the operator to delete
    something that is still playing. This lands here, one more UNION ALL
    branch rather than a second query, because the reconciliation
    function, the library header's counts and the audit report all read
    this one result -- and because it is where `playlist_artifacts` is
    created; nothing has produced an artifact yet on a fresh database, so
    the branch is inert until the first build.
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
        WHERE t.status != 'deleted' AND ep.status != 'deleted'
        UNION ALL
        SELECT e.poster_key AS key, 'poster' AS kind,
               t.name AS title_name, sh.slug AS slug, t.status AS title_status
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        LEFT JOIN slug_history sh ON sh.title_id = t.id AND sh.is_current
        WHERE e.poster_key IS NOT NULL AND t.status != 'deleted' AND ep.status != 'deleted'
        UNION ALL
        SELECT pa.object_key AS key, 'playlist' AS kind,
               p.name AS title_name, p.slug AS slug, p.status AS title_status
        FROM playlist_artifacts pa
        JOIN playlists p ON p.id = pa.playlist_id
        WHERE p.status != 'deleted'
        """
    )


# --- Episode/edition queries (0006_series_and_editions.py) ----------------


async def touch_title_changed(conn: asyncpg.Connection, title_id) -> None:
    """Bump a title's changed_at to now() -- the library's new ordering key
    (migrations/versions/0006_series_and_editions.py). Called by the
    edition-scoped readiness twins below, so a title rises to the top of
    the library the moment one of its episodes finishes or fails, exactly
    as an operator watching the screen would expect. A later plan's own
    catalog write calls this directly for the same reason.
    """
    await conn.execute("UPDATE titles SET changed_at = now() WHERE id = $1", title_id)


async def claim_episode_number(
    conn: asyncpg.Connection, title_id, episode_id, number: int
) -> None:
    """Give one episode a new current number, inside the caller's own
    transaction -- the number equivalent of rename_slug, and the one place
    both a fresh creation (create_title_with_edition) and an explicit
    renumber go through.

    Flips this episode's own current row (if it has one) to not-current,
    then inserts the new current row with a fresh clock_timestamp(). The
    insert is an upsert on the exact (title_id, number, episode_id) triple
    -- the identical ON CONFLICT ... DO UPDATE shape rename_slug uses for a
    rename back to a slug the same title held before, needed for the
    identical reason: a renumber back to a number this same episode held
    earlier hits a row that already exists with is_current=false, and a
    plain INSERT would raise instead of reclaiming it.

    Deliberately does not refuse a number a *different* episode currently
    holds. The resolve rule (resolve_episode_slug) always answers the
    newest row for a given (title, number), regardless of which episode is
    is_current for its own number -- so the older episode's now-superseded
    claim simply stops winning the moment this one is inserted; nothing
    about that older row needs to change for the new claim to take effect,
    the same way slug reuse needs no change to the tombstoned row it
    outranks.
    """
    await conn.execute(
        """
        UPDATE episode_number_history SET is_current = false
        WHERE title_id = $1 AND episode_id = $2 AND is_current
        """,
        title_id,
        episode_id,
    )
    await conn.execute(
        """
        INSERT INTO episode_number_history (title_id, number, episode_id, is_current, created_at)
        VALUES ($1, $2, $3, true, clock_timestamp())
        ON CONFLICT (title_id, number, episode_id)
        DO UPDATE SET is_current = true, created_at = clock_timestamp()
        """,
        title_id,
        number,
        episode_id,
    )


async def taken_episode_numbers(conn: asyncpg.Connection, title_id) -> set[int]:
    """Every number currently claimed by an episode of this title -- the
    number equivalent of taken_slugs, for a renumber form's own collision
    check.
    """
    rows = await conn.fetch(
        "SELECT number FROM episode_number_history WHERE title_id = $1 AND is_current",
        title_id,
    )
    return {row["number"] for row in rows}


async def episode_for_number(
    conn: asyncpg.Connection, title_id, number: int
) -> asyncpg.Record | None:
    """The episode a number currently resolves to for this title, and
    whether that claim is the episode's own current one.

    "In use right now" and "used to be this episode's number" are two
    different answers a renumber form has to tell apart before offering a
    number back to the operator -- this makes both available in one read,
    on the identical newest-wins ordering resolve_episode_slug itself uses,
    so the two functions can never disagree about which claim currently
    wins a given number.
    """
    return await conn.fetchrow(
        """
        SELECT episode_id, is_current
        FROM episode_number_history
        WHERE title_id = $1 AND number = $2
        ORDER BY created_at DESC
        LIMIT 1
        """,
        title_id,
        number,
    )


async def resolve_episode_slug(
    conn: asyncpg.Connection, slug: str, number: int, edition_slug: str | None = None
) -> asyncpg.Record | None:
    """Resolve the numbered address, GET /v/{slug}/{number}[/{edition_slug}],
    to a title, an episode, and an edition.

    Joins the slug's own newest-wins row (the same rule resolve_slug
    already applies) to the number's own newest-wins row within that same
    title (episode_number_history, the identical rule applied a second
    time to the number half of the address) -- an old slug and an old
    number both keep answering on one formula each, exactly as ADDR-05
    already promises for a slug alone. `number` arrives here as an
    already-parsed integer (see orchestrator.resolve): every leading-zero
    form of a number collapses onto the same integer before this statement
    ever runs, so there is only ever one number to look up and this query
    carries no special case for "01" versus "1".

    When `edition_slug` is given, the edition returned is the one edition
    of this episode whose own `slug` column matches it exactly -- not a
    fallback to the default when it fails to match, "an unknown edition
    slug resolves to nothing" the same way an unknown number does. When
    `edition_slug` is omitted, the episode's own default_edition_id is
    used. Returns the title's status, the episode's id and status, and the
    resolved edition's id, status and stereo layout (plan 10's check page
    needs the exact edition's own 3D hint, not the title's first episode's
    -- see routes_episodes.py), or None when the slug or the number does
    not resolve to anything at all.
    """
    return await conn.fetchrow(
        """
        WITH winning_slug AS (
            SELECT sh.title_id
            FROM slug_history sh
            WHERE sh.slug = $1
            ORDER BY sh.created_at DESC
            LIMIT 1
        )
        SELECT
            t.id AS title_id,
            t.status AS status,
            ep.id AS episode_id,
            ep.status AS episode_status,
            CASE WHEN $3::text IS NOT NULL THEN chosen.id ELSE ep.default_edition_id END
                AS edition_id,
            CASE WHEN $3::text IS NOT NULL THEN chosen.status ELSE def_edition.status END
                AS edition_status,
            CASE WHEN $3::text IS NOT NULL THEN chosen.stereo_layout ELSE def_edition.stereo_layout END
                AS stereo_layout
        FROM winning_slug ws
        JOIN titles t ON t.id = ws.title_id
        JOIN LATERAL (
            SELECT episode_id
            FROM episode_number_history
            WHERE title_id = ws.title_id AND number = $2
            ORDER BY created_at DESC
            LIMIT 1
        ) enh ON true
        JOIN episodes ep ON ep.id = enh.episode_id
        LEFT JOIN editions def_edition ON def_edition.id = ep.default_edition_id
        LEFT JOIN editions chosen ON chosen.episode_id = ep.id AND chosen.slug = $3
        """,
        slug,
        number,
        edition_slug,
    )


async def resolve_title_edition_slug(
    conn: asyncpg.Connection, slug: str, edition_slug: str
) -> asyncpg.Record | None:
    """Resolve a film's alternative-edition address, GET /v/{slug}/{segment}
    for a `segment` that is not made only of digits -- an edition of the
    title's own lowest episode, the one this whole resolver family exists
    for a single-episode title to have more than one address.

    Reuses the exact lowest-episode lateral resolve_slug's own null-pointer
    branch runs, then adds one more join: the edition must belong to that
    lowest episode and carry this exact edition slug. This is why an
    edition slug may never be made only of digits (see
    orchestrator.slugs.normalize_edition_slug) -- the caller decides which
    resolver to run purely from the segment's own shape, in Python, before
    either statement runs, and a slug that looked like a number would make
    that decision ambiguous.

    Returns the same column shape resolve_episode_slug does, plus that
    lowest episode's own current `number` (plan 10's check page heading
    names it even for a film, D-158), or None when the slug or the edition
    slug does not resolve.
    """
    return await conn.fetchrow(
        """
        SELECT
            t.id AS title_id,
            t.status AS status,
            lowest.id AS episode_id,
            lowest.status AS episode_status,
            lowest.number AS number,
            e.id AS edition_id,
            e.status AS edition_status,
            e.stereo_layout AS stereo_layout
        FROM slug_history sh
        JOIN titles t ON t.id = sh.title_id
        JOIN LATERAL (
            -- "lowest episode" tiebreak reads episode_number_history, not
            -- the legacy episodes.number column -- see resolve_slug's own
            -- docstring (plan 10 fix). Exposes its own current number
            -- directly, so the outer query needs no second join for it.
            SELECT ep.id, ep.status, enh0.number AS number
            FROM episodes ep
            JOIN seasons s ON s.id = ep.season_id
            LEFT JOIN episode_number_history enh0
                ON enh0.episode_id = ep.id AND enh0.is_current
            WHERE s.title_id = t.id
            ORDER BY s.number, COALESCE(enh0.number, ep.number)
            LIMIT 1
        ) lowest ON true
        JOIN editions e ON e.episode_id = lowest.id AND e.slug = $2
        WHERE sh.slug = $1
        ORDER BY sh.created_at DESC
        LIMIT 1
        """,
        slug,
        edition_slug,
    )


async def episode_neighbours(
    conn: asyncpg.Connection, title_id, number: int, edition_slug: str | None = None
) -> asyncpg.Record | None:
    """The previous and next episode of `title_id` in number order around
    `number` -- the check page's own navigation (D-158), one query rather
    than a full listing filtered in the route or two separate reads.

    Reads `episode_number_history`'s own *current* claims (`is_current`),
    the exact set `taken_episode_numbers` already reads -- a number a
    sibling used to hold but no longer does is not a neighbour to walk to.
    `previous_has_edition_slug`/`next_has_edition_slug` answer whether that
    neighbour carries an edition of its own matching `edition_slug` (`None`
    when the current page is not edition-specific, which makes both
    columns simply false) -- the caller's own fallback rule (D-158: keep
    the same edition slug when the neighbour has it, otherwise fall back
    to that neighbour's default) reads these two booleans rather than
    running a second query per direction.
    """
    return await conn.fetchrow(
        """
        WITH numbers AS (
            SELECT DISTINCT number FROM episode_number_history
            WHERE title_id = $1 AND is_current
        ),
        prev AS (SELECT MAX(number) AS number FROM numbers WHERE number < $2),
        next AS (SELECT MIN(number) AS number FROM numbers WHERE number > $2)
        SELECT
            prev.number AS previous_number,
            next.number AS next_number,
            EXISTS (
                SELECT 1 FROM editions e
                JOIN episodes ep ON ep.id = e.episode_id
                JOIN episode_number_history enh
                    ON enh.episode_id = ep.id AND enh.is_current
                WHERE enh.title_id = $1 AND enh.number = prev.number AND e.slug = $3
            ) AS previous_has_edition_slug,
            EXISTS (
                SELECT 1 FROM editions e
                JOIN episodes ep ON ep.id = e.episode_id
                JOIN episode_number_history enh
                    ON enh.episode_id = ep.id AND enh.is_current
                WHERE enh.title_id = $1 AND enh.number = next.number AND e.slug = $3
            ) AS next_has_edition_slug
        FROM prev, next
        """,
        title_id,
        number,
        edition_slug,
    )


async def mark_edition_active(conn: asyncpg.Connection, edition_id) -> None:
    """Flip a 'preparing' edition to 'active' -- the edition-scoped twin of
    mark_title_active, since readiness now lives on the edition rather than
    blanketing every address a title owns (D-157). Clears failure_reason
    too, for the same reason mark_title_active does: the column must never
    lie if this edition id is ever reused after an earlier failure. Also
    touches the owning title's changed_at, so the library rises to reflect
    the episode that just finished.
    """
    async with conn.transaction():
        title_id = await conn.fetchval(
            """
            SELECT t.id FROM editions e
            JOIN episodes ep ON ep.id = e.episode_id
            JOIN seasons s ON s.id = ep.season_id
            JOIN titles t ON t.id = s.title_id
            WHERE e.id = $1
            """,
            edition_id,
        )
        await conn.execute(
            "UPDATE editions SET status = 'active', failure_reason = NULL WHERE id = $1",
            edition_id,
        )
        if title_id is not None:
            await touch_title_changed(conn, title_id)


async def mark_edition_failed(conn: asyncpg.Connection, edition_id, reason_key: str) -> None:
    """Flip an edition to 'failed' with `reason_key`, a translation-catalog
    key -- the edition-scoped twin of mark_title_failed. Validated the same
    way the worker-facing job writers already validate a reason key
    (_validate_failure_reason_key): this column, like theirs, is reachable
    from a caller this module cannot vet at the call site. Also touches the
    owning title's changed_at, the same as mark_edition_active -- a failure
    is exactly as much "something happened to this title" as a success is.
    """
    _validate_failure_reason_key(reason_key)
    async with conn.transaction():
        title_id = await conn.fetchval(
            """
            SELECT t.id FROM editions e
            JOIN episodes ep ON ep.id = e.episode_id
            JOIN seasons s ON s.id = ep.season_id
            JOIN titles t ON t.id = s.title_id
            WHERE e.id = $1
            """,
            edition_id,
        )
        await conn.execute(
            "UPDATE editions SET status = 'failed', failure_reason = $2 WHERE id = $1",
            edition_id,
            reason_key,
        )
        if title_id is not None:
            await touch_title_changed(conn, title_id)


async def mark_edition_started(conn: asyncpg.Connection, edition_id) -> None:
    """Set started_at to clock_timestamp() the moment a worker actually
    dequeues this edition's job -- the edition-scoped twin of
    mark_title_started, called by the worker, never by the route. See that
    column's own comment in migrations/versions/0006_series_and_editions.py
    for why "queued" and "started" have to stay tellable apart at the
    edition level once readiness no longer blankets a whole title.
    """
    await conn.execute(
        "UPDATE editions SET started_at = clock_timestamp() WHERE id = $1", edition_id
    )


async def requeue_edition(conn: asyncpg.Connection, edition_id) -> None:
    """Put a 'failed' edition back to 'preparing' for a per-episode Retry
    (D-164) -- the edition-scoped twin of requeue_title: clears
    failure_reason and started_at, the same two fields requeue_title
    clears, so the episode row renders queued, indistinguishable from a
    first attempt.

    Also lifts the *title* itself out of 'failed' when it is currently
    there, the one title-level transition sync_title_status can never
    produce on its own (D-157: its own rule only ever moves a title to
    'active' or 'failed', never back to 'preparing'). sync_title_status
    only ever marks a title 'failed' once every one of its episodes has
    failed -- the retried episode's edition is a moment away from being
    the only thing keeping that title showing the full-failure row
    template, which hides the episode list this retry needs to be watched
    in. A title still 'active' from a sibling episode, or already
    'preparing', is left alone by the `AND status = 'failed'` guard below
    -- retry must never demote a working address back to 'preparing'.
    """
    async with conn.transaction():
        title_id = await conn.fetchval(
            """
            SELECT t.id FROM editions e
            JOIN episodes ep ON ep.id = e.episode_id
            JOIN seasons s ON s.id = ep.season_id
            JOIN titles t ON t.id = s.title_id
            WHERE e.id = $1
            """,
            edition_id,
        )
        await conn.execute(
            "UPDATE editions SET status = 'preparing', failure_reason = NULL, "
            "started_at = NULL WHERE id = $1",
            edition_id,
        )
        if title_id is not None:
            await conn.execute(
                "UPDATE titles SET status = 'preparing', failure_reason = NULL, "
                "started_at = NULL WHERE id = $1 AND status = 'failed'",
                title_id,
            )
            await touch_title_changed(conn, title_id)


async def episode_for_action(conn: asyncpg.Connection, episode_id) -> asyncpg.Record | None:
    """One episode's own status, its title id, and its default edition's
    status/failure reason -- what every per-episode action route
    (routes_episodes.py) reads fresh on every request, never trusted from
    the page that opened (T-05-43): delete/undo/purge read `episode_status`
    the same way the title-level routes read `title_status`; cancel/retry
    read the default edition's own status; dismiss refuses unless it is
    'failed'.
    """
    return await conn.fetchrow(
        """
        SELECT
            ep.id AS episode_id, ep.status AS episode_status,
            ep.default_edition_id AS default_edition_id,
            t.id AS title_id,
            e.status AS edition_status, e.failure_reason AS edition_failure_reason
        FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        LEFT JOIN editions e ON e.id = ep.default_edition_id
        WHERE ep.id = $1
        """,
        episode_id,
    )


async def mark_episode_for_delete(
    conn: asyncpg.Connection, episode_id, grace_seconds: int
) -> None:
    """Flip one episode to pending_delete with delete_at = now() +
    grace_seconds -- the episode-scoped twin of mark_for_delete. Every
    address this episode answers (its own numbered address, and any slug
    whose history row points straight at it) starts reading its status as
    gone immediately; the grace window only delays the reaper's physical
    delete_object, exactly as it does for a whole title.
    """
    await conn.execute(
        """
        UPDATE episodes
        SET status = 'pending_delete', delete_at = now() + make_interval(secs => $2)
        WHERE id = $1
        """,
        episode_id,
        grace_seconds,
    )


async def cancel_delete_episode(conn: asyncpg.Connection, episode_id) -> None:
    """Undo mark_episode_for_delete -- the episode-scoped twin of
    cancel_delete. Back to active, delete_at cleared; every address this
    episode answers starts resolving again immediately.
    """
    await conn.execute(
        "UPDATE episodes SET status = 'active', delete_at = NULL WHERE id = $1",
        episode_id,
    )


async def due_for_reaping_episodes(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Episodes whose delete_at has passed -- identifiers only (episode_id,
    title_id), not aggregated object keys.

    Deliberately not one row of aggregated keys per episode, unlike
    due_for_reaping's own array_agg for a whole title -- the same reason
    due_for_reaping_playlists gives for playlists: an episode can carry an
    arbitrary number of editions, and episode_object_keys below is the
    function that walks them for one episode at a time, at the point the
    reaper is actually about to delete its objects.
    """
    return await conn.fetch(
        """
        SELECT ep.id AS episode_id, s.title_id AS title_id
        FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        WHERE ep.status = 'pending_delete' AND ep.delete_at <= now()
        """
    )


async def episode_object_keys(conn: asyncpg.Connection, episode_id) -> list[str]:
    """Every object key (video, and poster when set) one episode's editions
    hold -- what the reaper must remove from storage before
    finish_delete_episode. Read-only, deletes nothing -- the same crash-
    safety shape failed_title_object_keys and never_active_title_object_keys
    already give: delete the objects first, then call finish_delete_episode
    to remove or tombstone the row that was the only record of the keys.
    """
    rows = await conn.fetch(
        "SELECT object_key, poster_key FROM editions WHERE episode_id = $1",
        episode_id,
    )
    keys: list[str] = []
    for row in rows:
        if row["object_key"]:
            keys.append(row["object_key"])
        if row["poster_key"]:
            keys.append(row["poster_key"])
    return keys


async def finish_delete_episode(conn: asyncpg.Connection, episode_id) -> None:
    """Finish one episode's reap, once the reaper has removed the objects
    episode_object_keys named for it.

    **Tombstone or hard delete, decided here.** An episode with any edition
    that is -- or, since nothing in this codebase ever moves an edition
    backward out of 'active', ever was -- 'active' is tombstoned: its own
    status flips to 'deleted' and every row survives untouched, the same
    way a reaped title's rows survive (referenced_objects's own docstring:
    "only titles.status flips"). This has to be a tombstone and not a hard
    delete, because a number-history row or a slug-history row may point at
    this exact episode forever, and a gone address must be able to say it
    is gone rather than answer as if it had never existed.

    An episode none of whose editions ever reached 'active' can never have
    been handed to a viewer at all -- there is no live address for a
    tombstone to keep honest -- so it is dropped outright instead, in the
    same foreign-key unwind order delete_never_active_title already
    establishes for a whole title: the episode's own default pointer
    cleared first, then its editions, then the episode row itself.
    """
    async with conn.transaction():
        ever_active = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM editions WHERE episode_id = $1 AND status = 'active'
            )
            """,
            episode_id,
        )
        if ever_active:
            await conn.execute(
                "UPDATE episodes SET status = 'deleted' WHERE id = $1", episode_id
            )
            return

        await conn.execute(
            "UPDATE episodes SET default_edition_id = NULL WHERE id = $1", episode_id
        )
        await conn.execute("DELETE FROM editions WHERE episode_id = $1", episode_id)
        await conn.execute("DELETE FROM episodes WHERE id = $1", episode_id)


# --- Growing an existing title, attaching an edition (0006, plan 05) -------


class EpisodeNumberTaken(Exception):
    """Raised by `attach_episode_to_title` when `number` is already held by
    a *current* claim of this title -- the caller turns this into its own
    refusal; nothing about the episode is written.
    """


async def title_by_current_slug(conn: asyncpg.Connection, slug: str) -> asyncpg.Record | None:
    """The title `slug` currently resolves to, restricted to a title an
    operator could actually add another episode to right now (D-159):
    'active' or 'preparing'. A title mid-delete or already gone is never
    offered as an attach target even though its slug_history row survives
    untouched (neither `mark_for_delete` nor `finish_delete` ever flips
    `is_current` -- that is what keeps a tombstoned address resolving at
    all).
    """
    return await conn.fetchrow(
        """
        SELECT t.id AS title_id, t.name AS title_name, t.status AS title_status
        FROM slug_history sh
        JOIN titles t ON t.id = sh.title_id
        WHERE sh.slug = $1 AND sh.is_current AND t.status IN ('active', 'preparing')
        """,
        slug,
    )


async def episode_by_number(
    conn: asyncpg.Connection, title_id, number: int
) -> asyncpg.Record | None:
    """The episode that currently holds `number` within `title_id`, or
    `None` when no episode currently claims it -- the add form's own
    read for D-159's "a different file of an episode that already exists"
    case, scoped to the *current* claim only (unlike `episode_for_number`,
    which also answers the renumber form's "used to be this episode's
    number" question and stops at the history table). Joined through to
    the episode's own row rather than stopping at
    `episode_number_history`, since the caller needs to know the episode
    exists and what it is, not merely that some claim of this number
    exists somewhere in history.
    """
    return await conn.fetchrow(
        """
        SELECT ep.id AS episode_id, ep.status AS episode_status,
               ep.default_edition_id AS default_edition_id
        FROM episode_number_history enh
        JOIN episodes ep ON ep.id = enh.episode_id
        WHERE enh.title_id = $1 AND enh.number = $2 AND enh.is_current
        """,
        title_id,
        number,
    )


async def episode_with_title(conn: asyncpg.Connection, episode_id) -> asyncpg.Record | None:
    """One episode's own title id/name and current number, in one row --
    what the add form's "opened from the library's Add edition action"
    entry point needs (D-176) to write `add.editing_edition_of`'s
    sentence, and what the add route (Task 2) uses to confirm a submitted
    `target_episode_id` actually names a real episode before it ever
    reaches `add_edition_to_episode`.
    """
    return await conn.fetchrow(
        """
        SELECT
            ep.id AS episode_id, ep.status AS episode_status,
            t.id AS title_id, t.name AS title_name,
            enh.number AS number
        FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        LEFT JOIN episode_number_history enh
            ON enh.episode_id = ep.id AND enh.is_current
        WHERE ep.id = $1
        """,
        episode_id,
    )


async def default_edition_technical(
    conn: asyncpg.Connection, episode_id
) -> asyncpg.Record | None:
    """The technical facts of an episode's current default edition that
    `orchestrator.media.add.suggest_edition_slug` (D-177) compares a new
    edition's own choices against: its audio language, its resolution,
    its burned-subtitle language and signs-only attribute, and whether it
    is itself a conversion rather than a remux -- read through the same
    "earliest job of this edition" lookup `editions_by_source_paths`
    already keys a title's own `edition_kind` off of, scoped here to one
    known edition instead of a source path.
    """
    return await conn.fetchrow(
        """
        SELECT
            e.audio_language AS audio_language,
            e.width AS width, e.height AS height,
            e.subtitle_burned_language AS subtitle_language,
            e.subtitle_burned_signs_only AS subtitle_signs_only,
            (
                SELECT j.kind FROM jobs j
                WHERE j.edition_id = e.id
                ORDER BY j.created_at
                LIMIT 1
            ) = 'transcode' AS is_transcode
        FROM episodes ep
        JOIN editions e ON e.id = ep.default_edition_id
        WHERE ep.id = $1
        """,
        episode_id,
    )


async def audio_languages_on_episode(conn: asyncpg.Connection, episode_id) -> set[str]:
    """Every non-null audio language already used by an edition of this
    episode -- `suggest_edition_slug`'s (D-177) own tie-break for a second
    dub of the same language, which needs the studio folder appended so
    two studios do not collide.
    """
    rows = await conn.fetch(
        "SELECT DISTINCT audio_language FROM editions "
        "WHERE episode_id = $1 AND audio_language IS NOT NULL",
        episode_id,
    )
    return {row["audio_language"] for row in rows}


async def edition_slug_taken_on_episode(
    conn: asyncpg.Connection, episode_id, slug: str
) -> bool:
    """Whether `slug` already names an edition of this episode -- D-177's
    own uniqueness scope (per episode, not global), read inside the
    caller's own locked transaction exactly as a title slug's uniqueness
    already is.
    """
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM editions WHERE episode_id = $1 AND slug = $2)",
        episode_id,
        slug,
    )


async def attach_episode_to_title(
    conn: asyncpg.Connection,
    title_id,
    number: int,
    edition_id,
    object_key: str,
    *,
    status: str = "active",
    source_path: str | None = None,
    audio_stream_index: int | None = None,
    audio_channels: int | None = None,
    stereo_layout: str | None = None,
) -> "uuid.UUID":
    """Create a new episode of an existing title's first season, at
    `number`, plus its edition -- the attach-to-an-existing-title twin of
    `create_title_with_edition` (D-159), sharing the exact claim-lock
    discipline that function's own docstring documents at length: the
    first statement inside this transaction takes `SLUG_CLAIM_LOCK_KEY`,
    and `taken_episode_numbers` is read only after that lock is held,
    closing the identical race one level down -- for a number instead of
    a slug. A caller must follow the same rule
    `create_title_with_edition` states: open the transaction and take
    this lock before reading anything this function's own refusal below
    depends on, or nest this call inside a caller that already did (the
    lock is session-reentrant, so nesting is harmless).

    Raises `LookupError` when `title_id` names no title with a season (a
    submission naming a stale or crafted id gets exactly this, never a
    created row). Raises `EpisodeNumberTaken` when `number` is already
    held by a *different* episode's current claim -- refused rather than
    silently taking over the address, since claiming it here would need
    the same care `claim_episode_number`'s own docstring gives a
    deliberate renumber, and a fresh-episode creation is not that
    operation.

    `status` sets the *edition's* own status (unlike
    `create_title_with_edition`'s identically-named parameter, which sets
    the *title's*, since the title here already exists): 'preparing' when
    the caller's work has not run yet (the web add form's own D-49
    shape), 'active' by the schema's own default otherwise (the CLI's
    shape). Every other keyword mirrors `create_title_with_edition`'s own
    for the identical reason.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SLUG_CLAIM_LOCK_KEY)

        season_id = await conn.fetchval(
            "SELECT id FROM seasons WHERE title_id = $1 ORDER BY number LIMIT 1",
            title_id,
        )
        if season_id is None:
            raise LookupError(f"no title {title_id!r} to attach an episode to")

        taken = await taken_episode_numbers(conn, title_id)
        if number in taken:
            raise EpisodeNumberTaken(number)

        episode_id = await conn.fetchval(
            "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
        )
        await conn.execute(
            """
            INSERT INTO editions (
                id, episode_id, object_key, source_path,
                audio_stream_index, audio_channels, stereo_layout, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            edition_id,
            episode_id,
            object_key,
            source_path,
            audio_stream_index,
            audio_channels,
            stereo_layout,
            status,
        )
        await conn.execute(
            "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
            edition_id,
            episode_id,
        )
        await claim_episode_number(conn, title_id, episode_id, number)
        await touch_title_changed(conn, title_id)
    return episode_id


async def add_edition_to_episode(
    conn: asyncpg.Connection,
    episode_id,
    slug: str,
    edition_id,
    object_key: str,
    *,
    status: str = "active",
    source_path: str | None = None,
    audio_stream_index: int | None = None,
    audio_channels: int | None = None,
    stereo_layout: str | None = None,
) -> None:
    """Create a new edition of an existing episode, under a caller-
    validated, already-unique `slug` -- add-an-edition twin of
    `attach_episode_to_title`, same lock, same race (two operators adding
    "the Russian dub" at once). See that function's own docstring, and
    `create_title_with_edition`'s, for the lock's full protection and what
    a caller must do for it to hold.

    Never touches `episodes.default_edition_id` (D-178) -- only
    `set_default_edition` does that, separately. Raises `LookupError` for
    a missing episode. The caller must have already validated/claimed
    `slug` via `orchestrator.media.add.plan_edition_slug` inside this same
    locked transaction; no rename path, ever (D-179).
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SLUG_CLAIM_LOCK_KEY)

        title_id = await conn.fetchval(
            """
            SELECT t.id FROM episodes ep
            JOIN seasons s ON s.id = ep.season_id
            JOIN titles t ON t.id = s.title_id
            WHERE ep.id = $1
            """,
            episode_id,
        )
        if title_id is None:
            raise LookupError(f"no episode {episode_id!r} to add an edition to")

        await conn.execute(
            """
            INSERT INTO editions (
                id, episode_id, slug, object_key, source_path,
                audio_stream_index, audio_channels, stereo_layout, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            edition_id,
            episode_id,
            slug,
            object_key,
            source_path,
            audio_stream_index,
            audio_channels,
            stereo_layout,
            status,
        )
        await touch_title_changed(conn, title_id)


async def set_default_edition(conn: asyncpg.Connection, edition_id) -> None:
    """Move an episode's default-edition pointer at `edition_id` (D-178) --
    called from exactly one place in this codebase outside an operator's
    own explicit "Make default" click: a job's terminal write, when
    `jobs.make_default_when_ready` was true and the edition it produced
    just became ready. Takes only `edition_id`, not the episode id
    alongside it -- the episode is looked up from the edition itself, so
    a caller that already has the edition on hand (every real caller)
    never needs a second read first.

    A no-op, not an error, when `edition_id` names no edition (defensive:
    nothing in this codebase calls this for an edition it has not just
    confirmed exists). Also touches the owning title's `changed_at`, the
    same as every other write that changes what an episode's own address
    plays.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT ep.id AS episode_id, t.id AS title_id
            FROM editions e
            JOIN episodes ep ON ep.id = e.episode_id
            JOIN seasons s ON s.id = ep.season_id
            JOIN titles t ON t.id = s.title_id
            WHERE e.id = $1
            """,
            edition_id,
        )
        if row is None:
            return
        await conn.execute(
            "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
            edition_id,
            row["episode_id"],
        )
        await touch_title_changed(conn, row["title_id"])


async def sync_title_status(
    conn: asyncpg.Connection, title_id, *, reason_key: str | None = None
) -> None:
    """The one place the title-level aggregate rule lives (D-157): called
    by every terminal job write (queue.py's in-process remuxer, and
    routes_worker.py's done/failed routes) after it has already flipped
    the *edition* the job produced. A title with any episode whose own
    default edition is 'active' is itself 'active' -- ready from its
    first ready episode, exactly as a one-episode title always has been,
    since for one episode the two facts are the same by construction and
    this rule reduces to `mark_title_active` unconditionally, byte-
    identical to before this function existed. A title none of whose
    episodes has ever had a ready default edition, and whose every
    episode's default edition has now failed, is 'failed'. Anything in
    between -- one episode ready or failed, another still preparing --
    leaves the title's own status untouched: it is not yet failed
    (something might still finish) and it is not newly ready (nothing
    new became ready this time).

    `reason_key` is the catalog key this call site's own job just failed
    with -- only read when every episode really has failed, since that is
    the only branch that can write `titles.failure_reason`.
    """
    async with conn.transaction():
        agg = await conn.fetchrow(
            """
            SELECT
                bool_or(de.status = 'active') AS any_ready,
                bool_and(de.status = 'failed') AS all_failed
            FROM episodes ep
            JOIN seasons s ON s.id = ep.season_id
            LEFT JOIN editions de ON de.id = ep.default_edition_id
            WHERE s.title_id = $1
            """,
            title_id,
        )
        if agg is None:
            return
        if agg["any_ready"]:
            await mark_title_active(conn, title_id)
        elif agg["all_failed"]:
            await mark_title_failed(conn, title_id, reason_key or "failed.reason.unknown")


async def lowest_episode_of_title(conn: asyncpg.Connection, title_id) -> asyncpg.Record | None:
    """The title's own lowest episode -- the exact lateral
    `resolve_title_edition_slug` already runs, exposed as its own reader
    for a caller that needs to act on that episode rather than resolve
    through it. A film-shaped title (one episode, addressed as
    GET /v/{slug}/{edition_slug} with no number segment, D-156) has only
    this one episode; a series has one per number, and this is the one a
    two-segment edition address always means.
    """
    return await conn.fetchrow(
        """
        -- "lowest episode" tiebreak reads episode_number_history, not the
        -- legacy episodes.number column -- see resolve_slug's own
        -- docstring (plan 10 fix).
        SELECT ep.id AS episode_id, ep.status AS episode_status,
               ep.default_edition_id AS default_edition_id
        FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        LEFT JOIN episode_number_history enh
            ON enh.episode_id = ep.id AND enh.is_current
        WHERE s.title_id = $1
        ORDER BY s.number, COALESCE(enh.number, ep.number)
        LIMIT 1
        """,
        title_id,
    )


async def edition_by_slug(
    conn: asyncpg.Connection, episode_id, slug: str | None
) -> asyncpg.Record | None:
    """The one edition of `episode_id` whose slug is exactly `slug` (`None`
    matches the episode's own slug-less first edition, D-177) -- its own
    id, object/poster keys, whether it is the episode's current default,
    and how many editions the episode holds in total. A caller removing an
    edition (D-178) reads all three facts in one round trip to decide
    whether to refuse, remove just this edition, or remove the whole
    episode along with its only one.
    """
    return await conn.fetchrow(
        """
        SELECT
            e.id AS edition_id,
            e.object_key AS object_key,
            e.poster_key AS poster_key,
            (e.id = ep.default_edition_id) AS is_default,
            (SELECT COUNT(*) FROM editions WHERE episode_id = ep.id) AS edition_count
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        WHERE e.episode_id = $1 AND e.slug IS NOT DISTINCT FROM $2
        """,
        episode_id,
        slug,
    )


async def delete_edition(conn: asyncpg.Connection, edition_id) -> None:
    """Hard-delete one edition's row immediately -- the only edition-
    removal shape with no grace window (a non-default edition removed
    while a sibling edition still exists, D-178): the schema carries no
    per-edition `delete_at` the way a title or an episode does. The caller
    must have already removed the objects `edition_by_slug` named for this
    id from storage, and must never call this for an episode's only or its
    default edition -- both of those go through `mark_episode_for_delete`
    instead, at the episode level, on the same countdown a whole episode's
    removal already uses.
    """
    await conn.execute("DELETE FROM editions WHERE id = $1", edition_id)


# --- The folder confirmation screen's save (0006, plan 08) -----------------


async def create_title_with_episodes(
    conn: asyncpg.Connection,
    name: str,
    slug: str,
    episodes: list[dict],
    *,
    season_number: int = 1,
    edition_status: str = "preparing",
) -> tuple["uuid.UUID", list["uuid.UUID"]]:
    """The folder-wide twin of `create_title_with_edition`: one title, one
    season carrying `season_number` (D-149/D-150 -- a season is a title,
    so the number this row carries is the one the address suffix and the
    display name both derive from), and one episode-plus-edition per entry
    in `episodes`, each claiming its own number in the same transaction
    `claim_episode_number` already backs every other creation path with.

    Each `episodes` entry is a plain dict carrying `number` (required) and
    whatever `edition_id`/`object_key`/`source_path`/`audio_stream_index`/
    `audio_channels`/`stereo_layout` the caller already resolved for that
    row (all but the first two are optional, `None` when absent, mirroring
    `create_title_with_edition`'s own keyword shape one row at a time).
    Unlike `create_title_with_edition` (whose edition is left at the
    schema's own default status for every existing caller, CLI included),
    every edition created here is written `edition_status` (default
    'preparing') explicitly: the whole point of this function is the web
    add flow's D-49 shape, where the row must exist and be reserved before
    any ffmpeg/upload work starts.

    **Concurrent slug and number claims.** The first statement inside this
    transaction takes `SLUG_CLAIM_LOCK_KEY`, exactly as
    `create_title_with_edition`'s own docstring documents at length -- read
    that docstring for the race this closes and what a caller must do
    (open the transaction and take the lock before this call, or accept
    this call's own lock as the whole transaction) for the protection to
    actually hold. Numbers within `episodes` are claimed one at a time
    through `claim_episode_number`, under the same lock, so two folder
    saves racing for the same title name can never produce two seasons
    both claiming episode 1.

    Returns `(title_id, [episode_id, ...])`, the second list in the same
    order as `episodes`, for the caller to zip against the jobs it still
    has to enqueue -- this function enqueues nothing, mirroring every
    other creation path in this module.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SLUG_CLAIM_LOCK_KEY)
        title_id = await conn.fetchval(
            "INSERT INTO titles (name, status) VALUES ($1, $2) RETURNING id",
            name,
            "preparing",
        )
        season_id = await conn.fetchval(
            "INSERT INTO seasons (title_id, number) VALUES ($1, $2) RETURNING id",
            title_id,
            season_number,
        )
        episode_ids: list["uuid.UUID"] = []
        for spec in episodes:
            episode_id = await conn.fetchval(
                "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
            )
            await conn.execute(
                """
                INSERT INTO editions (
                    id, episode_id, object_key, source_path,
                    audio_stream_index, audio_channels, stereo_layout, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                spec["edition_id"],
                episode_id,
                spec["object_key"],
                spec.get("source_path"),
                spec.get("audio_stream_index"),
                spec.get("audio_channels"),
                spec.get("stereo_layout"),
                edition_status,
            )
            await conn.execute(
                "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
                spec["edition_id"],
                episode_id,
            )
            await claim_episode_number(conn, title_id, episode_id, spec["number"])
            episode_ids.append(episode_id)
        await reuse_slug(conn, slug, title_id)
    return title_id, episode_ids


async def attach_episodes_to_title(
    conn: asyncpg.Connection,
    title_id,
    episodes: list[dict],
    *,
    edition_status: str = "preparing",
) -> list["uuid.UUID"]:
    """The many-episode twin of `attach_episode_to_title`, for a folder
    save adding several episodes to one existing title in a single
    transaction -- the identical claim-lock discipline that function's own
    docstring documents, taken once here rather than once per episode.

    Raises `LookupError` when `title_id` names no title with a season.
    Raises `EpisodeNumberTaken` the moment any entry in `episodes` names a
    number already held by a *different* episode's current claim --
    checked against the numbers taken before this call started, plus every
    number this same call has already claimed for an earlier entry in the
    same list, so two rows within one submission can never silently
    overwrite each other's number either. `episodes` and `edition_status`
    take the same shape `create_title_with_episodes` documents.

    Touches `touch_title_changed` once at the end, not once per episode --
    the library only needs to know the title changed, not how many times.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SLUG_CLAIM_LOCK_KEY)

        season_id = await conn.fetchval(
            "SELECT id FROM seasons WHERE title_id = $1 ORDER BY number LIMIT 1",
            title_id,
        )
        if season_id is None:
            raise LookupError(f"no title {title_id!r} to attach episodes to")

        taken = await taken_episode_numbers(conn, title_id)
        episode_ids: list["uuid.UUID"] = []
        for spec in episodes:
            number = spec["number"]
            if number in taken:
                raise EpisodeNumberTaken(number)
            taken.add(number)

            episode_id = await conn.fetchval(
                "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
            )
            await conn.execute(
                """
                INSERT INTO editions (
                    id, episode_id, object_key, source_path,
                    audio_stream_index, audio_channels, stereo_layout, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                spec["edition_id"],
                episode_id,
                spec["object_key"],
                spec.get("source_path"),
                spec.get("audio_stream_index"),
                spec.get("audio_channels"),
                spec.get("stereo_layout"),
                edition_status,
            )
            await conn.execute(
                "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
                spec["edition_id"],
                episode_id,
            )
            await claim_episode_number(conn, title_id, episode_id, number)
            episode_ids.append(episode_id)

        await touch_title_changed(conn, title_id)
    return episode_ids


async def reparent_edition_to_episode(
    conn: asyncpg.Connection,
    *,
    edition_id,
    old_title_id,
    old_episode_id,
    old_slug: str,
    target_title_id,
    number: int,
) -> "uuid.UUID":
    """A file already added as its own standalone title joins a series
    without its bytes being touched (D-160/D-161, T-05-37): a new episode
    under `target_title_id` at `number`, the existing `edition_id` moved
    onto it, and `old_slug` written a fresh, permanent alias so the
    address handed out for the standalone file keeps answering with this
    exact episode forever, never the series' own lowest one.

    **The object key is never rewritten, re-read or re-uploaded.** This
    function issues no S3 call and touches no `object_key` column; the
    edition row that already names the object is the one row that moves.

    Same claim-lock discipline as `create_title_with_episodes`/
    `attach_episodes_to_title`: the first statement takes
    `SLUG_CLAIM_LOCK_KEY`. Raises `LookupError` when `target_title_id`
    names no title with a season, `EpisodeNumberTaken` when `number` is
    already held by a different episode of that title.

    **Write order, and why it is fixed.** The old episode's own
    `default_edition_id` is cleared *before* the edition's own `episode_id`
    is repointed -- the pointer this column names is about to belong to a
    different episode, and clearing it first means no row anywhere ever
    names an edition that has already left. Only once the edition has
    moved is the old episode (now genuinely empty) safe to drop, in the
    same foreign-key unwind order `delete_failed_title` already establishes
    (`editions_episode_id_fkey` carries no `ON DELETE` clause, so a season/
    episode row cannot be dropped while an edition still names it).

    **The new `slug_history` row is deliberately not `is_current`.** The
    partial unique index `slug_history_one_current_per_title` allows at
    most one current row per title, and `target_title_id` already has its
    own -- this row is not the series' own address anyway, it is a
    permanent alias for one episode, and `resolve_slug`'s newest-`created_
    at`-wins rule (with no `is_current` filter) is what makes an alias with
    `is_current = false` outrank the standalone title's own now-superseded
    current row without touching it.

    **The old title is tombstoned, not hard-deleted.** Its own
    `slug_history` rows (including the current one this alias outranks)
    survive untouched -- the old address depending on `slug_history`
    surviving is the entire mechanism above -- and `finish_delete_episode`'s
    own docstring is the reason an address that was ever live is
    tombstoned rather than erased: `titles.status` flips to 'deleted', the
    row itself stays. Its now-empty season/episode/edition rows are
    unwound first so nothing orphaned is left pointing at a tombstoned
    title.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", SLUG_CLAIM_LOCK_KEY)

        season_id = await conn.fetchval(
            "SELECT id FROM seasons WHERE title_id = $1 ORDER BY number LIMIT 1",
            target_title_id,
        )
        if season_id is None:
            raise LookupError(f"no title {target_title_id!r} to move an episode into")

        taken = await taken_episode_numbers(conn, target_title_id)
        if number in taken:
            raise EpisodeNumberTaken(number)

        new_episode_id = await conn.fetchval(
            "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
        )
        await claim_episode_number(conn, target_title_id, new_episode_id, number)

        # Clear the old episode's default pointer before the edition's own
        # reference moves -- see the docstring above for why this order is
        # fixed rather than incidental.
        await conn.execute(
            "UPDATE episodes SET default_edition_id = NULL WHERE id = $1", old_episode_id
        )
        await conn.execute(
            "UPDATE editions SET episode_id = $1 WHERE id = $2", new_episode_id, edition_id
        )
        await conn.execute(
            "UPDATE episodes SET default_edition_id = $1 WHERE id = $2",
            edition_id,
            new_episode_id,
        )

        # The permanent alias -- see the docstring above for why
        # is_current is false here.
        await conn.execute(
            """
            INSERT INTO slug_history (slug, title_id, episode_id, is_current, created_at)
            VALUES ($1, $2, $3, false, clock_timestamp())
            """,
            old_slug,
            target_title_id,
            new_episode_id,
        )

        # Unwind the old title's now-empty rows, the same FK order
        # delete_failed_title already establishes -- the title row and its
        # slug_history survive, tombstoned rather than erased.
        await conn.execute("DELETE FROM editions WHERE episode_id = $1", old_episode_id)
        await conn.execute("DELETE FROM episodes WHERE id = $1", old_episode_id)
        await conn.execute("DELETE FROM seasons WHERE title_id = $1", old_title_id)
        await conn.execute("UPDATE titles SET status = 'deleted' WHERE id = $1", old_title_id)

        await touch_title_changed(conn, target_title_id)
    return new_episode_id


# --- Playlist queries (0004_playlists.py) ---------------------------------


async def create_playlist(conn: asyncpg.Connection, name: str, slug: str):
    """Insert a new playlist row, `mode='pending'`, no artifact yet --
    returns its id.
    """
    return await conn.fetchval(
        "INSERT INTO playlists (name, slug) VALUES ($1, $2) RETURNING id",
        name,
        slug,
    )


async def resolve_playlist_slug(conn: asyncpg.Connection, slug: str) -> asyncpg.Record | None:
    """Resolve a playlist slug to the row the public `/p/{slug}` address
    reads -- a direct lookup on `playlists.slug`, not a newest-wins join
    over a history table, because playlists keep no history (D-103).

    `built_object_key` is a **joined alias**, not a column: a left join
    against `playlist_artifacts` restricted to `superseded_at IS NULL AND
    position IS NULL`, which is at most one row -- the current single
    assembled file, or nothing at all under the mechanism that produces
    segments. There is exactly one place that knows which artifact is
    current, and this is it. The value carries its extension (mirroring
    `editions.object_key`'s own stored shape) and is passed to the signer
    as-is -- see `orchestrator.signing`'s module docstring for what
    appending a second one would do.
    """
    return await conn.fetchrow(
        """
        SELECT
            p.id AS id,
            p.status AS status,
            p.mode AS mode,
            p.built_at AS built_at,
            p.name AS name,
            p.slug AS slug,
            pa.object_key AS built_object_key
        FROM playlists p
        LEFT JOIN playlist_artifacts pa
            ON pa.playlist_id = p.id
           AND pa.superseded_at IS NULL
           AND pa.position IS NULL
        WHERE p.slug = $1
        """,
        slug,
    )


async def taken_playlist_slugs(conn: asyncpg.Connection) -> set[str]:
    """Every playlist slug already in use -- `/p/` and `/v/` are separate
    path prefixes and separate namespaces, so this is read from
    `playlists.slug` alone, never `slug_history`.
    """
    rows = await conn.fetch("SELECT slug FROM playlists")
    return {row["slug"] for row in rows}


def _playlist_row_from_record(row: asyncpg.Record) -> dict:
    """Turn one raw `playlists`-joined record into the dict shape the
    Library screen and the public address both read -- parses
    `build_failure_params` (stored as jsonb, returned by asyncpg as raw
    JSON text since this pool registers no jsonb codec) into a plain dict,
    and derives `refused` from whether a failure reason was ever recorded.
    """
    data = dict(row)
    params_raw = data.pop("build_failure_params_raw")
    reason_key = data["refused_reason_key"]
    data["refused"] = reason_key is not None
    data["refused_reason_params"] = json.loads(params_raw) if reason_key is not None else None
    return data


_PLAYLIST_ROW_COLUMNS = """
    p.id AS playlist_id, p.name AS name, p.slug AS slug, p.status AS status,
    p.delete_at AS delete_at, p.built_at AS built_at, p.created_at AS created_at,
    p.build_failure_reason AS refused_reason_key,
    p.build_failure_params::text AS build_failure_params_raw,
    count(pi.id) AS member_count,
    CASE WHEN count(pi.id) FILTER (WHERE e.duration_seconds IS NULL) = 0
         THEN sum(e.duration_seconds) ELSE NULL END AS duration_seconds,
    job.job_id AS job_id, job.job_kind AS job_kind, job.job_status AS job_status,
    job.job_db_stage AS job_db_stage, job.job_progress_pct AS job_progress_pct,
    job.job_eta_seconds AS job_eta_seconds, job.job_cancel_requested AS job_cancel_requested,
    job.job_queued_ahead AS job_queued_ahead
"""
_PLAYLIST_ROW_FROM = """
    FROM playlists p
    LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
    LEFT JOIN editions e ON e.id = pi.edition_id
    LEFT JOIN LATERAL (
        SELECT j.id AS job_id, j.kind AS job_kind, j.status AS job_status,
               j.stage AS job_db_stage, j.progress_pct AS job_progress_pct,
               j.eta_seconds AS job_eta_seconds,
               j.cancel_requested AS job_cancel_requested,
               (
                   SELECT count(*) FROM jobs j2
                   WHERE j2.status = 'queued' AND j2.kind = j.kind
                     AND j2.created_at < j.created_at
               ) AS job_queued_ahead
        FROM jobs j
        WHERE j.playlist_id = p.id AND j.status IN ('queued', 'running')
        ORDER BY j.created_at DESC
        LIMIT 1
    ) job ON true
"""
# Every joined `job.*` column is listed alongside `p.id` rather than left
# to functional-dependency inference: Postgres only infers that
# automatically for columns of the table whose own primary key is in
# GROUP BY (`p.*`, via `p.id`), not for a LATERAL-joined derived table --
# even though `job` is provably at most one row per `p.id` (the LATERAL's
# own LIMIT 1), the planner has no way to know that without these columns
# named explicitly.
_PLAYLIST_ROW_GROUP_BY = (
    "GROUP BY p.id, job.job_id, job.job_kind, job.job_status, job.job_db_stage, "
    "job.job_progress_pct, job.job_eta_seconds, job.job_cancel_requested, "
    "job.job_queued_ahead"
)


async def playlist_rows(conn: asyncpg.Connection) -> list[dict]:
    """Every non-deleted playlist with its member count and, when every
    member's duration is known, their sum -- the Library screen's
    playlist-row data source, newest first, parallel to `library_rows`.
    """
    rows = await conn.fetch(
        f"SELECT {_PLAYLIST_ROW_COLUMNS} {_PLAYLIST_ROW_FROM} "
        f"WHERE p.status != 'deleted' {_PLAYLIST_ROW_GROUP_BY} "
        "ORDER BY p.created_at DESC"
    )
    return [_playlist_row_from_record(row) for row in rows]


async def playlist_row(conn: asyncpg.Connection, playlist_id) -> dict | None:
    """One playlist row by id, same shape as `playlist_rows` -- the
    membership/assembly screen's data source.
    """
    row = await conn.fetchrow(
        f"SELECT {_PLAYLIST_ROW_COLUMNS} {_PLAYLIST_ROW_FROM} "
        f"WHERE p.id = $1 {_PLAYLIST_ROW_GROUP_BY}",
        playlist_id,
    )
    return _playlist_row_from_record(row) if row is not None else None


async def add_playlist_item(conn: asyncpg.Connection, playlist_id, edition_id) -> None:
    """Append `edition_id` to the end of `playlist_id`'s member list.
    Repeats are allowed (D-101) -- a position is its own entity, not a
    reference, so the same edition can be inserted more than once with no
    special case.
    """
    await conn.execute(
        """
        INSERT INTO playlist_items (playlist_id, edition_id, position)
        VALUES (
            $1, $2,
            COALESCE(
                (SELECT max(position) + 1 FROM playlist_items WHERE playlist_id = $1),
                0
            )
        )
        """,
        playlist_id,
        edition_id,
    )


async def playlist_detail(conn: asyncpg.Connection, playlist_id) -> dict | None:
    """One playlist row by id -- the assembly screen's own data source,
    same column set `resolve_playlist_slug` selects (minus the joined
    artifact key, which the assembly screen has no use for) plus the
    creation time.
    """
    row = await conn.fetchrow(
        """
        SELECT id, status, mode, built_at, name, slug, created_at
        FROM playlists
        WHERE id = $1
        """,
        playlist_id,
    )
    return dict(row) if row is not None else None


async def update_playlist(conn: asyncpg.Connection, playlist_id, name: str, slug: str) -> None:
    """Save a changed name and/or address -- the slug has already gone
    through the same `normalize_slug`/`pick_unique_slug` path the create
    route uses, with this playlist's own current slug excluded from the
    taken set (see routes_playlists.py's update route).
    """
    await conn.execute(
        "UPDATE playlists SET name = $2, slug = $3 WHERE id = $1",
        playlist_id,
        name,
        slug,
    )


async def playlist_members(conn: asyncpg.Connection, playlist_id) -> list[dict]:
    """Every member of `playlist_id`, in position order -- `item_id`,
    `edition_id`, `position`, `title_name`, `duration_seconds`, and
    `dropped` (whether the underlying title has been deleted).

    `dropped` is computed here, not inferred by the template (03-UI-SPEC.md
    States -> member-order row gap mark): the template must not have to
    know which status values mean gone. A title's edition rows survive its
    own deletion (see `referenced_objects`'s docstring -- only
    `titles.status` flips), so this is a LEFT JOIN all the way down and
    `dropped` reads the joined title's status directly rather than the
    presence of a row.
    """
    rows = await conn.fetch(
        """
        SELECT
            pi.id AS item_id,
            pi.position AS position,
            pi.edition_id AS edition_id,
            t.name AS title_name,
            e.duration_seconds AS duration_seconds,
            (t.id IS NULL OR t.status = 'deleted') AS dropped
        FROM playlist_items pi
        LEFT JOIN editions e ON e.id = pi.edition_id
        LEFT JOIN episodes ep ON ep.id = e.episode_id
        LEFT JOIN seasons s ON s.id = ep.season_id
        LEFT JOIN titles t ON t.id = s.title_id
        WHERE pi.playlist_id = $1
        ORDER BY pi.position
        """,
        playlist_id,
    )
    return [dict(row) for row in rows]


async def active_editions_for_picker(conn: asyncpg.Connection) -> list[dict]:
    """Every active title's default edition, newest first -- the assembly
    screen's member picker. Same default-edition-per-title shape
    `library_rows` already establishes (`_LIBRARY_ROW_FROM`'s own LATERAL
    join), restricted to active titles and narrowed to the three fields
    the picker row needs.
    """
    rows = await conn.fetch(
        """
        SELECT e.id AS edition_id, t.name AS title_name,
               e.duration_seconds AS duration_seconds
        FROM titles t
        JOIN LATERAL (
            -- "lowest episode" tiebreak reads episode_number_history, not
            -- the legacy episodes.number column -- see resolve_slug's own
            -- docstring (plan 10 fix).
            SELECT ep.default_edition_id
            FROM episodes ep
            JOIN seasons s ON s.id = ep.season_id
            LEFT JOIN episode_number_history enh
                ON enh.episode_id = ep.id AND enh.is_current
            WHERE s.title_id = t.id
            ORDER BY s.number, COALESCE(enh.number, ep.number)
            LIMIT 1
        ) first_ep ON true
        JOIN editions e ON e.id = first_ep.default_edition_id
        WHERE t.status = 'active'
        ORDER BY t.created_at DESC
        """
    )
    return [dict(row) for row in rows]


async def remove_playlist_item(conn: asyncpg.Connection, playlist_id, item_id) -> None:
    """Delete `item_id` from `playlist_id` and renumber the remainder to a
    contiguous run, in one statement, scoped to the playlist so an item
    identifier from a different playlist cannot delete anything here
    (T-03-18). Raises `LookupError` when no row matched that scoping --
    the caller answers 404 rather than a silent no-op redirect.
    """
    async with conn.transaction():
        result = await conn.execute(
            "DELETE FROM playlist_items WHERE id = $1 AND playlist_id = $2",
            item_id,
            playlist_id,
        )
        if result == "DELETE 0":
            raise LookupError("item not in this playlist")

        await conn.execute(
            """
            UPDATE playlist_items AS pi
            SET position = renumbered.new_position
            FROM (
                SELECT id, row_number() OVER (ORDER BY position) - 1 AS new_position
                FROM playlist_items
                WHERE playlist_id = $1
            ) AS renumbered
            WHERE pi.id = renumbered.id
            """,
            playlist_id,
        )


async def move_playlist_item(
    conn: asyncpg.Connection, playlist_id, item_id, direction: str
) -> None:
    """Move `item_id` one place `direction` ("up" or "down") within
    `playlist_id`, then rewrite every position in the playlist to a
    contiguous run, in one transaction, scoped to the playlist (T-03-18,
    T-03-19).

    Rewriting the whole run rather than swapping two numbers is
    deliberate and cheap at this scale: a playlist is tens of rows at
    most, a single rewrite can never leave a gap or a duplicate behind,
    and the alternative schemes (fractional positions, gap-based
    numbering) exist to solve a concurrent-multiple-writer problem this
    single-operator product does not have. A move at either end of the
    list, or a direction that is neither "up" nor "down", is a no-op --
    not an error and not a wrap; the caller's own UI disables the button
    that would ask for one at either end.
    """
    if direction not in ("up", "down"):
        return

    async with conn.transaction():
        rows = await conn.fetch(
            "SELECT id FROM playlist_items WHERE playlist_id = $1 ORDER BY position",
            playlist_id,
        )
        ids = [row["id"] for row in rows]
        try:
            index = ids.index(item_id)
        except ValueError:
            return

        step = 1 if direction == "down" else -1
        neighbor_index = index + step
        if neighbor_index < 0 or neighbor_index >= len(ids):
            return

        ids[index], ids[neighbor_index] = ids[neighbor_index], ids[index]

        await conn.executemany(
            "UPDATE playlist_items SET position = $2 WHERE id = $1",
            [(item_id_, position) for position, item_id_ in enumerate(ids)],
        )


async def mark_playlist_for_delete(conn: asyncpg.Connection, playlist_id, grace_seconds: int) -> None:
    """Flip a playlist to `pending_delete` with `delete_at = now() +
    grace_seconds` -- the same shape `mark_for_delete` already gives a
    title. `GET /p/{slug}` answers 404 for this status immediately (D-103:
    no tombstone), the grace window only delays the reaper's physical
    delete.
    """
    await conn.execute(
        """
        UPDATE playlists
        SET status = 'pending_delete', delete_at = now() + make_interval(secs => $2)
        WHERE id = $1
        """,
        playlist_id,
        grace_seconds,
    )


async def finish_delete_playlist(conn: asyncpg.Connection, playlist_id) -> None:
    """Mark a playlist deleted once its artifacts have been reclaimed."""
    await conn.execute("UPDATE playlists SET status = 'deleted' WHERE id = $1", playlist_id)


async def cancel_delete_playlist(conn: asyncpg.Connection, playlist_id) -> None:
    """Undo `mark_playlist_for_delete` -- back to active, `delete_at`
    cleared, `GET /p/{slug}` resolving again immediately. The same shape
    `cancel_delete` gives a title, and the same reason it exists: the
    grace window is only useful if something can call it off.
    """
    await conn.execute(
        "UPDATE playlists SET status = 'active', delete_at = NULL WHERE id = $1",
        playlist_id,
    )


async def due_for_reaping_playlists(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Playlists whose `delete_at` has passed -- identifiers only.

    **Deliberately not one key per playlist**, unlike `due_for_reaping`'s
    aggregated `object_keys`. A playlist's objects live in
    `playlist_artifacts`, one row per stored object: one mechanism
    produces a single assembled file, the other a segment per member, and
    a rebuild leaves a superseded generation behind that is reclaimed on
    its own schedule. The sweep reads those rows through
    `playlist_artifact_keys`. A signature returning one key per playlist
    would silently reclaim one object out of fifty and record the deletion
    as done with the rest of the bytes still in the bucket -- so the shape
    is stated here rather than left to be simplified back.
    """
    return await conn.fetch(
        """
        SELECT id AS playlist_id
        FROM playlists
        WHERE status = 'pending_delete' AND delete_at <= now()
        """
    )


async def playlist_artifact_keys(conn: asyncpg.Connection, playlist_id) -> list[str]:
    """Every stored object one playlist has produced -- current and
    superseded generations both, since a deletion reclaims all of them.

    Full storage keys including the extension, exactly as
    `playlist_artifacts.object_key` holds them: deleting with a bare
    identifier is a silent no-op against the object store, and the audit
    would go on reporting the surviving object as garbage forever.
    """
    rows = await conn.fetch(
        "SELECT object_key FROM playlist_artifacts WHERE playlist_id = $1",
        playlist_id,
    )
    return [row["object_key"] for row in rows]


async def delete_playlist_artifact(conn: asyncpg.Connection, artifact_id) -> None:
    """Drop one `playlist_artifacts` row, once its object is gone from
    the bucket. Object first, row second, never the reverse: this row is
    the only record of the key, so a failure between the two steps has to
    leave something the next cycle can retry against rather than an object
    nothing can name.
    """
    await conn.execute("DELETE FROM playlist_artifacts WHERE id = $1", artifact_id)


async def playlist_build_inputs(conn: asyncpg.Connection, playlist_id) -> list[dict]:
    """Everything one build needs about one playlist's members, in
    position order, from a single round trip: the stored object key, its
    size and duration, the title's name, the seven recorded stream
    parameters, and whether the title has been deleted.

    One query rather than a member-by-member fan-out on purpose. The
    caller runs on the event loop that also answers `GET /p/{slug}` and
    `/healthz`, and a fan-out there costs one round trip per member before
    anything else in this process gets a turn.

    `dropped` reads the joined title's status directly, exactly as
    `playlist_members` does -- an edition row survives its own title's
    deletion, so presence proves nothing.
    """
    rows = await conn.fetch(
        """
        SELECT
            pi.position AS position,
            e.id AS edition_id,
            e.object_key AS object_key,
            e.size_bytes AS size_bytes,
            e.duration_seconds AS duration_seconds,
            t.name AS title_name,
            e.fps AS fps,
            e.video_profile AS video_profile,
            e.video_level AS video_level,
            e.pix_fmt AS pix_fmt,
            e.audio_codec AS audio_codec,
            e.audio_sample_rate AS audio_sample_rate,
            e.audio_channel_layout AS audio_channel_layout,
            (t.id IS NULL OR t.status = 'deleted') AS dropped
        FROM playlist_items pi
        LEFT JOIN editions e ON e.id = pi.edition_id
        LEFT JOIN episodes ep ON ep.id = e.episode_id
        LEFT JOIN seasons s ON s.id = ep.season_id
        LEFT JOIN titles t ON t.id = s.title_id
        WHERE pi.playlist_id = $1
        ORDER BY pi.position
        """,
        playlist_id,
    )
    return [dict(row) for row in rows]


async def set_playlist_built(
    conn: asyncpg.Connection, playlist_id, mode: str, artifacts
) -> bool:
    """Point a playlist at what a build just produced. **One transaction,
    and its shape is the fix for two races** -- returns False when the
    build must be thrown away, True when the swap happened.

    `artifacts` is a sequence of mappings carrying `object_key` (the full
    storage key, extension included, mirroring `editions.object_key`),
    `position` (null for a single assembled file, the segment's place
    otherwise) and `duration_seconds`.

    The playlist row is taken inside the transaction with a row lock and
    its status read there. If it is no longer active the operator deleted
    it while this build ran and the reaper has already finished the row:
    nothing is written and False comes back, because inserting an artifact
    for a deleted playlist points a dead row at an object that is excluded
    from the referenced set and has already been swept past -- nothing
    would ever reclaim it. The caller deletes what it uploaded instead.

    The previous generation is identified by that same in-transaction
    read, never by a key the job carried from when it was constructed. Two
    saves in a row are ordinary here: the second job was built before the
    first one committed, so a key captured then names an artifact that is
    already gone and misses the one that is actually live -- deleting one
    that was already deleted and leaking the other. Superseding by
    statement, inside the lock, makes both races impossible rather than
    unlikely. Do not simplify the lock away.
    """
    async with conn.transaction():
        status = await conn.fetchval(
            "SELECT status FROM playlists WHERE id = $1 FOR UPDATE", playlist_id
        )
        if status != "active":
            return False

        await conn.execute(
            """
            UPDATE playlist_artifacts SET superseded_at = now()
            WHERE playlist_id = $1 AND superseded_at IS NULL
            """,
            playlist_id,
        )
        await conn.executemany(
            """
            INSERT INTO playlist_artifacts
                (playlist_id, object_key, position, duration_seconds)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (
                    playlist_id,
                    artifact["object_key"],
                    artifact.get("position"),
                    artifact.get("duration_seconds"),
                )
                for artifact in artifacts
            ],
        )
        await conn.execute(
            """
            UPDATE playlists
            SET mode = $2,
                built_at = now(),
                build_failure_reason = NULL,
                build_failure_params = '{}'::jsonb
            WHERE id = $1
            """,
            playlist_id,
            mode,
        )
        return True


async def set_playlist_build_failed(
    conn: asyncpg.Connection, playlist_id, reason_key: str, reason_params: dict
) -> None:
    """Record a refusal as a translation catalogue key plus the values its
    sentence substitutes -- never an assembled sentence and never raw tool
    output, the same rule the title failure column already carries.

    Touches no artifact row and does not clear `built_at`: a rebuild that
    failed leaves the previous generation serving, which is the whole
    promise that a rebuild never interrupts what someone is watching.
    """
    await conn.execute(
        """
        UPDATE playlists
        SET build_failure_reason = $2, build_failure_params = $3::jsonb
        WHERE id = $1
        """,
        playlist_id,
        reason_key,
        json.dumps(reason_params or {}),
    )


async def playlist_artifact_sections(conn: asyncpg.Connection, playlist_id) -> list[dict]:
    """The current generation's artifacts in play order -- what the public
    address renders its document from, and the same rows its validation
    reads its known keys from. **One query**, because that address runs on
    the event loop this whole process shares: a fan-out there stalls the
    video addresses and `/healthz` alongside it.

    Restricted to `superseded_at IS NULL`: the generation a rebuild
    replaced stays in the bucket for anyone still reading it, but it is
    never listed again.

    A member whose title has since been deleted is left out here, which is
    the whole of "a deleted clip drops out and the rest keep playing". The
    document is generated per request, so the next person to open the
    address simply gets a list without it, and anyone already watching
    keeps the copy they downloaded. Nothing is rebuilt and nothing is
    marked for the operator to fix.

    An artifact is matched to its member by position, which is what one
    segment per member buys. The pairing goes stale if the operator
    reorders or removes members without saving -- the artifacts still
    belong to the previous membership. That case keeps playing what was
    actually built (`pi.id IS NULL` admits an artifact with no member at
    its position) rather than refusing a working link, and the next save
    puts the two back in step.
    """
    rows = await conn.fetch(
        """
        SELECT pa.object_key AS object_key,
               pa.position AS position,
               pa.duration_seconds AS duration_seconds,
               t.name AS title_name
        FROM playlist_artifacts pa
        LEFT JOIN playlist_items pi
            ON pi.playlist_id = pa.playlist_id AND pi.position = pa.position
        LEFT JOIN editions e ON e.id = pi.edition_id
        LEFT JOIN episodes ep ON ep.id = e.episode_id
        LEFT JOIN seasons s ON s.id = ep.season_id
        LEFT JOIN titles t ON t.id = s.title_id
        WHERE pa.playlist_id = $1
          AND pa.superseded_at IS NULL
          AND (pi.id IS NULL OR t.id IS NULL OR t.status != 'deleted')
        ORDER BY pa.position NULLS FIRST
        """,
        playlist_id,
    )
    return [dict(row) for row in rows]


async def due_for_reclaim_artifacts(
    conn: asyncpg.Connection, older_than_seconds: int
) -> list[asyncpg.Record]:
    """Every artifact a rebuild superseded longer ago than
    `older_than_seconds`, with its key and its own row identifier.

    Superseded artifacts only. A deleted playlist's artifacts are the
    deletion sweep's business and are read by playlist; one query
    answering both questions would have to return two different shapes.

    Defined next to the writer that creates these rows rather than beside
    the sweep that consumes them: the retention rule is a property of how
    a rebuild swaps generations, and stated far from that it drifts. The
    age to pass is the signing lifetime -- the longest a link handed out
    before the rebuild can still be valid.
    """
    return await conn.fetch(
        """
        SELECT id, object_key, playlist_id
        FROM playlist_artifacts
        WHERE superseded_at IS NOT NULL
          AND superseded_at < now() - make_interval(secs => $1)
        """,
        older_than_seconds,
    )


async def editions_missing_technical(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Every active edition whose `fps` is still null -- what the
    `backfill-technical` command reads to fill in the seven joinability
    columns for a row added before this migration. Carries the identifier,
    the title's name, the source path and the three facts the probe/remux
    reconciliation needs (the selected audio stream index, its channel
    count and whether it was re-encoded).

    It also carries the six values `set_edition_technical` writes that this
    row already has. That writer updates all fifteen columns in one
    statement, so a backfill has to hand back what it is not there to
    change -- above all `size_bytes`, which describes the object in storage
    and is not the size of the source file the backfill probes.
    """
    return await conn.fetch(
        """
        SELECT
            e.id AS edition_id,
            t.name AS title_name,
            e.source_path AS source_path,
            e.audio_stream_index AS audio_stream_index,
            e.audio_channels AS audio_channels,
            e.audio_reencoded AS audio_reencoded,
            e.size_bytes AS size_bytes,
            e.duration_seconds AS duration_seconds,
            e.width AS width,
            e.height AS height,
            e.audio_language AS audio_language,
            e.audio_title AS audio_title
        FROM editions e
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        JOIN titles t ON t.id = s.title_id
        WHERE t.status = 'active' AND e.fps IS NULL
        """
    )


# --- Operator credential and session queries (0003_operator_auth.py) ------


async def operator_exists(conn: asyncpg.Connection) -> bool:
    """Whether the credential table already has its one row -- the
    first-run check that routes every request to the setup page while it
    is empty."""
    return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM operator_credential)")


async def create_operator(
    conn: asyncpg.Connection, operator_id, username: str, password_hash: str
) -> None:
    """Insert the one operator row. A unique violation propagates rather
    than being caught here -- the caller decides what to say about it.
    There are now two constraints that can raise it, the username and the
    one-row singleton index, and the caller must not try to tell them
    apart: both mean the same thing to a person, which is that this
    database already has its operator.
    """
    await conn.execute(
        "INSERT INTO operator_credential (id, username, password_hash) "
        "VALUES ($1, $2, $3)",
        operator_id,
        username,
        password_hash,
    )


async def credential_by_username(
    conn: asyncpg.Connection, username: str
) -> asyncpg.Record | None:
    """The stored hash and id for a login attempt to verify against."""
    return await conn.fetchrow(
        "SELECT id, password_hash FROM operator_credential WHERE username = $1",
        username,
    )


async def set_password_and_revoke_sessions(
    conn: asyncpg.Connection, operator_id, password_hash: str
) -> None:
    """Change the stored hash and kill every session in one transaction
    (D-86). A password is changed when someone believes it was taken, so
    leaving live sessions alive would leave exactly the access the change
    was meant to remove.
    """
    async with conn.transaction():
        await conn.execute(
            "UPDATE operator_credential "
            "SET password_hash = $2, password_changed_at = now() "
            "WHERE id = $1",
            operator_id,
            password_hash,
        )
        await conn.execute("DELETE FROM sessions WHERE operator_id = $1", operator_id)


async def create_session(
    conn: asyncpg.Connection,
    token_hash: str,
    operator_id,
    lifetime_seconds: int,
    expected_password_hash: str,
) -> bool:
    """Insert a session, but only if `expected_password_hash` still
    matches the stored hash at the instant of insert -- returns False,
    inserting nothing, when it does not.

    Not a plain INSERT. A login reads the stored hash, verifies it (which
    deliberately takes the better part of a second), and only then
    creates the session. A password reset changes the hash and deletes
    every session in one transaction. Those two overlap in the obvious
    way: a login that read the old hash before the reset would insert its
    session after it, and the reset's delete would have already run. The
    result would be a live session authenticated by the password that was
    just revoked, at the one moment the revocation exists for.

    Re-reading the hash without locking does not close it, because the
    reset may still be uncommitted at that instant. `FOR SHARE` makes the
    two orders serialize: if the reset holds the row, this statement
    waits for it, then re-evaluates its condition against the updated
    row, finds a different hash and inserts nothing; if this statement
    holds the row first, the reset waits, and its delete then removes the
    session that was just created. Either way the outcome is the one
    D-86 asks for.

    The caller treats False exactly like a wrong password -- the same
    generic message, the same delay -- because from the operator's side
    it is one: the password they typed is no longer the password.
    """
    result = await conn.fetchval(
        """
        INSERT INTO sessions (token_hash, operator_id, expires_at)
        SELECT $1, c.id, now() + make_interval(secs => $3)
          FROM operator_credential c
         WHERE c.id = $2 AND c.password_hash = $4
           FOR SHARE OF c
        RETURNING token_hash
        """,
        token_hash,
        operator_id,
        lifetime_seconds,
        expected_password_hash,
    )
    return result is not None


async def validate_and_renew_session(
    conn: asyncpg.Connection,
    token_hash: str,
    lifetime_seconds: int,
    renew_after_seconds: int,
) -> tuple:
    """Look up a session and slide its window forward in one statement,
    returning `(operator_id, renewed)`. `operator_id` is None for an
    expired or unknown token.

    Validation and renewal are one statement on purpose. A separate
    SELECT then UPDATE would let a request arrive between them and act on
    a session that has just expired.

    The renewal is throttled, and `renew_after_seconds` is what throttles
    it. The window still slides; it is simply not rewritten while the
    session's remaining life is within that much of the full lifetime.
    Writing on every request would mean a heap tuple and an index entry
    per request on a column that carries an index -- multiplied by every
    poster on a library render and by a four-second poll per preparing
    row per open tab, the sliding window would become the busiest writer
    in the system, for a session that is thirty days long. Throttled, it
    is at most one write a day per session and the operator cannot tell
    the difference.

    The `renewed` flag is not diagnostics. The cookie carries its own
    Max-Age, and the browser drops it that far after the cookie was
    issued no matter how much the row was renewed -- so a window that
    slid only in the database would still expire in the browser thirty
    days after the login. The gate re-issues the cookie exactly when this
    flag comes back true, which is the natural trigger the throttle
    creates. Without both halves the sliding window is a promise that
    only the database keeps.

    Note for tests: inside a transaction now() is the transaction's start
    time, so a test that opens a transaction, makes two requests and
    expects the second to differ from the first cannot pass however
    correct this query is.
    """
    row = await conn.fetchrow(
        """
        WITH renewed AS (
             UPDATE sessions
                SET expires_at = now() + make_interval(secs => $2)
              WHERE token_hash = $1
                AND expires_at > now()
                AND expires_at < now() + make_interval(secs => $2) - make_interval(secs => $3)
            RETURNING operator_id
        )
        SELECT s.operator_id, EXISTS (SELECT 1 FROM renewed) AS renewed
          FROM sessions s
         WHERE s.token_hash = $1 AND s.expires_at > now()
        """,
        token_hash,
        lifetime_seconds,
        renew_after_seconds,
    )
    if row is None:
        return None, False
    return row["operator_id"], row["renewed"]


async def delete_session(conn: asyncpg.Connection, token_hash: str) -> None:
    """Sign out one device (D-82) -- other sessions are untouched."""
    await conn.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)


async def delete_sessions_for_operator(conn: asyncpg.Connection, operator_id) -> None:
    """Every session for this operator, gone -- the bulk half of D-86's
    password-reset revocation, called directly by tests and by the CLI
    reset path.
    """
    await conn.execute("DELETE FROM sessions WHERE operator_id = $1", operator_id)


async def sweep_expired_sessions(conn: asyncpg.Connection) -> int:
    """Delete every session whose window has actually closed, returning
    how many rows were removed so the reaper can log something meaningful
    when it is not zero.
    """
    result = await conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    # asyncpg's command tag looks like "DELETE 3" -- the row count is
    # everything after the last space.
    return int(result.split()[-1])


# --- The durable job queue (D-118) --------------------------------------
#
# One table, `jobs` (migrations/versions/0005_jobs.py), holds all three
# kinds of background work -- `remux`, `playlist`, `transcode`. Every
# function below is a single statement or a tightly scoped pair; none
# opens a transaction the caller is expected to close, and `claim_job` in
# particular must never hold its row lock across the caller's own work
# (see its own docstring for why).

JOB_MAX_ATTEMPTS = 3

_FAILURE_REASON_KEY_RE = re.compile(r"^[a-z]+(\.[a-z_]+)+$")


def _validate_failure_reason_key(reason_key: str) -> None:
    """A failure reason is a dotted translation-catalog key, never a
    sentence and never tool output -- the same rule titles.failure_reason
    already carries, enforced here in code because this column is written
    by the worker over the network, a caller this module cannot vet at
    the call site the way an in-process one could be.
    """
    if not _FAILURE_REASON_KEY_RE.match(reason_key):
        raise ValueError(
            f"failure reason key {reason_key!r} is not a dotted catalog "
            "key -- only translation-catalog keys (e.g. "
            "'failed.reason.upload') may be stored here, never raw tool "
            "output or a sentence"
        )


async def enqueue_job(
    conn: asyncpg.Connection,
    *,
    kind: str,
    title_id=None,
    playlist_id=None,
    edition_id=None,
    source_path: str | None = None,
    audio_stream_index: int | None = None,
    subtitle_kind: str | None = None,
    subtitle_stream_index: int | None = None,
    subtitle_path: str | None = None,
    stereo_layout: str | None = None,
    external_audio_path: str | None = None,
    make_default_when_ready: bool = False,
):
    """Insert a queued job of `kind` and return its id.

    Carries only what the operator chose (D-119) -- source path, audio
    track, subtitle choice, 3D layout, an external audio choice (D-180)
    beside the embedded one, and now the default-edition request (D-178):
    `make_default_when_ready` is stored on the job rather than applied at
    save time, read back and acted on only once the job's own terminal
    write finds the edition it produced actually ready
    (`queries.sync_title_status`'s own callers). The worker re-probes the
    file and builds its own ffmpeg command at claim time with the same
    shared code the add form used, so shipping a command across the
    network here would only guarantee the two disagree.
    """
    return await conn.fetchval(
        """
        INSERT INTO jobs (
            kind, title_id, playlist_id, edition_id, source_path,
            audio_stream_index, subtitle_kind, subtitle_stream_index,
            subtitle_path, stereo_layout, external_audio_path,
            make_default_when_ready
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
        """,
        kind,
        title_id,
        playlist_id,
        edition_id,
        source_path,
        audio_stream_index,
        subtitle_kind,
        subtitle_stream_index,
        subtitle_path,
        stereo_layout,
        external_audio_path,
        make_default_when_ready,
    )


async def claim_job(
    conn: asyncpg.Connection, *, kind: str, worker_name: str
) -> asyncpg.Record | None:
    """Claim the oldest queued job of `kind` for `worker_name`, or return
    None when nothing is queued.

    One statement: a `SELECT ... FOR UPDATE SKIP LOCKED` CTE feeding the
    status-flipping `UPDATE`, run as a single `fetchrow` call with no
    transaction opened around it. This is deliberate, not merely
    convenient -- a claim spread across an opened transaction, a return to
    the caller, and a commit made later holds the row lock for as long as
    the caller works, which for a conversion is minutes across a network,
    and serialises the whole queue the moment a second claimer exists
    (WORK-03). Skipping locked rows rather than waiting on them means a
    slow or wedged claimer never blocks any other claimer behind it
    (T-04-15).
    """
    return await conn.fetchrow(
        """
        WITH next_job AS (
            SELECT id FROM jobs
            WHERE status = 'queued' AND kind = $1
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE jobs
        SET status = 'running', stage = 'working', attempts = attempts + 1,
            worker_name = $2, started_at = now(), heartbeat_at = now()
        FROM next_job
        WHERE jobs.id = next_job.id
        RETURNING jobs.*
        """,
        kind,
        worker_name,
    )


async def heartbeat_job(
    conn: asyncpg.Connection,
    job_id,
    *,
    worker_name: str,
    attempt: int,
    stage: str,
    progress_pct: float | None = None,
    eta_seconds: int | None = None,
    realtime_factor: float | None = None,
) -> asyncpg.Record | None:
    """Write progress only when the job id, the worker name and the
    attempt number all match the row as it currently stands; returns the
    row it wrote, or None when they do not (T-04-06).

    The mismatch case is the whole point, not an edge case: a converter
    that lost contact with the orchestrator and kept encoding must not be
    able to overwrite the progress of the attempt that superseded it once
    the reaper has already requeued the job and a second worker claimed
    it. The added `status = 'running'` closes the same gap one column
    over: a job the terminal write already finished (or a stale sweep
    already failed) still matches on worker name and attempt alone, and
    without this a late progress report would silently resurrect the
    stage/progress of a job that is no longer anyone's to report on.
    """
    return await conn.fetchrow(
        """
        UPDATE jobs
        SET stage = $4,
            progress_pct = COALESCE($5, progress_pct),
            eta_seconds = COALESCE($6, eta_seconds),
            realtime_factor = COALESCE($7, realtime_factor),
            heartbeat_at = now()
        WHERE id = $1 AND worker_name = $2 AND attempts = $3 AND status = 'running'
        RETURNING *
        """,
        job_id,
        worker_name,
        attempt,
        stage,
        progress_pct,
        eta_seconds,
        realtime_factor,
    )


async def finish_job(
    conn: asyncpg.Connection,
    job_id,
    *,
    worker_name: str,
    attempt: int,
    outcome: str,
    reason_key: str | None = None,
    realtime_factor: float | None = None,
) -> bool:
    """Record the terminal outcome (`done`, `failed` or `cancelled`) and
    report whether it wrote. Like `heartbeat_job`, writes only when the
    job id, the worker name and the attempt all match the row as it
    currently stands (T-04-06) -- both halves are the contract: a
    superseded attempt must neither silently write the row nor silently
    report success.

    `reason_key` is required for `outcome='failed'` and is validated
    against a dotted catalog-key shape before it is stored (T-04-07), so
    tool output cannot reach the column the library row renders even if a
    caller passes it.

    The added `status = 'running'` is what makes this a *terminal* write
    in fact, not merely in name: without it, a report that arrives after
    the job has already finished (a duplicate `done`, a late `failed`
    from an attempt the reaper already requeued and a second attempt
    already completed) still matches on worker name and attempt and
    rewrites a job -- and, through the caller's own follow-up write --
    an active title, back to whatever this stale report says. Every
    other writer of the `status` column (`claim_job`, `requeue_job`,
    `fail_job_exhausted`, `request_job_cancel`) only ever transitions a
    job *out of* `running`, never back into it, so this condition can
    never make a legitimate write unreachable -- confirmed by reading
    each of them, not assumed.
    """
    if outcome == "failed":
        if not reason_key:
            raise ValueError("finish_job(outcome='failed') requires reason_key")
        _validate_failure_reason_key(reason_key)
    row = await conn.fetchrow(
        """
        UPDATE jobs
        SET status = $4, finished_at = now(), failure_reason = $5,
            realtime_factor = COALESCE($6, realtime_factor)
        WHERE id = $1 AND worker_name = $2 AND attempts = $3 AND status = 'running'
        RETURNING id
        """,
        job_id,
        worker_name,
        attempt,
        outcome,
        reason_key,
        realtime_factor,
    )
    return row is not None


async def request_job_cancel(
    conn: asyncpg.Connection, job_id
) -> asyncpg.Record | None:
    """Ask for a job to stop (D-129).

    A running job has no way to be told anything except through its next
    heartbeat, so this only sets the flag and leaves the row `running` --
    the worker reads it back on its next heartbeat, kills ffmpeg and
    reports the outcome itself. A queued job has no worker to tell at
    all, so it is finished as `cancelled` outright, on the spot. The two
    cases are distinguishable by the caller through the returned row's
    own `status`: `running` (with `cancel_requested` now true) means
    "wait", `cancelled` means "already done, the row can disappear
    immediately".
    """
    row = await conn.fetchrow(
        """
        UPDATE jobs SET cancel_requested = true
        WHERE id = $1 AND status = 'running'
        RETURNING *
        """,
        job_id,
    )
    if row is not None:
        return row
    return await conn.fetchrow(
        """
        UPDATE jobs
        SET status = 'cancelled', finished_at = now()
        WHERE id = $1 AND status = 'queued'
        RETURNING *
        """,
        job_id,
    )


async def due_for_requeue(
    conn: asyncpg.Connection, *, stale_after_seconds: int
) -> list[asyncpg.Record]:
    """Every `running` job whose heartbeat has gone stale -- what the
    reaper's sweep reads before deciding, per row, whether to requeue it,
    fail it outright (attempts exhausted) or finish it cancelled (D-129).
    """
    return await conn.fetch(
        """
        SELECT * FROM jobs
        WHERE status = 'running'
          AND heartbeat_at < now() - make_interval(secs => $1)
        """,
        stale_after_seconds,
    )


async def requeue_job(conn: asyncpg.Connection, job_id) -> None:
    """Return a stale job to the queue: worker name, heartbeat and
    progress cleared, `attempts` preserved -- the attempt count is the
    cap's own memory and must survive a requeue for the cap to mean
    anything.
    """
    await conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', stage = 'queued', worker_name = NULL,
            heartbeat_at = NULL, progress_pct = NULL, eta_seconds = NULL,
            realtime_factor = NULL
        WHERE id = $1
        """,
        job_id,
    )


async def fail_job_exhausted(
    conn: asyncpg.Connection, job_id, *, reason_key: str
) -> None:
    """Mark a job failed because its attempt cap was reached -- not a
    requeue, and `attempts` is left exactly where it was, since this is
    the terminal outcome the cap exists to produce, not one more attempt.
    """
    _validate_failure_reason_key(reason_key)
    await conn.execute(
        """
        UPDATE jobs SET status = 'failed', finished_at = now(), failure_reason = $2
        WHERE id = $1
        """,
        job_id,
        reason_key,
    )


async def record_worker_seen(
    conn: asyncpg.Connection, *, worker_name: str, status: str
) -> None:
    """Upsert a converter's check-in: when it was last seen, and whether
    it is idle, working, or waiting for room on the shared card (D-113).

    `clock_timestamp()`, not `now()` -- the same reason `slug_history`
    uses it (migrations/versions/0001_initial_catalog.py): `now()` is
    frozen for the lifetime of a transaction, and two check-ins from the
    same test or the same short-lived caller would otherwise record an
    identical timestamp.
    """
    await conn.execute(
        """
        INSERT INTO worker_checkins (worker_name, last_seen_at, status)
        VALUES ($1, clock_timestamp(), $2)
        ON CONFLICT (worker_name) DO UPDATE
        SET last_seen_at = EXCLUDED.last_seen_at, status = EXCLUDED.status
        """,
        worker_name,
        status,
    )


async def last_worker_seen(conn: asyncpg.Connection) -> asyncpg.Record | None:
    """The most recently seen converter's name, time and status, or None
    when none has ever checked in -- the interface's three sentences
    (D-113/D-131): a named worker some minutes ago, none ever, or the
    named worker waiting for the card.
    """
    return await conn.fetchrow(
        "SELECT worker_name, last_seen_at, status FROM worker_checkins "
        "ORDER BY last_seen_at DESC LIMIT 1"
    )


async def count_queued_ahead(conn: asyncpg.Connection, job_id) -> int:
    """How many queued jobs of the *same kind* were created before this
    one (D-118) -- counted within a kind so a playlist assembly never
    appears to be ahead of a conversion in the position an operator is
    told.
    """
    return await conn.fetchval(
        """
        SELECT count(*) FROM jobs
        WHERE status = 'queued'
          AND kind = (SELECT kind FROM jobs WHERE id = $1)
          AND created_at < (SELECT created_at FROM jobs WHERE id = $1)
        """,
        job_id,
    )


async def job_for_title(conn: asyncpg.Connection, title_id) -> asyncpg.Record | None:
    """The live (queued or running) job for a title, if any -- lets the
    library render a stage without the caller knowing a job id.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM jobs
        WHERE title_id = $1 AND status IN ('queued', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        title_id,
    )


async def job_for_edition(conn: asyncpg.Connection, edition_id) -> asyncpg.Record | None:
    """The live (queued or running) job for one edition, if any -- the
    per-episode Cancel action's own read (D-164), scoped one level below
    `job_for_title`: an episode's default edition is what its row's own
    in-flight chip and Cancel control are keyed on.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM jobs
        WHERE edition_id = $1 AND status IN ('queued', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        edition_id,
    )


async def last_job_for_edition(conn: asyncpg.Connection, edition_id) -> asyncpg.Record | None:
    """The most recent job of any status for one edition -- what a
    per-episode Retry reads to requeue the same choices (D-164), the
    edition-scoped twin of `last_job_for_title`.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM jobs
        WHERE edition_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        edition_id,
    )


async def job_for_playlist(
    conn: asyncpg.Connection, playlist_id
) -> asyncpg.Record | None:
    """The live (queued or running) job for a playlist, if any -- the
    playlist-build equivalent of `job_for_title` above.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM jobs
        WHERE playlist_id = $1 AND status IN ('queued', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        playlist_id,
    )


async def last_job_for_title(conn: asyncpg.Connection, title_id) -> asyncpg.Record | None:
    """The most recent job of any status for a title -- what Retry reads
    to requeue the same choices (D-130), unlike `job_for_title`, which
    only ever answers a *live* job and would find nothing for the failed
    row Retry acts on.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM jobs
        WHERE title_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        title_id,
    )


async def last_job_for_playlist(conn: asyncpg.Connection, playlist_id) -> asyncpg.Record | None:
    """The playlist equivalent of `last_job_for_title` -- Build again's own
    read.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM jobs
        WHERE playlist_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        playlist_id,
    )


async def job_by_id(conn: asyncpg.Connection, job_id) -> asyncpg.Record | None:
    """The whole job row by its own id, whatever its status -- the
    worker-facing routes' own lookup for a job that a heartbeat or a
    report just named but whose write was refused (a mismatched attempt,
    or an id that never existed at all). `heartbeat_job`/`finish_job`
    themselves only ever report whether they wrote, not the row as it
    stands; this is how a caller finds out what a superseded report
    belonged to -- its title, its edition, what it was asked to do -- so
    it can act on that (e.g. deleting the orphan the superseded attempt
    uploaded) without trusting anything the request itself claimed.
    """
    return await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)


async def edition_object_key(conn: asyncpg.Connection, edition_id) -> str | None:
    """The stored object key for `edition_id`, or None if it does not
    exist. `jobs` carries no object_key column of its own -- the
    destination a transcode result lands at is always the edition's own,
    fixed when the edition row was created -- so this is the worker-facing
    routes' own lookup for it, from the job row rather than from anything
    the request itself claims (T-04-06).
    """
    return await conn.fetchval(
        "SELECT object_key FROM editions WHERE id = $1", edition_id
    )


async def edition_for_remux(conn: asyncpg.Connection, edition_id) -> asyncpg.Record | None:
    """The destination object key and the operator's chosen channel count
    for a claimed `remux` job -- the two edition-side facts `claim_job`'s
    own row cannot carry, since `jobs` has no `object_key` column (see
    `edition_object_key` above) and the channel count is recorded once on
    the edition at creation time rather than duplicated onto every job row
    of every kind. One query so the addition consumer builds its job from
    a single round trip, the same discipline `job_row` used to give the
    old in-memory queue before the durable table replaced it.
    """
    return await conn.fetchrow(
        "SELECT object_key, audio_channels FROM editions WHERE id = $1", edition_id
    )


async def set_edition_conversion_detail(
    conn: asyncpg.Connection,
    edition_id,
    *,
    source_codec: str | None,
    source_profile: str | None,
    subtitle_burned_language: str | None,
    subtitle_burned_title: str | None,
    stereo_layout: str | None,
) -> None:
    """Record what a conversion made and what it made it from (D-146) --
    a *separate* function from `set_edition_technical` above rather than
    five more keyword arguments on it: that one records what the stored
    object is, and every one of its callers knows all of it; this one
    records what the source was and what was done to it, which only a
    conversion knows.
    """
    await conn.execute(
        """
        UPDATE editions
        SET source_codec = $2, source_profile = $3,
            subtitle_burned_language = $4, subtitle_burned_title = $5,
            stereo_layout = $6
        WHERE id = $1
        """,
        edition_id,
        source_codec,
        source_profile,
        subtitle_burned_language,
        subtitle_burned_title,
        stereo_layout,
    )


async def median_realtime_factor(
    conn: asyncpg.Connection, kind: str = "transcode", last_n: int = 20
) -> float | None:
    """The median recorded speed of the last `last_n` finished jobs of
    `kind`, or None when none has finished yet -- what makes D-148's
    pre-start time estimate measured rather than assumed. Median, not
    mean: one pathological source triples a mean and leaves the estimate
    lying for weeks.
    """
    rows = await conn.fetch(
        """
        SELECT realtime_factor FROM (
            SELECT realtime_factor FROM jobs
            WHERE kind = $1 AND status = 'done' AND realtime_factor IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT $2
        ) recent
        """,
        kind,
        last_n,
    )
    values = sorted(row["realtime_factor"] for row in rows)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
