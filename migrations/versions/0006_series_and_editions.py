"""Readiness moved down to the edition, a delete countdown scoped to one
episode, a slug that can point at an episode, and a number-history table
shaped exactly like the slug history -- the four mechanisms the numbered
address (GET /v/{slug}/{number}) needs that the schema has never had.

**Readiness moves from the title to the edition.** `titles.status` used to
be the only readiness signal there was, correct when a title had exactly
one address. With a season converting episode by episode, the first ready
episode has to answer while the twelfth is still queued -- one column
blanketing every address a title owns can no longer say that. `editions
.status` (preparing/active/failed) and `episodes.status`
(active/pending_delete/deleted) are the two new signals; `resolve_slug`,
`resolve_edition` and the two numbered resolvers this phase adds all read
them instead of trusting the title alone. The alternative rejected here is
a single richer title-level enum trying to encode "which of my episodes are
ready" -- that pushes the fan-out into a single column's values instead of
where it structurally belongs, one level down.

**The delete countdown gets an episode-scoped twin.** `episodes.delete_at`
is the same five-minute grace window, undo and reaper sweep `titles
.delete_at` already gives a whole title, scoped to one episode's editions
-- so retiring a single episode of a series no longer has to retire the
whole title to get the same undo safety.

**A slug learns to point at an episode.** `slug_history.episode_id`,
nullable, `REFERENCES episodes(id) ON DELETE SET NULL`. An ordinary title
slug leaves it null and resolves to the title's lowest episode exactly as
before; a slug that followed one standalone file into a series carries it
and resolves to that one episode forever, wherever the file ends up.
`ON DELETE SET NULL` is not decoration: `delete_failed_title` and
`delete_never_active_title` (orchestrator/db/queries.py) both run
`DELETE FROM episodes` before `DELETE FROM slug_history`, so without it
either hard-delete path raises a foreign-key violation the moment this
migration lands. Setting it null on delete is the correct outcome rather
than a workaround for that ordering: the row falls back to resolving
through the title pointer it always had, and an episode whose address was
ever served is tombstoned by `finish_delete_episode` rather than hard-
deleted, so no address that is still live can be nulled out this way.

**Episode numbers get a history table.** `episode_number_history` is
`slug_history` (migrations/versions/0001_initial_catalog.py) shape for
shape, for the identical reason: a title id, a number, an episode id, a
current flag, and `created_at` defaulting to `clock_timestamp()` rather
than `now()` -- `now()` is frozen for the life of a transaction, so two
rows written together (a renumber and a create, say) would tie and make
`resolve_episode_slug`'s (orchestrator/db/queries.py) newest-wins rule pick
arbitrarily instead of correctly, exactly the reason 0001's own
`slug_history.created_at` comment gives. The foreign key to `episodes` is
`ON DELETE CASCADE`, not `SET NULL` like the slug pointer above: a number-
history row means nothing once its episode is gone (there is no "resolves
to the title instead" fallback for a number the way there is for a slug),
and the two hard-delete paths already drop the episode row before the
title row, so the cascade fires at the same point in that same order.
Primary key over (title, number, episode) rather than a surrogate id,
because the identical (title, number) pair legitimately names two
different episodes across time (a renumber, or a number changing hands)
and `claim_episode_number` (orchestrator/db/queries.py) re-inserts the
exact same triple on a renumber-back exactly as `rename_slug` re-inserts
the exact same (slug, title) pair on a rename-back -- both do it with an
`ON CONFLICT ... DO UPDATE`, which needs that triple to be the conflict
target. `episode_number_history_one_current_per_episode`, a partial unique
index on (title, episode) where `is_current`, is the number-history
equivalent of `slug_history_one_current_per_title`: at most one current
number per episode. `episode_number_history_lookup`, on (title, number,
created_at DESC), backs the resolve rule the same way
`slug_history_slug_created_at` backs `resolve_slug` -- newest row for a
given (title, number) wins, with no `is_current` filter in that query,
because "newest" and "current" answer two different questions here exactly
as they do for a slug: `is_current` says what number an episode holds
*now*, the resolve rule says what episode a number *answers as* right now,
and an old number kept by nobody newer still has to resolve to the episode
that last claimed it.

**Other additive columns, each mirroring one this schema already has:**

- `editions.subtitle_burned_signs_only`, boolean, nullable -- sits beside
  the two burned-in subtitle columns 0005_jobs.py added. Nullable rather
  than defaulting to false, because "not known" (an edition from before
  this attribute existed) and "not signs-only" are different facts, and
  the interface only shows the attribute once it has actually been
  resolved for a given edition.
- `editions.started_at`, timestamptz, nullable -- the edition-level twin of
  `titles.started_at` (0002_add_as_is.py), null while its own conversion
  job is only queued and set the moment a worker actually picks it up.
  Added here rather than left for a later migration: readiness already
  moved from the title to the edition in this same revision, and the
  "queued vs. actually working" distinction that column exists for has to
  move with it -- a title-level `started_at` cannot answer that question
  for one episode out of twelve converting at different times.
- `titles.changed_at`, timestamptz, not null, defaulting to `now()` --
  the library's new ordering key, so a title rises when an episode or
  edition is added or becomes ready instead of staying pinned to its
  original creation time forever.
- `jobs.external_audio_path`, text, nullable -- sits beside the existing
  `subtitle_path` payload column. A job carries what the operator chose,
  nothing derived from it: an external audio file is exactly as much a
  choice as a subtitle sidecar is. Unconstrained against the filesystem on
  purpose -- containment is the route's job before the row is written and
  the converter's job again before the file is opened, never the
  database's.
- `jobs.make_default_when_ready`, boolean, not null, defaulting to false --
  the operator's own tick asking that the edition this job produces become
  its episode's default. Stored rather than applied at save time, so the
  pointer only moves once the edition is actually ready.

**Backfill, in the same upgrade.** Without this, the numbered address
resolves nothing for the whole catalog the owner already has -- every
title that predates this migration would need re-adding for its own
address to work, which is exactly the manual step this phase exists to
remove. One `INSERT ... SELECT` writes a current `episode_number_history`
row for every episode that already exists, taking its title id from the
episode's own season and its number from the episode row itself. A second
statement sets `titles.changed_at` from each title's own `created_at`, so
the new ordering starts out identical to the one it replaces instead of
reshuffling the library the moment this lands.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-09
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Readiness moves to the edition and the episode -------------------
    op.execute(
        """
        ALTER TABLE editions ADD COLUMN status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('preparing', 'active', 'failed'))
        """
    )
    op.execute("ALTER TABLE editions ADD COLUMN failure_reason text")
    op.execute("ALTER TABLE editions ADD COLUMN subtitle_burned_signs_only boolean")
    op.execute(
        """
        COMMENT ON COLUMN editions.subtitle_burned_signs_only IS
        'Nullable on purpose: null means not yet resolved (an edition from '
        'before this attribute existed, or one whose subtitle was never '
        'burned), false means resolved and not signs-only. The interface '
        'only shows the badge once this is genuinely known.'
        """
    )
    op.execute("ALTER TABLE editions ADD COLUMN started_at timestamptz")

    op.execute(
        """
        ALTER TABLE episodes ADD COLUMN status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'pending_delete', 'deleted'))
        """
    )
    op.execute("ALTER TABLE episodes ADD COLUMN delete_at timestamptz")

    # --- A slug can point at one specific episode --------------------------
    op.execute(
        """
        ALTER TABLE slug_history ADD COLUMN episode_id uuid
        REFERENCES episodes(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN slug_history.episode_id IS
        'Null for an ordinary title slug, which keeps resolving through '
        'the title''s lowest episode exactly as before this column existed. '
        'Set for a slug that followed one standalone file into a series, so '
        'that address keeps answering with its own episode forever rather '
        'than the series'' first one. ON DELETE SET NULL rather than '
        'CASCADE: delete_failed_title and delete_never_active_title '
        '(orchestrator/db/queries.py) both remove episodes before '
        'slug_history rows, so this has to survive that order, and falling '
        'back to the title pointer is the right outcome anyway -- an '
        'episode whose address was ever actually served is tombstoned by '
        'finish_delete_episode, never hard-deleted, so a live pointer can '
        'never be the one this clause nulls out.'
        """
    )

    # --- The library's ordering key -----------------------------------------
    op.execute(
        "ALTER TABLE titles ADD COLUMN changed_at timestamptz NOT NULL DEFAULT now()"
    )

    # --- What the operator chose, for a job still to carry ------------------
    op.execute("ALTER TABLE jobs ADD COLUMN external_audio_path text")
    op.execute(
        "ALTER TABLE jobs ADD COLUMN make_default_when_ready boolean NOT NULL DEFAULT false"
    )

    # --- Episode-number history, shaped like slug_history --------------------
    op.execute(
        """
        CREATE TABLE episode_number_history (
            title_id    uuid NOT NULL REFERENCES titles(id),
            number      integer NOT NULL,
            episode_id  uuid NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            is_current  boolean NOT NULL DEFAULT true,
            -- clock_timestamp(), NOT now(): see slug_history.created_at's
            -- own comment in 0001_initial_catalog.py for the exact race
            -- this closes -- now() is frozen for the whole transaction and
            -- would tie two rows written together, making the newest-wins
            -- resolve rule (resolve_episode_slug, orchestrator/db/queries.py)
            -- pick arbitrarily instead of correctly.
            created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (title_id, number, episode_id)
        )
        """
    )
    # At most one current number per episode -- the number-history
    # equivalent of slug_history_one_current_per_title.
    op.execute(
        """
        CREATE UNIQUE INDEX episode_number_history_one_current_per_episode
        ON episode_number_history (title_id, episode_id)
        WHERE is_current
        """
    )
    # Backs the resolve rule: newest created_at wins for a given
    # (title, number), with no is_current filter -- the same shape
    # slug_history_slug_created_at backs resolve_slug with.
    op.execute(
        """
        CREATE INDEX episode_number_history_lookup
        ON episode_number_history (title_id, number, created_at DESC)
        """
    )

    # --- Backfill: every episode already in the catalog gets its history ---
    # row, so the numbered address answers for data written before today
    # with no operator action and no second migration.
    op.execute(
        """
        INSERT INTO episode_number_history (title_id, number, episode_id, is_current)
        SELECT s.title_id, e.number, e.id, true
        FROM episodes e
        JOIN seasons s ON s.id = e.season_id
        """
    )
    # The library's new ordering starts out identical to the one it
    # replaces, rather than shuffling on first deploy.
    op.execute("UPDATE titles SET changed_at = created_at")


def downgrade() -> None:
    op.execute("DROP INDEX episode_number_history_lookup")
    op.execute("DROP INDEX episode_number_history_one_current_per_episode")
    op.execute("DROP TABLE episode_number_history")

    op.execute("ALTER TABLE jobs DROP COLUMN make_default_when_ready")
    op.execute("ALTER TABLE jobs DROP COLUMN external_audio_path")

    op.execute("ALTER TABLE titles DROP COLUMN changed_at")

    op.execute("ALTER TABLE slug_history DROP COLUMN episode_id")

    op.execute("ALTER TABLE episodes DROP COLUMN delete_at")
    op.execute("ALTER TABLE episodes DROP COLUMN status")

    op.execute("ALTER TABLE editions DROP COLUMN started_at")
    op.execute("ALTER TABLE editions DROP COLUMN subtitle_burned_signs_only")
    op.execute("ALTER TABLE editions DROP COLUMN failure_reason")
    op.execute("ALTER TABLE editions DROP COLUMN status")
