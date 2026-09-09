"""An edition gets its own delete countdown (plan 11, D-178).

The library now offers "Make default" and "Delete" on every non-default
edition of an episode (`orchestrator/web/routes_episodes.py`'s four new
`/library/edition/{id}/...` routes). Delete follows the exact same
undo/purge/reaper-sweep shape a title's and an episode's own delete already
have -- a mouse slip on a five-minute-old dub must be as recoverable as a
mouse slip on a whole episode.

`editions.status` gains `pending_delete`, matching `titles.status` and
`episodes.status`'s own vocabulary (0002_add_as_is.py, 0006_series_and_
editions.py) exactly so `_readiness_status_code` and every other reader that
already treats a non-`active` status as "not currently playable" needs no
new case. No `deleted` value is added: unlike a title or an episode, nothing
in this schema keeps a per-edition slug-history-shaped record for an
edition's own address to tombstone through, so a reaped edition is dropped
outright (`queries.delete_edition`, unchanged) rather than flipped to a
value nothing ever reads. `delete_at`, timestamptz nullable, is the plain
edition-scoped twin of `episodes.delete_at`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-09
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editions DROP CONSTRAINT editions_status_check")
    op.execute(
        """
        ALTER TABLE editions ADD CONSTRAINT editions_status_check
        CHECK (status IN ('preparing', 'active', 'failed', 'pending_delete'))
        """
    )
    op.execute("ALTER TABLE editions ADD COLUMN delete_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE editions DROP COLUMN delete_at")
    op.execute("ALTER TABLE editions DROP CONSTRAINT editions_status_check")
    op.execute(
        """
        ALTER TABLE editions ADD CONSTRAINT editions_status_check
        CHECK (status IN ('preparing', 'active', 'failed'))
        """
    )
