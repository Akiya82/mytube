"""The job queue gets an order that survives one-transaction batches.

`jobs.created_at` defaulted to `now()`, which is frozen for the whole
lifetime of a transaction. A folder save enqueues one job per episode
inside one transaction (`orchestrator.web.routes_titles`'s folder-move
path), so every job of an eleven-episode season landed with the exact same
timestamp -- "created earlier than mine" then counted nothing, for every
one of them, and the position an operator was shown read zero on every row.

This project has already paid for this exact lesson once, on
`slug_history.created_at` (migrations/versions/0001_initial_catalog.py):
the fix there was `clock_timestamp()`, the wall clock that advances inside
a transaction, and the fix here is the identical one-line change to the
identical kind of column.

This migration changes only the column default. It rewrites no existing
row: every job already in the queue keeps the `created_at` it already has.
Jobs that predate this migration and were enqueued together still share a
tied timestamp, and the ordering key `(created_at, id)` this plan's query
changes use resolves that tie on the row's own random identifier rather
than on enqueue order -- an arbitrary order among those tied rows, once,
not a reconstruction of the order they were actually written in. A
migration that rewrote timestamps to reconstruct enqueue order would be
real machinery bought against one season, with more ways to be wrong than
the symptom it cures.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-10
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ALTER COLUMN created_at SET DEFAULT clock_timestamp()")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs ALTER COLUMN created_at SET DEFAULT now()")
