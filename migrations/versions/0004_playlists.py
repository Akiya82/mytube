"""Schema for playlists: the address, the ordered membership, the artifact
table that holds what either assembly mechanism produces, and the seven
edition columns the joinability check needs.

`playlists` carries no artifact-key column at all. What a playlist has
produced lives in `playlist_artifacts` below, one row per stored object,
and the address query reads the current one through a join. Two reasons:
a mechanism that produces many objects (a segmented list) cannot fit in
one column, and a single column would have to be both the current pointer
and the reclamation record at once -- which is what makes a rebuild either
delete something a viewer is still reading or leak it.

`playlist_artifacts` is created unconditionally now, before either
assembly mechanism is chosen, because adding it after the mechanism lands
would mean a second migration in the middle of the build work. Under the
mechanism that produces a single assembled file it holds exactly one row
per build and costs nothing.

The seven new `editions` columns describe the stored object, not the
source file -- see their own comment below.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE playlists (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text NOT NULL,
            -- A plain unique slug, not tombstoned, unlike a video's
            -- address: renaming a playlist breaks its old
            -- address. Chosen for schema simplicity; the retrofit, if
            -- ever revisited, is a polymorphic owner column on the slug
            -- history table plus a migration.
            slug        text NOT NULL UNIQUE,
            status      text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'pending_delete', 'deleted')),
            -- Which mechanism produced what is in playlist_artifacts:
            -- 'pending' means nothing yet, 'concat' means one row,
            -- 'hls' means one row per segment in position order.
            -- Only 'pending' and 'hls' are ever written: physical joining
            -- was ruled out by measurement, so nothing produces a
            -- 'concat' row and no code reads for one. The value is kept
            -- in the check deliberately -- narrowing it costs a migration
            -- for no behavioural gain, and a re-encoding stage could want
            -- it back. Its presence here is not evidence that anything
            -- writes it.
            mode        text NOT NULL DEFAULT 'pending'
                        CHECK (mode IN ('pending', 'concat', 'hls')),
            -- The readiness signal, deliberately separate from anything
            -- about the objects -- one of the two mechanisms produces no
            -- single object to point at. The public address answers
            -- "not ready" on this column alone, never on a key and never
            -- on a member count: a viewer cannot act on the difference
            -- between an empty list and an unfinished one.
            built_at    timestamptz,
            -- A translation catalogue key, never a sentence -- the same
            -- rule the title failure column already carries, for the
            -- same reason (the interface is localized). build_failure_params
            -- holds the values the sentence substitutes.
            build_failure_reason  text,
            build_failure_params  jsonb NOT NULL DEFAULT '{}'::jsonb,
            delete_at   timestamptz,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE playlist_items (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            playlist_id uuid NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            edition_id  uuid NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
            -- Deliberately no UNIQUE constraint on (playlist_id, position):
            -- a reorder rewrites every position in the list in one
            -- statement and a unique constraint would fire on the
            -- transient overlap. The same edition is allowed to appear
            -- twice at different positions because a position is
            -- its own entity, not a reference.
            position    integer NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX playlist_items_playlist_id_position "
        "ON playlist_items (playlist_id, position)"
    )

    op.execute(
        """
        CREATE TABLE playlist_artifacts (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            playlist_id uuid NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            -- The FULL storage key, extension included, exactly the shape
            -- editions.object_key already holds -- keys are shaped
            -- "{a fresh identifier}.mp4" under either mechanism, segments
            -- included, so the delivery block's existing address pattern,
            -- the existing signer and the reclamation query all cover
            -- them unchanged. Reclamation and the audit compare this
            -- directly against a bucket listing, which carries
            -- extensions, so a bare identifier here would make every
            -- removal a silent no-op.
            object_key  text NOT NULL UNIQUE,
            -- Null for a single assembled file, set for a segment -- this
            -- one nullable integer is the whole difference between the
            -- two mechanisms at the storage layer.
            position    integer,
            duration_seconds  double precision,
            created_at  timestamptz NOT NULL DEFAULT now(),
            -- Null for the artifacts a playlist is currently served from,
            -- set for the generation a rebuild replaced. A superseded
            -- artifact is NOT deleted at swap time: a viewer holding a
            -- signed address is still reading it, and the address is
            -- valid for the full signing lifetime. It stays in the
            -- bucket, stays counted as referenced, and is reclaimed by
            -- the background sweep once it is older than that lifetime.
            -- A build never overwrites an object -- it writes new keys
            -- and moves the pointer by setting this column on the old
            -- generation in the same transaction, so the change is
            -- atomic from every reader's point of view.
            superseded_at  timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX playlist_artifacts_playlist_id_superseded_position "
        "ON playlist_artifacts (playlist_id, superseded_at, position)"
    )

    # These columns describe the STORED object, not the source file. The
    # video stream is always copied, so its parameters carry over
    # unchanged; the audio does not, because the remux re-encodes it to
    # one codec whenever the source codec is not already that one. Two
    # members that are genuinely identical in the bucket -- one copied,
    # one converted -- would look like a mismatch if the source codec
    # were recorded, and the joinability check would refuse a list that
    # would have joined perfectly. What gets written here is the
    # effective post-remux value.
    #
    # Rows added before this migration carry nulls. A null means "never
    # measured", neither "matches" nor "does not match" -- the refusal
    # path says so in its own words rather than telling the operator
    # their clips are incompatible. The command that fills them in is
    # backfill-technical.
    op.execute("ALTER TABLE editions ADD COLUMN fps double precision")
    op.execute("ALTER TABLE editions ADD COLUMN video_profile text")
    op.execute("ALTER TABLE editions ADD COLUMN video_level integer")
    op.execute("ALTER TABLE editions ADD COLUMN pix_fmt text")
    op.execute("ALTER TABLE editions ADD COLUMN audio_codec text")
    op.execute("ALTER TABLE editions ADD COLUMN audio_sample_rate integer")
    op.execute("ALTER TABLE editions ADD COLUMN audio_channel_layout text")


def downgrade() -> None:
    op.execute("ALTER TABLE editions DROP COLUMN audio_channel_layout")
    op.execute("ALTER TABLE editions DROP COLUMN audio_sample_rate")
    op.execute("ALTER TABLE editions DROP COLUMN audio_codec")
    op.execute("ALTER TABLE editions DROP COLUMN pix_fmt")
    op.execute("ALTER TABLE editions DROP COLUMN video_level")
    op.execute("ALTER TABLE editions DROP COLUMN video_profile")
    op.execute("ALTER TABLE editions DROP COLUMN fps")

    op.execute("DROP INDEX playlist_artifacts_playlist_id_superseded_position")
    op.execute("DROP TABLE playlist_artifacts")

    op.execute("DROP INDEX playlist_items_playlist_id_position")
    op.execute("DROP TABLE playlist_items")

    op.execute("DROP TABLE playlists")
