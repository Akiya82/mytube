"""Schema for the as-is add flow: a title mid-flight, an edition that
remembers its source and its poster.

Every column here exists because plan 03's add form needs to answer one of
four questions the schema could not answer before: is this file already
added (source_path), what did ffprobe already tell us about it
(size/duration/width/height/audio_*), where does its poster frame live
(poster_key), and is a title still being prepared or did it fail
(status/failure_reason/started_at). See the columns below for the specific
decision each one carries.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # titles.failure_reason holds a TRANSLATION CATALOG KEY, never a
    # sentence -- the interface is localized (D-66), so an English sentence
    # stored here would be untranslatable at render time. Values look like
    # "failed.reason.upload" or "failed.reason.interrupted", matching the
    # keys already living in orchestrator/locales/{en,ru}.json.
    op.execute("ALTER TABLE titles ADD COLUMN failure_reason text")

    # titles.started_at: null while a title is only queued, set the moment
    # a worker actually picks it up (mark_title_started, called by the
    # worker at dequeue time, never by the route). Two things need this and
    # neither can be faked from a single-value status column: the interface
    # has to tell "waiting" from "working" (D-45's estimate line reads
    # differently for each), and restart reconciliation (preparing_rows_at_
    # startup below) has to tell a row that was mid-remux from a row that
    # never started -- calling both "interrupted" would be a lie about the
    # row that was only ever sitting in the queue, and the operator acts on
    # that word.
    op.execute("ALTER TABLE titles ADD COLUMN started_at timestamptz")

    # titles.status gains 'preparing' and 'failed' (D-49/D-53). The
    # constraint declared inline in 0001 was unnamed and Postgres named it
    # titles_status_check by its own default convention for a single-column
    # inline CHECK -- confirmed live against the running database
    # (`\d titles`) rather than assumed, per this revision's own plan.
    # Consequence for GET /v/{slug}, stated once here rather than left
    # implicit: a title in 'preparing' or 'failed' is not 'active', so the
    # existing resolve route already answers 410 for it with no code
    # change -- the address has not been handed to anyone yet.
    op.execute("ALTER TABLE titles DROP CONSTRAINT titles_status_check")
    op.execute(
        """
        ALTER TABLE titles ADD CONSTRAINT titles_status_check
        CHECK (status IN ('active', 'preparing', 'failed', 'pending_delete', 'deleted'))
        """
    )

    # editions.source_path (D-48): the share-relative path the edition came
    # from, so a second attempt at the same file can be recognised and
    # offered its existing link instead of silently creating a duplicate.
    # Nullable -- older rows (and any future non-share source) have none.
    op.execute("ALTER TABLE editions ADD COLUMN source_path text")
    op.execute(
        """
        CREATE INDEX editions_source_path
        ON editions (source_path)
        WHERE source_path IS NOT NULL
        """
    )

    # Technical detail the library row shows (D-60), all produced by the
    # ffprobe call the add form already makes on click (D-56).
    op.execute("ALTER TABLE editions ADD COLUMN size_bytes bigint")
    op.execute("ALTER TABLE editions ADD COLUMN duration_seconds double precision")
    op.execute("ALTER TABLE editions ADD COLUMN width integer")
    op.execute("ALTER TABLE editions ADD COLUMN height integer")
    op.execute("ALTER TABLE editions ADD COLUMN audio_language text")
    op.execute("ALTER TABLE editions ADD COLUMN audio_title text")
    op.execute(
        "ALTER TABLE editions ADD COLUMN audio_reencoded boolean NOT NULL DEFAULT false"
    )

    # editions.audio_stream_index / audio_channels: not display fields --
    # the two facts a background job cannot reconstruct on its own once the
    # in-process queue (D-49/D-50) is gone from memory. With them on the
    # row, a job is rebuildable from the catalog alone (job_row below), and
    # the queue stops being the only place the operator's audio-track
    # choice exists.
    op.execute("ALTER TABLE editions ADD COLUMN audio_stream_index integer")
    op.execute("ALTER TABLE editions ADD COLUMN audio_channels integer")

    # editions.poster_key (D-72): the object key of the poster frame.
    # Nullable -- extraction can fail and a missing poster must degrade to
    # a placeholder rather than block an add.
    op.execute("ALTER TABLE editions ADD COLUMN poster_key text")


def downgrade() -> None:
    op.execute("ALTER TABLE editions DROP COLUMN poster_key")
    op.execute("ALTER TABLE editions DROP COLUMN audio_channels")
    op.execute("ALTER TABLE editions DROP COLUMN audio_stream_index")
    op.execute("ALTER TABLE editions DROP COLUMN audio_reencoded")
    op.execute("ALTER TABLE editions DROP COLUMN audio_title")
    op.execute("ALTER TABLE editions DROP COLUMN audio_language")
    op.execute("ALTER TABLE editions DROP COLUMN height")
    op.execute("ALTER TABLE editions DROP COLUMN width")
    op.execute("ALTER TABLE editions DROP COLUMN duration_seconds")
    op.execute("ALTER TABLE editions DROP COLUMN size_bytes")
    op.execute("DROP INDEX editions_source_path")
    op.execute("ALTER TABLE editions DROP COLUMN source_path")

    op.execute("ALTER TABLE titles DROP CONSTRAINT titles_status_check")
    op.execute(
        """
        ALTER TABLE titles ADD CONSTRAINT titles_status_check
        CHECK (status IN ('active', 'pending_delete', 'deleted'))
        """
    )
    op.execute("ALTER TABLE titles DROP COLUMN started_at")
    op.execute("ALTER TABLE titles DROP COLUMN failure_reason")
