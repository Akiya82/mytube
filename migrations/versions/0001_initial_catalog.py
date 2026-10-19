"""Initial catalog schema: titles, slug_history, seasons, episodes, editions.

Skeleton laid down all at once even though the first release only ever has
one title with one season/episode: retrofitting the title -> season -> episode
hierarchy later would mean migrating exactly the addresses that leave first
for other people's VRChat worlds.

slug_history has a COMPOSITE primary key (slug, title_id), not a single-
column `slug` key. A single-column `slug` PK cannot hold
two rows for the same slug text, which is exactly what slug reuse needs: a
tombstoned title's slug row must survive, and a second title must be able to
take the same slug text without an UPDATE that would overwrite (and thus
lose) the tombstone. Resolve rule for the app layer (orchestrator/db/
queries.py: resolve_slug): for a given slug, take the row with the newest
created_at. That one formula covers both rename (a slug has a single row,
still resolves after rename) and reuse (a slug has two rows, the newer one
wins, the older stays as history) -- see slug_history_slug_created_at below.

episodes.default_edition_id and editions.episode_id reference each other
(a slug resolves to a title plus a pointer to its default edition, and the
pointer has to live at the episode level). Postgres allows the cycle but
not within a single CREATE TABLE, since each table would need to exist before the other's FK can point at it:
editions is created first (episode_id column present but unconstrained),
then episodes (with its FK to editions.id), then a separate ALTER TABLE adds
editions.episode_id's FK now that episodes exists. downgrade() undoes this
in reverse: drop the added FK, then drop tables in dependency order.

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE titles (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text NOT NULL,
            status      text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'pending_delete', 'deleted')),
            delete_at   timestamptz,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE slug_history (
            slug        text NOT NULL,
            title_id    uuid NOT NULL REFERENCES titles(id),
            is_current  boolean NOT NULL DEFAULT true,
            -- clock_timestamp(), NOT now()/CURRENT_TIMESTAMP: now() is frozen
            -- for the lifetime of a transaction, so two slug_history rows
            -- inserted in the same transaction (e.g. a tombstoned title's
            -- deletion and its slug's reuse batched together) would get an
            -- identical created_at and make the "newest wins" resolve rule
            -- (see resolve_slug in orchestrator/db/queries.py) pick
            -- arbitrarily instead of correctly. clock_timestamp() advances
            -- on every call regardless of transaction boundaries.
            created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (slug, title_id)
        )
        """
    )
    # History rows are never deleted, only is_current flips on
    # rename. At most one current row per title (not per slug -- a title can
    # only have one "live" slug at a time, but a slug text can belong to two
    # different titles' history rows at once during a slug reuse).
    op.execute(
        """
        CREATE UNIQUE INDEX slug_history_one_current_per_title
        ON slug_history (title_id)
        WHERE is_current
        """
    )
    # Backs the resolve rule ("newest created_at wins for this slug").
    op.execute(
        """
        CREATE INDEX slug_history_slug_created_at
        ON slug_history (slug, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE seasons (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            title_id    uuid NOT NULL REFERENCES titles(id),
            number      integer NOT NULL DEFAULT 1
        )
        """
    )

    # Created before episodes: editions.episode_id has no FK yet (see module
    # docstring on the titles<->episodes cycle).
    op.execute(
        """
        CREATE TABLE editions (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            episode_id  uuid NOT NULL,
            slug        text,
            object_key  text NOT NULL UNIQUE,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE episodes (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            season_id           uuid NOT NULL REFERENCES seasons(id),
            number              integer NOT NULL DEFAULT 1,
            default_edition_id  uuid REFERENCES editions(id)
        )
        """
    )

    op.execute(
        """
        ALTER TABLE editions
        ADD CONSTRAINT editions_episode_id_fkey
        FOREIGN KEY (episode_id) REFERENCES episodes(id)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE editions DROP CONSTRAINT editions_episode_id_fkey")
    op.execute("DROP TABLE episodes")
    op.execute("DROP TABLE editions")
    op.execute("DROP TABLE seasons")
    op.execute("DROP INDEX slug_history_slug_created_at")
    op.execute("DROP INDEX slug_history_one_current_per_title")
    op.execute("DROP TABLE slug_history")
    op.execute("DROP TABLE titles")
