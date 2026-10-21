"""Operator credential and session tables.

Gives the interface an operator to be and a session to hold. Both are
tables rather than process state: an in-process dictionary would sign the
operator out on every rebuild, and every release changes the image.

**Every `CREATE` below carries `IF NOT EXISTS`, and the reason is a real
recovery path rather than defensive habit.** The documented restore applies
a dump taken with `--clean --if-exists` over a live database and then
brings the stack back up, which runs `alembic upgrade head` on every
container start. A dump taken before this revision existed carries no
`operator_credential` and no `sessions`, so it emits no `DROP` for either --
but it does restore `alembic_version` to the revision it was taken at. The
result is a database whose version says `0002` while both new tables are
still standing, and the next start replays this revision against them.
Without `IF NOT EXISTS` the `CREATE` fails, the entrypoint runs under
`set -eu` and dies, and `edge` waits on a health condition that will never
come -- so restoring a backup would take video down for viewers as well as
the interface. That is the most expensive failure this migration can produce
and it costs three words to close.

**Migrations still run from the container entrypoint with no advisory
lock, and that stays as it is.** This is a real hazard for a service with
more than one replica: two containers starting together would both run
`alembic upgrade head` and race. This compose stack runs exactly one `api`,
compose stops the old container before starting the new one, and the
entrypoint already carries a comment saying migrations have to move out of
it the day a second replica appears. Adding a lock now would be a
mechanism whose failure mode nothing in this project can exercise, guarding
a race nothing in this project can produce. Recorded here as considered and
declined rather than missed; the entrypoint's existing note is where a
future reader will find it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-23
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_credential (
            id                    uuid PRIMARY KEY,
            username              text NOT NULL UNIQUE,
            password_hash         text NOT NULL,
            created_at            timestamptz NOT NULL DEFAULT now(),
            password_changed_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # `UNIQUE` on `username` only stops two rows sharing a name -- it does
    # NOT stop a second operator, because two different usernames satisfy
    # it perfectly. This product has exactly one operator and no notion of
    # a second, and that fact was otherwise enforced by a Python check in
    # front of two write paths -- the interface's own first-run page and
    # the in-container command -- which is a rule that holds only as long
    # as nobody adds a third path and nobody runs the two at once. A unique
    # index on a constant expression admits exactly one row whatever the
    # username is, and it holds under concurrency because it is an index.
    # Both writers get a unique violation instead of a second operator, and
    # neither has to remember to look first.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS operator_credential_singleton "
        "ON operator_credential ((true))"
    )
    # password_hash holds an argon2 encoded string, never a password.
    # password_changed_at moves on every rotation and is the column a
    # reset is visible in. The credential row deliberately survives a
    # pg_dump restore, unlike sessions below -- a restore that produces a
    # stack with nobody able to sign in would be the worse outcome.

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash   text PRIMARY KEY,
            operator_id  uuid NOT NULL REFERENCES operator_credential(id) ON DELETE CASCADE,
            created_at   timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL
        )
        """
    )
    # The primary key is a sha256 of the token, never the token itself, so
    # a row read out of the database by anything other than this
    # application is not a bearer credential. `ON DELETE CASCADE` is how
    # removing an operator removes their sessions without a second
    # statement. `expires_at` moves forward on every renewed request,
    # which is what makes the 30-day window sliding rather than absolute
    # .
    op.execute(
        "CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions (expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS sessions_expires_at_idx")
    op.execute("DROP INDEX IF EXISTS operator_credential_singleton")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP TABLE IF EXISTS operator_credential")
