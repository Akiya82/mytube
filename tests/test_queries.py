"""Integration tests for the phase-2 additions to orchestrator/db/queries.py
-- the schema and query surface plan 03 task 1 lays down (0002_add_as_is.py).
Marked `integration`, same as test_slug_history.py -- these need Docker.
"""
import asyncio
import uuid

import asyncpg
import pytest

from orchestrator.db import queries
from orchestrator.storage import reconcile
from orchestrator.slugs import pick_unique_slug

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _normalize_new_statuses_after_test(raw_conn):
    """raw_conn commits for real against the one session-scoped Postgres
    every test file in this run shares (see conftest.py). A 'preparing' or
    'failed' row left behind by this file would make
    test_slug_history.py::test_migration_downgrade_and_upgrade_round_trip
    fail later in the same session -- that test's downgrade re-adds the
    original three-value status constraint, which any leftover new-status
    row violates. Not a test smell to work around quietly: every test here
    that creates a 'preparing'/'failed' row is asserting something about
    that exact state, so the fix is to normalize afterwards, not to avoid
    creating them.
    """
    yield
    await raw_conn.execute(
        "UPDATE titles SET status = 'active' WHERE status IN ('preparing', 'failed')"
    )


async def _create_title(conn, name: str, slug: str, **kwargs) -> uuid.UUID:
    edition_id = uuid.uuid4()
    return await queries.create_title_with_edition(
        conn, name, slug, edition_id, f"{edition_id}.mp4", **kwargs
    )


async def test_new_status_values_are_accepted_and_invalid_one_is_rejected(raw_conn):
    preparing_id = await _create_title(raw_conn, "Preparing Row", "preparing-row-slug", status="preparing")
    row = await raw_conn.fetchrow("SELECT status FROM titles WHERE id = $1", preparing_id)
    assert row["status"] == "preparing"

    failed_id = await _create_title(raw_conn, "Failed Row", "failed-row-slug", status="failed")
    row = await raw_conn.fetchrow("SELECT status FROM titles WHERE id = $1", failed_id)
    assert row["status"] == "failed"

    with pytest.raises(asyncpg.CheckViolationError):
        await raw_conn.execute(
            "INSERT INTO titles (name, status) VALUES ($1, $2)",
            "Bogus Status Row",
            "not-a-real-status",
        )


async def test_create_title_with_edition_preparing_and_source_path_round_trips(raw_conn):
    title_id = await _create_title(
        raw_conn,
        "Round Trip Preparing",
        "round-trip-preparing-slug",
        status="preparing",
        source_path="Movies/round-trip.mkv",
        audio_stream_index=1,
        audio_channels=6,
    )

    row = await raw_conn.fetchrow(
        """
        SELECT t.status AS status, e.source_path AS source_path,
               e.audio_stream_index AS audio_stream_index, e.audio_channels AS audio_channels
        FROM titles t
        JOIN seasons s ON s.title_id = t.id
        JOIN episodes ep ON ep.season_id = s.id
        JOIN editions e ON e.id = ep.default_edition_id
        WHERE t.id = $1
        """,
        title_id,
    )
    assert row["status"] == "preparing"
    assert row["source_path"] == "Movies/round-trip.mkv"
    assert row["audio_stream_index"] == 1
    assert row["audio_channels"] == 6


async def test_editions_by_source_paths_finds_the_row_by_its_path(raw_conn):
    await _create_title(
        raw_conn, "Findable By Path", "findable-by-path-slug",
        source_path="Movies/findable.mkv",
    )

    matches = await queries.editions_by_source_paths(
        raw_conn, ["Movies/findable.mkv", "Movies/no-such-file.mkv"]
    )

    assert len(matches) == 1
    assert matches[0]["source_path"] == "Movies/findable.mkv"
    assert matches[0]["title_name"] == "Findable By Path"
    assert matches[0]["slug"] == "findable-by-path-slug"
    assert matches[0]["title_status"] == "active"


async def test_editions_by_source_paths_empty_list_returns_empty(raw_conn):
    assert await queries.editions_by_source_paths(raw_conn, []) == []


async def test_count_preparing_ahead_returns_right_number_with_three_rows_in_flight(raw_conn):
    # The shared session-scoped database may already carry 'preparing' rows
    # left over from earlier tests in this run -- count_preparing_ahead has
    # no per-test scoping to filter by, so this asserts the *relative*
    # ordering these three rows introduce rather than an absolute zero.
    first_id = await _create_title(
        raw_conn, "Queue First", "queue-first-slug", status="preparing"
    )
    await asyncio.sleep(0.01)
    second_id = await _create_title(
        raw_conn, "Queue Second", "queue-second-slug", status="preparing"
    )
    await asyncio.sleep(0.01)
    third_id = await _create_title(
        raw_conn, "Queue Third", "queue-third-slug", status="preparing"
    )

    ahead_first = await queries.count_preparing_ahead(raw_conn, first_id)
    ahead_second = await queries.count_preparing_ahead(raw_conn, second_id)
    ahead_third = await queries.count_preparing_ahead(raw_conn, third_id)

    assert ahead_second == ahead_first + 1
    assert ahead_third == ahead_first + 2


async def test_failed_title_object_keys_returns_both_keys_without_deleting_anything(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Doomed Failed Title", "doomed-failed-title-slug", edition_id,
        f"{edition_id}.mp4", status="failed",
    )
    await queries.set_edition_poster(raw_conn, edition_id, f"{edition_id}-poster.jpg")

    keys = await queries.failed_title_object_keys(raw_conn, title_id)

    assert set(keys) == {f"{edition_id}.mp4", f"{edition_id}-poster.jpg"}

    # Read-only: the row this plan (02-05) relies on for a crash-safe
    # dismiss (delete objects, using these keys, before the rows go) must
    # still exist after asking for the keys.
    still_there = await raw_conn.fetchrow("SELECT id FROM titles WHERE id = $1", title_id)
    assert still_there is not None


async def test_failed_title_object_keys_returns_only_video_key_when_poster_is_null(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Doomed No Poster", "doomed-no-poster-slug", edition_id,
        f"{edition_id}.mp4", status="failed",
    )

    keys = await queries.failed_title_object_keys(raw_conn, title_id)

    assert keys == [f"{edition_id}.mp4"]


async def test_failed_title_object_keys_refuses_an_active_title(raw_conn):
    title_id = await _create_title(raw_conn, "Very Much Active Keys", "very-much-active-keys-slug")

    with pytest.raises(ValueError):
        await queries.failed_title_object_keys(raw_conn, title_id)


async def test_delete_failed_title_removes_every_row(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Doomed Failed Title Rows", "doomed-failed-title-rows-slug", edition_id,
        f"{edition_id}.mp4", status="failed",
    )
    await queries.set_edition_poster(raw_conn, edition_id, f"{edition_id}-poster.jpg")

    await queries.delete_failed_title(raw_conn, title_id)

    remaining_title = await raw_conn.fetchrow("SELECT id FROM titles WHERE id = $1", title_id)
    remaining_edition = await raw_conn.fetchrow(
        "SELECT id FROM editions WHERE id = $1", edition_id
    )
    remaining_slug_history = await raw_conn.fetch(
        "SELECT title_id FROM slug_history WHERE title_id = $1", title_id
    )
    assert remaining_title is None
    assert remaining_edition is None
    assert remaining_slug_history == []


async def test_delete_failed_title_leaves_an_active_title_untouched(raw_conn):
    title_id = await _create_title(raw_conn, "Very Much Active", "very-much-active-slug")

    with pytest.raises(ValueError):
        await queries.delete_failed_title(raw_conn, title_id)

    row = await raw_conn.fetchrow("SELECT status FROM titles WHERE id = $1", title_id)
    assert row is not None
    assert row["status"] == "active"


async def test_due_for_reaping_returns_the_poster_key(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Reaping With Poster", "reaping-with-poster-slug", edition_id,
        f"{edition_id}.mp4",
    )
    await queries.set_edition_poster(raw_conn, edition_id, f"{edition_id}-poster.jpg")
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=0)

    rows = await queries.due_for_reaping(raw_conn)
    row = next(r for r in rows if r["title_id"] == title_id)

    assert list(row["object_keys"]) == [f"{edition_id}.mp4"]
    assert list(row["poster_keys"]) == [f"{edition_id}-poster.jpg"]


async def test_referenced_objects_returns_the_serving_slug_alongside_each_key(raw_conn):
    edition_id = uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Referenced Title", "referenced-title-slug", edition_id,
        f"{edition_id}.mp4",
    )

    rows = await queries.referenced_objects(raw_conn)
    video_row = next(r for r in rows if r["key"] == f"{edition_id}.mp4")

    assert video_row["kind"] == "video"
    assert video_row["slug"] == "referenced-title-slug"
    assert video_row["title_status"] == "active"


async def test_referenced_objects_drops_a_title_once_its_objects_are_gone(raw_conn):
    """The reaper deletes a title's objects and then calls finish_delete.
    If those keys stayed in the referenced set, reconcile would report them
    missing from the bucket forever -- the header would show a permanently
    growing "addresses broken" count and storage-audit would exit non-zero
    for the rest of the install's life, over deletions that worked exactly
    as asked. The one signal that means "a live address points at nothing"
    has to keep meaning that.
    """
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Reaped Title", "reaped-title-slug", edition_id,
        f"{edition_id}.mp4",
    )
    await queries.set_edition_poster(raw_conn, edition_id, f"{edition_id}.jpg")

    # Still referenced while the deletion is undoable: the objects are
    # untouched until the grace period expires, so a bucket without them
    # really would be a broken address.
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=3600)
    pending_keys = {r["key"] for r in await queries.referenced_objects(raw_conn)}
    assert f"{edition_id}.mp4" in pending_keys
    assert f"{edition_id}.jpg" in pending_keys

    await queries.finish_delete(raw_conn, title_id)

    rows = await queries.referenced_objects(raw_conn)
    assert f"{edition_id}.mp4" not in {r["key"] for r in rows}
    assert f"{edition_id}.jpg" not in {r["key"] for r in rows}

    # An empty bucket for this title is now the correct, quiet outcome:
    # nothing missing, and nothing unreferenced either.
    result = reconcile({}, [r for r in rows if str(edition_id) in r["key"]])
    assert result.missing == []
    assert result.unreferenced == []


async def test_cancel_delete_returns_title_to_active(raw_conn):
    title_id = await _create_title(raw_conn, "Cancel Me", "cancel-me-slug")
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=3600)

    await queries.cancel_delete(raw_conn, title_id)

    row = await raw_conn.fetchrow(
        "SELECT status, delete_at FROM titles WHERE id = $1", title_id
    )
    assert row["status"] == "active"
    assert row["delete_at"] is None


async def test_mark_title_active_and_failed_and_started(raw_conn):
    title_id = await _create_title(
        raw_conn, "Lifecycle Title", "lifecycle-title-slug", status="preparing"
    )

    await queries.mark_title_started(raw_conn, title_id)
    row = await raw_conn.fetchrow("SELECT started_at FROM titles WHERE id = $1", title_id)
    assert row["started_at"] is not None

    await queries.mark_title_failed(raw_conn, title_id, "failed.reason.upload")
    row = await raw_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title_id
    )
    assert row["status"] == "failed"
    assert row["failure_reason"] == "failed.reason.upload"

    await queries.mark_title_active(raw_conn, title_id)
    row = await raw_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title_id
    )
    assert row["status"] == "active"
    assert row["failure_reason"] is None


async def test_set_edition_technical_and_poster(raw_conn):
    edition_id = uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Technical Detail Title", "technical-detail-title-slug", edition_id,
        f"{edition_id}.mp4",
    )

    await queries.set_edition_technical(
        raw_conn, edition_id,
        size_bytes=123456, duration_seconds=90.5, width=1920, height=1080,
        audio_language="jpn", audio_title="Original", audio_reencoded=False,
    )
    await queries.set_edition_poster(raw_conn, edition_id, f"{edition_id}-poster.jpg")

    row = await raw_conn.fetchrow(
        "SELECT size_bytes, duration_seconds, width, height, audio_language, "
        "audio_title, audio_reencoded, poster_key FROM editions WHERE id = $1",
        edition_id,
    )
    assert row["size_bytes"] == 123456
    assert row["width"] == 1920
    assert row["audio_language"] == "jpn"
    assert row["audio_reencoded"] is False
    assert row["poster_key"] == f"{edition_id}-poster.jpg"


async def test_library_rows_and_library_row_include_new_title(raw_conn):
    title_id = await _create_title(raw_conn, "Library Fresh Title", "library-fresh-title-slug")

    rows = await queries.library_rows(raw_conn)
    assert any(row["title_id"] == title_id for row in rows)

    row = await queries.library_row(raw_conn, title_id)
    assert row is not None
    assert row["title_name"] == "Library Fresh Title"
    assert row["slug"] == "library-fresh-title-slug"


async def test_concurrent_create_with_same_candidate_gets_two_different_slugs(migrated_db_url):
    """Two connections race to claim the same candidate slug through the
    real create path (advisory lock + taken_slugs read inside one
    transaction, exactly the pattern create_title_with_edition's docstring
    requires of callers). Without SLUG_CLAIM_LOCK_KEY this test is flaky at
    best -- with it, the second worker's taken_slugs read only happens
    after the first worker has committed, so it always sees the first
    worker's slug as taken.
    """
    candidate = "racer"

    async def worker(name: str) -> str:
        conn = await asyncpg.connect(migrated_db_url)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
                )
                taken = await queries.taken_slugs(conn)
                slug, _similar = pick_unique_slug(candidate, taken)
                edition_id = uuid.uuid4()
                await queries.create_title_with_edition(
                    conn, name, slug, edition_id, f"{edition_id}.mp4"
                )
                return slug
        finally:
            await conn.close()

    slug_a, slug_b = await asyncio.gather(worker("Racer A"), worker("Racer B"))

    assert slug_a != slug_b
    assert {slug_a, slug_b} == {"racer", "racer-2"}


async def _add_second_episode(conn, title_id) -> uuid.UUID:
    """A second season/episode/edition under an existing title -- what the
    schema has always allowed and the next phase actually writes.
    """
    season_id = await conn.fetchval(
        "INSERT INTO seasons (title_id, number) VALUES ($1, 2) RETURNING id", title_id
    )
    episode_id = await conn.fetchval(
        "INSERT INTO episodes (season_id, number) VALUES ($1, 1) RETURNING id", season_id
    )
    edition_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO editions (id, episode_id, object_key) VALUES ($1, $2, $3)",
        edition_id,
        episode_id,
        f"{edition_id}.mp4",
    )
    await conn.execute(
        "UPDATE episodes SET default_edition_id = $2 WHERE id = $1", episode_id, edition_id
    )
    return edition_id


async def test_a_second_episode_does_not_duplicate_the_library_row(raw_conn):
    """Joined plainly, a title with two episodes yields two rows: the
    Library renders the title twice and the single-row poll picks an
    arbitrary one of them.
    """
    first_edition = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn,
        "Two Episode Title",
        "two-episode-title",
        first_edition,
        f"{first_edition}.mp4",
    )
    await _add_second_episode(raw_conn, title_id)

    rows = await queries.library_rows(raw_conn)
    mine = [row for row in rows if row["title_id"] == title_id]
    assert len(mine) == 1
    assert mine[0]["edition_id"] == first_edition

    row = await queries.library_row(raw_conn, title_id)
    assert row["edition_id"] == first_edition



# --- Widened technical detail, playlist CRUD, and the artifact union ------
# branch -- extends referenced_objects's existing UNION ALL rather than a
# second query. Every function called below is new; none exists yet, so
# each test fails on its own first call to `queries` rather than at
# collection.


async def test_set_edition_technical_persists_the_stream_parameters(raw_conn):
    edition_id = uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Stream Parameters Title", "stream-parameters-title-slug",
        edition_id, f"{edition_id}.mp4",
    )

    await queries.set_edition_technical(
        raw_conn, edition_id,
        size_bytes=123456, duration_seconds=90.5, width=1920, height=1080,
        audio_language="jpn", audio_title="Original", audio_reencoded=False,
        fps=23.976, video_profile="High", video_level=41, pix_fmt="yuv420p",
        audio_codec="aac", audio_sample_rate=48000, audio_channel_layout="stereo",
    )

    row = await raw_conn.fetchrow(
        "SELECT fps, video_profile, video_level, pix_fmt, audio_codec, "
        "audio_sample_rate, audio_channel_layout FROM editions WHERE id = $1",
        edition_id,
    )
    assert row["fps"] == pytest.approx(23.976)
    assert row["video_profile"] == "High"
    assert row["video_level"] == 41
    assert row["pix_fmt"] == "yuv420p"
    assert row["audio_codec"] == "aac"
    assert row["audio_sample_rate"] == 48000
    assert row["audio_channel_layout"] == "stereo"


async def test_playlist_create_and_resolve_round_trip(raw_conn):
    playlist_id = await queries.create_playlist(raw_conn, "Round Trip Playlist", "round-trip-playlist-slug")

    row = await queries.resolve_playlist_slug(raw_conn, "round-trip-playlist-slug")

    assert row is not None
    assert row["id"] == playlist_id
    assert row["mode"] == "pending"
    assert row["built_object_key"] is None
    assert row["built_at"] is None


async def test_playlist_items_keep_their_order_and_allow_repeats(raw_conn):
    playlist_id = await queries.create_playlist(raw_conn, "Ordered Playlist", "ordered-playlist-slug")
    edition_id = uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Ordered Member", "ordered-member-slug", edition_id, f"{edition_id}.mp4"
    )

    await queries.add_playlist_item(raw_conn, playlist_id, edition_id)
    await queries.add_playlist_item(raw_conn, playlist_id, edition_id)

    members = await queries.playlist_members(raw_conn, playlist_id)

    assert len(members) == 2
    assert [m["edition_id"] for m in members] == [edition_id, edition_id]
    positions = [m["position"] for m in members]
    assert positions == sorted(positions)


async def test_moving_a_playlist_item_rewrites_positions_without_gaps(raw_conn):
    playlist_id = await queries.create_playlist(raw_conn, "Movable Playlist", "movable-playlist-slug")
    edition_ids = [uuid.uuid4() for _ in range(3)]
    for i, edition_id in enumerate(edition_ids):
        await queries.create_title_with_edition(
            raw_conn, f"Movable Member {i}", f"movable-member-{i}-slug", edition_id, f"{edition_id}.mp4"
        )
        await queries.add_playlist_item(raw_conn, playlist_id, edition_id)

    members = await queries.playlist_members(raw_conn, playlist_id)
    first_item_id = members[0]["item_id"]

    await queries.move_playlist_item(raw_conn, playlist_id, first_item_id, "down")

    after = await queries.playlist_members(raw_conn, playlist_id)
    positions = sorted(m["position"] for m in after)
    base = positions[0]
    assert positions == list(range(base, base + len(after))), (
        "positions must be a contiguous run with no duplicate after a move"
    )
    assert len(set(positions)) == len(positions)


async def test_removing_a_playlist_item_renumbers_the_remainder_contiguously(raw_conn):
    playlist_id = await queries.create_playlist(raw_conn, "Trimmable Playlist", "trimmable-playlist-slug")
    edition_ids = [uuid.uuid4() for _ in range(3)]
    for i, edition_id in enumerate(edition_ids):
        await queries.create_title_with_edition(
            raw_conn, f"Trimmable Member {i}", f"trimmable-member-{i}-slug", edition_id, f"{edition_id}.mp4"
        )
        await queries.add_playlist_item(raw_conn, playlist_id, edition_id)

    members = await queries.playlist_members(raw_conn, playlist_id)
    middle_item_id = members[1]["item_id"]

    await queries.remove_playlist_item(raw_conn, playlist_id, middle_item_id)

    after = await queries.playlist_members(raw_conn, playlist_id)
    positions = sorted(m["position"] for m in after)
    assert len(after) == 2
    assert positions == list(range(positions[0], positions[0] + 2))
    assert middle_item_id not in [m["item_id"] for m in after]


async def test_removing_a_playlist_item_from_the_wrong_playlist_changes_nothing(raw_conn):
    """T-03-18: an item identifier scoped to one playlist must not delete
    anything when presented against a different one.
    """
    playlist_a = await queries.create_playlist(raw_conn, "Playlist A", "playlist-a-slug")
    playlist_b = await queries.create_playlist(raw_conn, "Playlist B", "playlist-b-slug")
    edition_id = uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Cross Playlist Member", "cross-playlist-member-slug", edition_id, f"{edition_id}.mp4"
    )
    await queries.add_playlist_item(raw_conn, playlist_a, edition_id)
    members = await queries.playlist_members(raw_conn, playlist_a)
    item_id = members[0]["item_id"]

    with pytest.raises(LookupError):
        await queries.remove_playlist_item(raw_conn, playlist_b, item_id)

    still_there = await queries.playlist_members(raw_conn, playlist_a)
    assert [m["item_id"] for m in still_there] == [item_id]


async def test_a_member_whose_title_was_deleted_reports_itself_as_dropped(raw_conn):
    """The gap mark (D-102) is a column the query computes, not a status
    value the template is expected to recognise -- so the thing worth
    proving against real Postgres is that deleting a title actually flips
    it, and that the member keeps its slot rather than disappearing from
    the list.
    """
    playlist_id = await queries.create_playlist(raw_conn, "Evening With A Hole", "evening-with-a-hole")
    kept_edition, doomed_edition = uuid.uuid4(), uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Kept Clip", "kept-clip-slug", kept_edition, f"{kept_edition}.mp4"
    )
    doomed_title = await queries.create_title_with_edition(
        raw_conn, "Doomed Clip", "doomed-clip-slug", doomed_edition, f"{doomed_edition}.mp4"
    )
    await queries.add_playlist_item(raw_conn, playlist_id, kept_edition)
    await queries.add_playlist_item(raw_conn, playlist_id, doomed_edition)

    before = await queries.playlist_members(raw_conn, playlist_id)
    assert [m["dropped"] for m in before] == [False, False]

    await queries.finish_delete(raw_conn, doomed_title)

    after = await queries.playlist_members(raw_conn, playlist_id)
    assert len(after) == 2, "the member keeps its slot; it does not vanish"
    assert [m["dropped"] for m in after] == [False, True]
    assert [m["position"] for m in after] == [m["position"] for m in before]
    assert after[0]["title_name"] == "Kept Clip"


async def test_a_built_artifact_reads_as_referenced_from_the_real_query(raw_conn):
    """Against the real query over the migrated database, not a hand-built
    row: storage.reconcile reads only key/kind/title_name/slug and is
    entirely kind-agnostic, so a hand-built row carrying a playlist kind
    would pass today, before a line of this phase is written, and would
    keep passing if the union branch were never added. Assert the key is
    the full storage key with its extension, byte-identical to what a
    bucket listing holds.
    """
    playlist_id = await queries.create_playlist(
        raw_conn, "Referenced Artifact Playlist", "referenced-artifact-playlist-slug"
    )
    artifact_key = f"{uuid.uuid4()}.mp4"
    await raw_conn.execute(
        "INSERT INTO playlist_artifacts (id, playlist_id, object_key, superseded_at) "
        "VALUES ($1, $2, $3, NULL)",
        uuid.uuid4(), playlist_id, artifact_key,
    )

    rows = await queries.referenced_objects(raw_conn)

    match = next((r for r in rows if r["key"] == artifact_key), None)
    assert match is not None
    assert match["kind"] == "playlist"


async def test_a_superseded_artifact_stays_referenced_until_it_is_reclaimed(raw_conn):
    playlist_id = await queries.create_playlist(
        raw_conn, "Superseded Artifact Playlist", "superseded-artifact-playlist-slug"
    )
    current_key = f"{uuid.uuid4()}.mp4"
    superseded_key = f"{uuid.uuid4()}.mp4"
    await raw_conn.execute(
        "INSERT INTO playlist_artifacts (id, playlist_id, object_key, superseded_at) "
        "VALUES ($1, $2, $3, NULL)",
        uuid.uuid4(), playlist_id, current_key,
    )
    await raw_conn.execute(
        "INSERT INTO playlist_artifacts (id, playlist_id, object_key, superseded_at) "
        "VALUES ($1, $2, $3, now())",
        uuid.uuid4(), playlist_id, superseded_key,
    )

    keys = {r["key"] for r in await queries.referenced_objects(raw_conn)}

    assert current_key in keys
    assert superseded_key in keys, (
        "the superseded artifact is still in the bucket while an address "
        "handed out before the rebuild can still reach it -- an audit "
        "calling it garbage would be wrong"
    )


async def test_a_finished_playlist_deletion_leaves_nothing_referenced(raw_conn):
    playlist_id = await queries.create_playlist(
        raw_conn, "Finished Deletion Playlist", "finished-deletion-playlist-slug"
    )
    artifact_key = f"{uuid.uuid4()}.mp4"
    await raw_conn.execute(
        "INSERT INTO playlist_artifacts (id, playlist_id, object_key, superseded_at) "
        "VALUES ($1, $2, $3, NULL)",
        uuid.uuid4(), playlist_id, artifact_key,
    )
    await queries.mark_playlist_for_delete(raw_conn, playlist_id, grace_seconds=0)
    await queries.finish_delete_playlist(raw_conn, playlist_id)

    keys = {r["key"] for r in await queries.referenced_objects(raw_conn)}

    assert artifact_key not in keys, (
        "a completed deletion must not be reported forever as a broken address"
    )
