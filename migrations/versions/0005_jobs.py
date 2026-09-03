"""Schema for the durable job queue: one table for all three kinds of
background work (`remux`, `playlist`, `transcode`), a converter check-in
table, and the five columns a converted edition needs to explain itself
later.

Three tables would be three mechanisms to keep in step and would still
leave a restart losing work (D-118) -- `jobs` is one table, and `kind` is
constrained to exactly the three literals this codebase's call sites use,
so a typo in a future caller fails loudly instead of creating a job
nothing will ever consume.

Every column that names what a job carries is nullable, because which
columns apply depends on `kind`: a `transcode`/`remux` job needs
`title_id` and `edition_id`, a `playlist` job needs `playlist_id`, and
neither kind needs the other's reference. The second CHECK constraint
below is what actually enforces that split, so a caller that mismatches
`kind` and its reference fails at INSERT time rather than producing a row
half the codebase will read wrong.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-03
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- The only three kinds of background work this project has --
            -- these three literals, spelled nowhere else, in the column,
            -- the check, every call site and this migration's own prose.
            -- A typo here is a database error at insert time, not a job
            -- nothing will ever consume.
            kind        text NOT NULL
                        CHECK (kind IN ('remux', 'playlist', 'transcode')),
            -- The lifecycle: queued -> running -> done/failed/cancelled.
            -- Separate from `stage` below -- this is what the claim, the
            -- heartbeat and the reaper's sweep all read and write.
            status      text NOT NULL DEFAULT 'queued'
                        CHECK (status IN
                            ('queued', 'running', 'done', 'failed', 'cancelled')),
            -- What the operator is told, distinct from `status`: a running
            -- job is either working or uploading, and "uploading" is not a
            -- different status, it is a different sentence over the same
            -- 'running' row. Also carries 'cancelling', the confirmed-but-
            -- not-yet-actioned state D-129 needs between the cancel request
            -- and the worker (or the reaper) actually finishing the job.
            stage       text NOT NULL DEFAULT 'queued'
                        CHECK (stage IN
                            ('queued', 'working', 'uploading', 'cancelling')),
            attempts    integer NOT NULL DEFAULT 0,
            worker_name text,
            heartbeat_at  timestamptz,
            progress_pct  double precision,
            eta_seconds   integer,
            realtime_factor  double precision,
            -- D-129: the cancel button sets this rather than mutating
            -- status directly -- a running job's worker learns of it from
            -- the heartbeat response and finishes the job itself; a queued
            -- job with no worker to tell is finished immediately instead
            -- (see queries.request_job_cancel).
            cancel_requested  boolean NOT NULL DEFAULT false,
            -- A translation catalog key, never a sentence -- same rule as
            -- titles.failure_reason (migrations/versions/0002_add_as_is.py)
            -- and playlists.build_failure_reason, for the same reason: the
            -- interface is localized.
            failure_reason  text,
            -- The payload columns: what the operator chose, never anything
            -- ffmpeg-shaped. The worker re-probes the file at claim time
            -- and builds its own command, so shipping a command across the
            -- network would only guarantee the two disagree.
            title_id    uuid REFERENCES titles(id) ON DELETE CASCADE,
            playlist_id uuid REFERENCES playlists(id) ON DELETE CASCADE,
            edition_id  uuid REFERENCES editions(id) ON DELETE CASCADE,
            source_path  text,
            audio_stream_index  integer,
            subtitle_kind  text,
            subtitle_stream_index  integer,
            subtitle_path  text,
            stereo_layout  text,
            started_at   timestamptz,
            finished_at  timestamptz,
            created_at   timestamptz NOT NULL DEFAULT now(),
            -- A job carries exactly the reference its own kind needs: a
            -- playlist job has playlist_id and neither title reference, a
            -- remux/transcode job has both title_id and edition_id and no
            -- playlist_id. Enforced here rather than left to application
            -- code, so a call site that mismatches kind and reference
            -- fails loudly at insert time.
            CONSTRAINT jobs_kind_matches_reference CHECK (
                (kind IN ('remux', 'transcode')
                    AND title_id IS NOT NULL AND edition_id IS NOT NULL
                    AND playlist_id IS NULL)
                OR
                (kind = 'playlist'
                    AND playlist_id IS NOT NULL
                    AND title_id IS NULL AND edition_id IS NULL)
            )
        )
        """
    )
    # The claim's own access path: the oldest queued row of a given kind.
    op.execute(
        "CREATE INDEX jobs_kind_status_created_at "
        "ON jobs (kind, status, created_at)"
    )
    # The stale sweep's own access path: running rows by heartbeat age.
    op.execute(
        "CREATE INDEX jobs_status_heartbeat_at "
        "ON jobs (status, heartbeat_at)"
    )

    op.execute(
        """
        CREATE TABLE worker_checkins (
            worker_name  text PRIMARY KEY,
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            -- D-113: a converter that is up but waiting for room on the
            -- shared card is a third sentence, not a fallback onto the
            -- other two -- without it the interface can only say "this
            -- converter, N minutes ago" or "none has ever connected",
            -- both of which point an owner at a converter that is in fact
            -- healthy and simply waiting.
            status       text NOT NULL DEFAULT 'idle'
                         CHECK (status IN ('idle', 'waiting_for_gpu', 'working'))
        )
        """
    )

    # A converted edition's library row explains itself a month later
    # (D-146): the codec AND the profile it was converted from (the
    # profile is the half that usually explains WHY conversion was
    # needed -- a ten-bit high profile is the single most common reason in
    # this library), which subtitle was burned in, and what stereo layout
    # the stored object is in. Nullable: every existing row predates these
    # and none of them is knowable retroactively. stereo_layout is written
    # by both add branches (see plan 10), not only the conversion one -- a
    # file that arrived already side-by-side needs its badge too.
    op.execute("ALTER TABLE editions ADD COLUMN source_codec text")
    op.execute("ALTER TABLE editions ADD COLUMN source_profile text")
    op.execute("ALTER TABLE editions ADD COLUMN subtitle_burned_language text")
    op.execute("ALTER TABLE editions ADD COLUMN subtitle_burned_title text")
    op.execute("ALTER TABLE editions ADD COLUMN stereo_layout text")


def downgrade() -> None:
    op.execute("ALTER TABLE editions DROP COLUMN stereo_layout")
    op.execute("ALTER TABLE editions DROP COLUMN subtitle_burned_title")
    op.execute("ALTER TABLE editions DROP COLUMN subtitle_burned_language")
    op.execute("ALTER TABLE editions DROP COLUMN source_profile")
    op.execute("ALTER TABLE editions DROP COLUMN source_codec")

    op.execute("DROP TABLE worker_checkins")

    op.execute("DROP INDEX jobs_status_heartbeat_at")
    op.execute("DROP INDEX jobs_kind_status_created_at")
    op.execute("DROP TABLE jobs")
