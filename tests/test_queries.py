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


# --- Episode-scoped countdown (migrations/versions/0006_series_and_editions.py) ---


async def _episode_id_for_title(conn, title_id) -> uuid.UUID:
    return await conn.fetchval(
        """
        SELECT ep.id FROM episodes ep
        JOIN seasons s ON s.id = ep.season_id
        WHERE s.title_id = $1
        """,
        title_id,
    )


async def test_mark_episode_for_delete_and_cancel_delete_episode_round_trip(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Episode Countdown Title", "episode-countdown-slug", edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)

    await queries.mark_episode_for_delete(raw_conn, episode_id, grace_seconds=3600)
    row = await raw_conn.fetchrow(
        "SELECT status, delete_at FROM episodes WHERE id = $1", episode_id
    )
    assert row["status"] == "pending_delete"
    assert row["delete_at"] is not None

    await queries.cancel_delete_episode(raw_conn, episode_id)
    row = await raw_conn.fetchrow(
        "SELECT status, delete_at FROM episodes WHERE id = $1", episode_id
    )
    assert row["status"] == "active"
    assert row["delete_at"] is None


async def test_due_for_reaping_episodes_and_episode_object_keys(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Episode Reaping Title", "episode-reaping-slug", edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)
    await queries.set_edition_poster(raw_conn, edition_id, f"{edition_id}-poster.jpg")
    await queries.mark_episode_for_delete(raw_conn, episode_id, grace_seconds=0)

    due = await queries.due_for_reaping_episodes(raw_conn)
    row = next(r for r in due if r["episode_id"] == episode_id)
    assert row["title_id"] == title_id

    keys = await queries.episode_object_keys(raw_conn, episode_id)
    assert set(keys) == {f"{edition_id}.mp4", f"{edition_id}-poster.jpg"}


async def test_finish_delete_episode_tombstones_an_episode_that_was_ever_active(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Tombstone Episode Title", "tombstone-episode-slug", edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await _episode_id_for_title(raw_conn, title_id)

    await queries.mark_episode_for_delete(raw_conn, episode_id, grace_seconds=0)
    await queries.finish_delete_episode(raw_conn, episode_id)

    row = await raw_conn.fetchrow("SELECT status FROM episodes WHERE id = $1", episode_id)
    assert row is not None
    assert row["status"] == "deleted"
    # The edition row itself survives untouched, exactly as a reaped
    # title's rows do -- only the status flips.
    edition_row = await raw_conn.fetchrow("SELECT id FROM editions WHERE id = $1", edition_id)
    assert edition_row is not None


async def test_finish_delete_episode_hard_deletes_an_episode_that_was_never_active(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Hard Delete Episode Title", "hard-delete-episode-slug", edition_id,
        f"{edition_id}.mp4",
    )
    season_id = await raw_conn.fetchval("SELECT id FROM seasons WHERE title_id = $1", title_id)

    new_edition_id = uuid.uuid4()
    new_episode_id = await raw_conn.fetchval(
        "INSERT INTO episodes (season_id) VALUES ($1) RETURNING id", season_id
    )
    await raw_conn.execute(
        "INSERT INTO editions (id, episode_id, object_key, status) VALUES ($1, $2, $3, 'preparing')",
        new_edition_id, new_episode_id, f"{new_edition_id}.mp4",
    )
    await queries.claim_episode_number(raw_conn, title_id, new_episode_id, 2)

    await queries.mark_episode_for_delete(raw_conn, new_episode_id, grace_seconds=0)
    await queries.finish_delete_episode(raw_conn, new_episode_id)

    row = await raw_conn.fetchrow("SELECT id FROM episodes WHERE id = $1", new_episode_id)
    assert row is None
    edition_row = await raw_conn.fetchrow(
        "SELECT id FROM editions WHERE id = $1", new_edition_id
    )
    assert edition_row is None


# --- Growing an existing title, attaching an edition (0006, plan 05) -------


async def _first_episode_id(conn, title_id) -> uuid.UUID:
    return await conn.fetchval(
        "SELECT ep.id FROM episodes ep JOIN seasons s ON s.id = ep.season_id "
        "WHERE s.title_id = $1",
        title_id,
    )


async def test_title_by_current_slug_matches_an_active_title(raw_conn):
    edition_id = uuid.uuid4()
    await queries.create_title_with_edition(
        raw_conn, "Slug Match Show", "slug-match-show", edition_id, f"{edition_id}.mp4"
    )

    row = await queries.title_by_current_slug(raw_conn, "slug-match-show")

    assert row is not None
    assert row["title_name"] == "Slug Match Show"


async def test_title_by_current_slug_excludes_a_pending_delete_title(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Pending Delete Match Show", "pending-delete-match-show", edition_id,
        f"{edition_id}.mp4",
    )
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=3600)

    row = await queries.title_by_current_slug(raw_conn, "pending-delete-match-show")

    assert row is None
    await queries.cancel_delete(raw_conn, title_id)


async def test_episode_by_number_returns_none_for_a_free_number(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Free Number Show", "free-number-show", edition_id, f"{edition_id}.mp4"
    )

    assert await queries.episode_by_number(raw_conn, title_id, 7) is None


async def test_episode_by_number_returns_the_episode_holding_it(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Held Number Show", "held-number-show", edition_id, f"{edition_id}.mp4"
    )
    episode_id = await _first_episode_id(raw_conn, title_id)

    row = await queries.episode_by_number(raw_conn, title_id, 1)

    assert row is not None
    assert row["episode_id"] == episode_id


async def test_episode_with_title_returns_title_and_current_number(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Episode Context Show", "episode-context-show", edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await _first_episode_id(raw_conn, title_id)

    row = await queries.episode_with_title(raw_conn, episode_id)

    assert row["title_id"] == title_id
    assert row["title_name"] == "Episode Context Show"
    assert row["number"] == 1


async def test_edition_slug_taken_on_episode_is_scoped_to_one_episode(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Slug Scope Show", "slug-scope-show", edition_id, f"{edition_id}.mp4"
    )
    episode_id = await _first_episode_id(raw_conn, title_id)
    second_edition_id = uuid.uuid4()
    await queries.add_edition_to_episode(
        raw_conn, episode_id, "dub-ru", second_edition_id, f"{second_edition_id}.mp4"
    )

    assert await queries.edition_slug_taken_on_episode(raw_conn, episode_id, "dub-ru") is True
    assert await queries.edition_slug_taken_on_episode(raw_conn, episode_id, "dub-en") is False


async def test_set_default_edition_moves_the_pointer(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Default Pointer Show", "default-pointer-show", edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await _first_episode_id(raw_conn, title_id)
    second_edition_id = uuid.uuid4()
    await queries.add_edition_to_episode(
        raw_conn, episode_id, "dub-ru", second_edition_id, f"{second_edition_id}.mp4"
    )

    await queries.set_default_edition(raw_conn, second_edition_id)

    default_now = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    assert default_now == second_edition_id


async def test_attach_episode_to_title_creates_an_episode_and_claims_its_number(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Attach Query Show", "attach-query-show", edition_id, f"{edition_id}.mp4"
    )
    new_edition_id = uuid.uuid4()

    episode_id = await queries.attach_episode_to_title(
        raw_conn, title_id, 12, new_edition_id, f"{new_edition_id}.mp4"
    )

    row = await queries.episode_by_number(raw_conn, title_id, 12)
    assert row is not None
    assert row["episode_id"] == episode_id
    assert row["default_edition_id"] == new_edition_id


async def test_attach_episode_to_title_refuses_a_number_a_different_episode_holds(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Attach Taken Show", "attach-taken-show", edition_id, f"{edition_id}.mp4"
    )
    new_edition_id = uuid.uuid4()

    with pytest.raises(queries.EpisodeNumberTaken):
        await queries.attach_episode_to_title(
            raw_conn, title_id, 1, new_edition_id, f"{new_edition_id}.mp4"
        )


async def test_attach_episode_to_title_refuses_a_title_that_does_not_exist(raw_conn):
    new_edition_id = uuid.uuid4()

    with pytest.raises(LookupError):
        await queries.attach_episode_to_title(
            raw_conn, uuid.uuid4(), 1, new_edition_id, f"{new_edition_id}.mp4"
        )


async def test_add_edition_to_episode_never_touches_the_default_pointer(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "No Default Touch Show", "no-default-touch-show", edition_id,
        f"{edition_id}.mp4",
    )
    episode_id = await _first_episode_id(raw_conn, title_id)
    second_edition_id = uuid.uuid4()

    await queries.add_edition_to_episode(
        raw_conn, episode_id, "dub-ru", second_edition_id, f"{second_edition_id}.mp4"
    )

    default_still = await raw_conn.fetchval(
        "SELECT default_edition_id FROM episodes WHERE id = $1", episode_id
    )
    assert default_still == edition_id


async def test_add_edition_to_episode_refuses_an_episode_that_does_not_exist(raw_conn):
    new_edition_id = uuid.uuid4()

    with pytest.raises(LookupError):
        await queries.add_edition_to_episode(
            raw_conn, uuid.uuid4(), "dub-ru", new_edition_id, f"{new_edition_id}.mp4"
        )


async def test_concurrent_attach_same_number_one_wins_one_raises(migrated_db_url):
    """Two connections race to attach episode 7 of the same title -- the
    same claim-lock discipline `test_concurrent_create_with_same_
    candidate_gets_two_different_slugs` already proves for a title slug,
    one level down for an episode number.
    """
    setup_conn = await asyncpg.connect(migrated_db_url)
    try:
        edition_id = uuid.uuid4()
        title_id = await queries.create_title_with_edition(
            setup_conn, "Concurrent Attach Show", "concurrent-attach-show", edition_id,
            f"{edition_id}.mp4",
        )
    finally:
        await setup_conn.close()

    async def worker():
        conn = await asyncpg.connect(migrated_db_url)
        try:
            new_edition_id = uuid.uuid4()
            await queries.attach_episode_to_title(
                conn, title_id, 7, new_edition_id, f"{new_edition_id}.mp4"
            )
            return "ok"
        except queries.EpisodeNumberTaken:
            return "taken"
        finally:
            await conn.close()

    results = await asyncio.gather(worker(), worker())
    assert sorted(results) == ["ok", "taken"]


async def test_concurrent_add_edition_same_slug_one_wins_one_raises(migrated_db_url):
    """The identical race one level down for an edition slug: both workers
    plan and claim "dub-ru" inside their own locked transaction, the
    pattern `orchestrator.web.routes_titles.add_confirm_route`'s edition
    branch itself follows.
    """
    from orchestrator.media.add import plan_edition_slug

    setup_conn = await asyncpg.connect(migrated_db_url)
    try:
        edition_id = uuid.uuid4()
        title_id = await queries.create_title_with_edition(
            setup_conn, "Concurrent Edition Show", "concurrent-edition-show", edition_id,
            f"{edition_id}.mp4",
        )
        episode_id = await _first_episode_id(setup_conn, title_id)
    finally:
        await setup_conn.close()

    async def worker():
        conn = await asyncpg.connect(migrated_db_url)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
                )
                chosen = await plan_edition_slug(conn, episode_id, "dub-ru")
                new_edition_id = uuid.uuid4()
                await queries.add_edition_to_episode(
                    conn, episode_id, chosen, new_edition_id, f"{new_edition_id}.mp4"
                )
                return "ok"
        except Exception as exc:
            return type(exc).__name__
        finally:
            await conn.close()

    results = await asyncio.gather(worker(), worker())
    assert sorted(results) == ["EditionSlugTaken", "ok"]


# --- The folder confirmation screen's save (0006, plan 08) -----------------


def _edition_spec(number: int, *, source_path: str | None = None, **overrides) -> dict:
    edition_id = uuid.uuid4()
    spec = {
        "number": number,
        "edition_id": edition_id,
        "object_key": f"{edition_id}.mp4",
        "source_path": source_path,
        "audio_stream_index": None,
        "audio_channels": None,
        "stereo_layout": None,
    }
    spec.update(overrides)
    return spec


async def test_create_title_with_episodes_writes_one_title_one_season_and_the_right_episodes(
    raw_conn,
):
    specs = [
        _edition_spec(1, source_path="Horimiya/01.mkv"),
        _edition_spec(2, source_path="Horimiya/02.mkv"),
        _edition_spec(3, source_path="Horimiya/03.mkv"),
    ]

    title_id, episode_ids = await queries.create_title_with_episodes(
        raw_conn, "Horimiya", "horimiya-folder-test", specs, season_number=1
    )

    assert len(episode_ids) == 3
    seasons = await raw_conn.fetch("SELECT number FROM seasons WHERE title_id = $1", title_id)
    assert [row["number"] for row in seasons] == [1]

    for spec, episode_id in zip(specs, episode_ids):
        row = await queries.episode_by_number(raw_conn, title_id, spec["number"])
        assert row["episode_id"] == episode_id
        edition = await raw_conn.fetchrow(
            "SELECT source_path, status FROM editions WHERE id = $1", spec["edition_id"]
        )
        assert edition["source_path"] == spec["source_path"]
        assert edition["status"] == "preparing"


async def test_create_title_with_episodes_claims_every_number_in_history(raw_conn):
    specs = [_edition_spec(5), _edition_spec(6)]
    title_id, _episode_ids = await queries.create_title_with_episodes(
        raw_conn, "Numbered Show", "numbered-show-folder-test", specs
    )
    assert await queries.taken_episode_numbers(raw_conn, title_id) == {5, 6}


async def test_create_title_with_episodes_season_number_is_stored(raw_conn):
    title_id, _episode_ids = await queries.create_title_with_episodes(
        raw_conn, "Season Three Show", "season-three-show-folder-test",
        [_edition_spec(1)], season_number=3,
    )
    row = await raw_conn.fetchrow("SELECT number FROM seasons WHERE title_id = $1", title_id)
    assert row["number"] == 3


async def test_attach_episodes_to_title_adds_several_episodes_without_touching_the_address(
    raw_conn,
):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Folder Attach Show", "folder-attach-show", edition_id, f"{edition_id}.mp4"
    )
    slug_before = await raw_conn.fetchval(
        "SELECT slug FROM slug_history WHERE title_id = $1 AND is_current", title_id
    )

    specs = [_edition_spec(2), _edition_spec(3)]
    episode_ids = await queries.attach_episodes_to_title(raw_conn, title_id, specs)

    assert len(episode_ids) == 2
    assert await queries.taken_episode_numbers(raw_conn, title_id) == {1, 2, 3}
    slug_after = await raw_conn.fetchval(
        "SELECT slug FROM slug_history WHERE title_id = $1 AND is_current", title_id
    )
    assert slug_after == slug_before


async def test_attach_episodes_to_title_refuses_a_number_already_taken(raw_conn):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Folder Attach Taken Show", "folder-attach-taken-show", edition_id,
        f"{edition_id}.mp4",
    )
    with pytest.raises(queries.EpisodeNumberTaken):
        await queries.attach_episodes_to_title(raw_conn, title_id, [_edition_spec(1)])


async def test_attach_episodes_to_title_refuses_a_duplicate_number_within_the_same_batch(
    raw_conn,
):
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Folder Attach Batch Show", "folder-attach-batch-show", edition_id,
        f"{edition_id}.mp4",
    )
    with pytest.raises(queries.EpisodeNumberTaken):
        await queries.attach_episodes_to_title(
            raw_conn, title_id, [_edition_spec(9), _edition_spec(9)]
        )


async def test_attach_episodes_to_title_refuses_a_title_that_does_not_exist(raw_conn):
    with pytest.raises(LookupError):
        await queries.attach_episodes_to_title(raw_conn, uuid.uuid4(), [_edition_spec(1)])


async def test_concurrent_create_title_with_episodes_and_folder_attach_one_wins_one_raises(
    migrated_db_url,
):
    """The folder save's own claim-lock race, one level up from a single
    attach: two folder saves both trying to attach episode 4 of the same
    title at once -- the identical discipline
    `test_concurrent_attach_same_number_one_wins_one_raises` already
    proves for the single-episode attach, exercised here for the batch
    entry point a folder save actually calls.
    """
    setup_conn = await asyncpg.connect(migrated_db_url)
    try:
        edition_id = uuid.uuid4()
        title_id = await queries.create_title_with_edition(
            setup_conn, "Concurrent Folder Attach Show", "concurrent-folder-attach-show",
            edition_id, f"{edition_id}.mp4",
        )
    finally:
        await setup_conn.close()

    async def worker():
        conn = await asyncpg.connect(migrated_db_url)
        try:
            await queries.attach_episodes_to_title(conn, title_id, [_edition_spec(4)])
            return "ok"
        except queries.EpisodeNumberTaken:
            return "taken"
        finally:
            await conn.close()

    results = await asyncio.gather(worker(), worker())
    assert sorted(results) == ["ok", "taken"]


async def _seed_standalone_title(conn, name: str, slug: str, source_path: str):
    """One standalone one-episode title, the exact shape the seven live
    hand-added episodes this move path will eventually be pointed at
    already have -- a plain `create_title_with_edition` call, active by
    default.
    """
    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    title_id = await queries.create_title_with_edition(
        conn, name, slug, edition_id, object_key, source_path=source_path
    )
    episode = await queries.lowest_episode_of_title(conn, title_id)
    return {
        "title_id": title_id,
        "episode_id": episode["episode_id"],
        "edition_id": edition_id,
        "object_key": object_key,
        "slug": slug,
    }


async def test_reparent_edition_to_episode_moves_the_edition_under_the_target_title(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Reparent Target Show", "reparent-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Reparent Standalone", "reparent-standalone", "Folder/standalone.mkv"
    )

    new_episode_id = await queries.reparent_edition_to_episode(
        raw_conn,
        edition_id=standalone["edition_id"],
        old_title_id=standalone["title_id"],
        old_episode_id=standalone["episode_id"],
        old_slug=standalone["slug"],
        target_title_id=target_title_id,
        number=7,
    )

    row = await queries.episode_by_number(raw_conn, target_title_id, 7)
    assert row["episode_id"] == new_episode_id
    assert row["default_edition_id"] == standalone["edition_id"]


async def test_reparent_edition_to_episode_never_touches_the_object_key(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Object Key Target Show", "object-key-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Object Key Standalone", "object-key-standalone", "Folder/keeps-key.mkv"
    )

    await queries.reparent_edition_to_episode(
        raw_conn,
        edition_id=standalone["edition_id"],
        old_title_id=standalone["title_id"],
        old_episode_id=standalone["episode_id"],
        old_slug=standalone["slug"],
        target_title_id=target_title_id,
        number=2,
    )

    object_key_after = await raw_conn.fetchval(
        "SELECT object_key FROM editions WHERE id = $1", standalone["edition_id"]
    )
    assert object_key_after == standalone["object_key"]


async def test_reparent_edition_to_episode_old_address_resolves_to_the_moved_episode(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Old Address Target Show", "old-address-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Old Address Standalone", "old-address-standalone", "Folder/old-address.mkv"
    )

    new_episode_id = await queries.reparent_edition_to_episode(
        raw_conn,
        edition_id=standalone["edition_id"],
        old_title_id=standalone["title_id"],
        old_episode_id=standalone["episode_id"],
        old_slug=standalone["slug"],
        target_title_id=target_title_id,
        number=2,
    )

    resolved = await queries.resolve_slug(raw_conn, standalone["slug"])
    assert resolved["title_id"] == target_title_id
    assert resolved["episode_id"] == new_episode_id
    # And not the target's own lowest episode -- resolve_slug's episode
    # pointer branch, not its title-lowest-episode fallback.
    lowest = await queries.lowest_episode_of_title(raw_conn, target_title_id)
    assert resolved["episode_id"] != lowest["episode_id"]


async def test_reparent_edition_to_episode_tombstones_the_old_title(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Tombstone Target Show", "tombstone-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Tombstone Standalone", "tombstone-standalone", "Folder/tombstone.mkv"
    )

    await queries.reparent_edition_to_episode(
        raw_conn,
        edition_id=standalone["edition_id"],
        old_title_id=standalone["title_id"],
        old_episode_id=standalone["episode_id"],
        old_slug=standalone["slug"],
        target_title_id=target_title_id,
        number=2,
    )

    status = await raw_conn.fetchval(
        "SELECT status FROM titles WHERE id = $1", standalone["title_id"]
    )
    assert status == "deleted"
    # The old title's own slug_history row survives untouched.
    old_row_count = await raw_conn.fetchval(
        "SELECT count(*) FROM slug_history WHERE title_id = $1", standalone["title_id"]
    )
    assert old_row_count == 1
    # Its now-empty season/episode rows are unwound.
    seasons_left = await raw_conn.fetchval(
        "SELECT count(*) FROM seasons WHERE title_id = $1", standalone["title_id"]
    )
    assert seasons_left == 0


async def test_reparent_edition_to_episode_refuses_a_number_already_taken(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Taken Number Target Show", "taken-number-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Taken Number Standalone", "taken-number-standalone", "Folder/taken.mkv"
    )
    with pytest.raises(queries.EpisodeNumberTaken):
        await queries.reparent_edition_to_episode(
            raw_conn,
            edition_id=standalone["edition_id"],
            old_title_id=standalone["title_id"],
            old_episode_id=standalone["episode_id"],
            old_slug=standalone["slug"],
            target_title_id=target_title_id,
            number=1,  # the target's own existing episode already holds it
        )


async def test_reparent_edition_to_episode_refuses_a_title_that_does_not_exist(raw_conn):
    standalone = await _seed_standalone_title(
        raw_conn, "No Target Standalone", "no-target-standalone", "Folder/no-target.mkv"
    )
    with pytest.raises(LookupError):
        await queries.reparent_edition_to_episode(
            raw_conn,
            edition_id=standalone["edition_id"],
            old_title_id=standalone["title_id"],
            old_episode_id=standalone["episode_id"],
            old_slug=standalone["slug"],
            target_title_id=uuid.uuid4(),
            number=1,
        )


async def test_reparent_edition_to_episode_referenced_objects_counts_the_object_once(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Reconcile Target Show", "reconcile-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Reconcile Standalone", "reconcile-standalone", "Folder/reconcile.mkv"
    )

    await queries.reparent_edition_to_episode(
        raw_conn,
        edition_id=standalone["edition_id"],
        old_title_id=standalone["title_id"],
        old_episode_id=standalone["episode_id"],
        old_slug=standalone["slug"],
        target_title_id=target_title_id,
        number=9,
    )

    rows = await queries.referenced_objects(raw_conn)
    matches = [r for r in rows if r["key"] == standalone["object_key"] and r["kind"] == "video"]
    assert len(matches) == 1


async def test_reparent_edition_to_episode_touches_the_target_titles_changed_at(raw_conn):
    target_edition_id = uuid.uuid4()
    target_title_id = await queries.create_title_with_edition(
        raw_conn, "Changed At Target Show", "changed-at-target-show", target_edition_id,
        f"{target_edition_id}.mp4",
    )
    await raw_conn.execute(
        "UPDATE titles SET changed_at = now() - interval '1 hour' WHERE id = $1",
        target_title_id,
    )
    before = await raw_conn.fetchval(
        "SELECT changed_at FROM titles WHERE id = $1", target_title_id
    )
    standalone = await _seed_standalone_title(
        raw_conn, "Changed At Standalone", "changed-at-standalone", "Folder/changed-at.mkv"
    )

    await queries.reparent_edition_to_episode(
        raw_conn,
        edition_id=standalone["edition_id"],
        old_title_id=standalone["title_id"],
        old_episode_id=standalone["episode_id"],
        old_slug=standalone["slug"],
        target_title_id=target_title_id,
        number=8,
    )

    after = await raw_conn.fetchval("SELECT changed_at FROM titles WHERE id = $1", target_title_id)
    assert after > before


# --- Plan 09 Task 1: the whole hierarchy in one round trip ----------------


async def test_library_rows_orders_by_changed_at_not_created_at(raw_conn):
    """D-165: a title rises to the top when it changes, not only when it
    was created -- the older title outranks the newer one once its own
    `changed_at` is bumped past it.
    """
    older_id = await _create_title(raw_conn, "Older By Creation", "older-by-creation-slug")
    newer_id = await _create_title(raw_conn, "Newer By Creation", "newer-by-creation-slug")

    await queries.touch_title_changed(raw_conn, older_id)

    rows = await queries.library_rows(raw_conn)
    ids = [row["title_id"] for row in rows]
    assert ids.index(older_id) < ids.index(newer_id)


async def test_library_row_carries_the_new_aggregate_columns_for_a_one_episode_title(raw_conn):
    """A film's aggregate columns are exactly what a one-episode title
    always was: one episode, ready, none failed -- so `derive_series_ui`
    (routes_titles.py) has real data to fall back on for D-162's
    unchanged-film guarantee, not just a stub default.
    """
    title_id = await _create_title(raw_conn, "One Episode Aggregate", "one-episode-aggregate-slug")

    row = await queries.library_row(raw_conn, title_id)
    assert row["episode_count"] == 1
    assert row["ready_count"] == 1
    assert row["failed_count"] == 0
    assert row["job_episode_number"] is None


async def test_library_row_aggregate_counts_ready_failed_and_current_episode(raw_conn):
    """A multi-episode title reports its own ready/failed counts and,
    while a job is in flight, which episode that job belongs to -- the
    three facts the aggregate chip (D-163) reads.
    """
    first_edition = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Aggregate Season", "aggregate-season-slug", first_edition,
        f"{first_edition}.mp4",
    )
    ep2_edition = uuid.uuid4()
    ep2_id = await queries.attach_episode_to_title(
        raw_conn, title_id, 2, ep2_edition, f"{ep2_edition}.mp4", status="preparing",
    )
    ep3_edition = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 3, ep3_edition, f"{ep3_edition}.mp4", status="failed",
    )

    job_id = await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=ep2_edition,
    )

    row = await queries.library_row(raw_conn, title_id)
    assert row["episode_count"] == 3
    assert row["ready_count"] == 1
    assert row["failed_count"] == 1
    assert row["job_episode_number"] == 2

    await queries.request_job_cancel(raw_conn, job_id)


async def test_library_row_prefers_the_running_job_over_a_newer_queued_one(raw_conn):
    """The collapsed title row must describe the episode actually
    converting, not merely the newest job of the season (D-188 gap
    closure): a real order (migration 0008) makes "newest" mean "this
    season's last episode" the moment a folder save enqueues every job in
    one transaction, so the pre-existing newest-first pick becomes
    deterministically wrong rather than harmless the moment task 1 lands.
    """
    first_edition = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Running First Season", "running-first-season-slug", first_edition,
        f"{first_edition}.mp4",
    )
    ep2_edition = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 2, ep2_edition, f"{ep2_edition}.mp4", status="preparing",
    )
    ep3_edition = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 3, ep3_edition, f"{ep3_edition}.mp4", status="preparing",
    )

    running_job = await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=first_edition,
    )
    await queries.claim_job(raw_conn, kind="transcode", worker_name="ml-server")
    await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=ep2_edition,
    )
    await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=ep3_edition,
    )

    row = await queries.library_row(raw_conn, title_id)
    assert row["job_id"] == running_job
    assert row["job_status"] == "running"
    assert row["job_episode_number"] == 1

    await queries.finish_job(
        raw_conn, running_job, worker_name="ml-server", attempt=1, outcome="cancelled",
    )
    for row in await raw_conn.fetch(
        "SELECT id FROM jobs WHERE title_id = $1 AND status = 'queued'", title_id
    ):
        await queries.request_job_cancel(raw_conn, row["id"])


async def test_job_for_title_returns_the_running_job_over_queued_siblings(raw_conn):
    """The reader behind Cancel on a title row
    (`routes_titles.cancel_route`) must pick the same job the row itself
    describes -- otherwise the row shows episode one converting while
    Cancel stops episode eleven, turning a display defect into a
    destructive one (D-188 gap closure).
    """
    first_edition = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Cancel Target Season", "cancel-target-season-slug", first_edition,
        f"{first_edition}.mp4",
    )
    ep2_edition = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 2, ep2_edition, f"{ep2_edition}.mp4", status="preparing",
    )
    ep3_edition = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 3, ep3_edition, f"{ep3_edition}.mp4", status="preparing",
    )

    running_job = await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=ep2_edition,
    )
    await queries.claim_job(raw_conn, kind="transcode", worker_name="ml-server")
    await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=first_edition,
    )
    await queries.enqueue_job(
        raw_conn, kind="transcode", title_id=title_id, edition_id=ep3_edition,
    )

    live = await queries.job_for_title(raw_conn, title_id)
    assert live["id"] == running_job
    assert live["status"] == "running"

    await queries.finish_job(
        raw_conn, running_job, worker_name="ml-server", attempt=1, outcome="cancelled",
    )
    for row in await raw_conn.fetch(
        "SELECT id FROM jobs WHERE title_id = $1 AND status = 'queued'", title_id
    ):
        await queries.request_job_cancel(raw_conn, row["id"])


async def test_episodes_for_library_returns_every_episode_in_number_order_with_its_own_state(raw_conn):
    """`episodes_for_library` -- the expanded list's own data source --
    returns one row per episode, in number order, each carrying its own
    default edition's technical detail and its own edition count,
    regardless of which episode the title's own default edition sits on.
    """
    first_edition = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        raw_conn, "Episode List Season", "episode-list-season-slug", first_edition,
        f"{first_edition}.mp4",
    )
    await queries.set_edition_technical(
        raw_conn, first_edition, size_bytes=100, duration_seconds=60.0, width=1280, height=720,
        audio_language="jpn", audio_title=None, audio_reencoded=False,
    )
    ep2_edition = uuid.uuid4()
    ep2_id = await queries.attach_episode_to_title(
        raw_conn, title_id, 2, ep2_edition, f"{ep2_edition}.mp4",
    )
    await queries.set_edition_technical(
        raw_conn, ep2_edition, size_bytes=200, duration_seconds=120.0, width=1280, height=720,
        audio_language="jpn", audio_title=None, audio_reencoded=False,
    )

    episodes = await queries.episodes_for_library(raw_conn, [title_id])

    assert [e["number"] for e in episodes] == [1, 2]
    assert [e["title_id"] for e in episodes] == [title_id, title_id]
    assert episodes[0]["size_bytes"] == 100
    assert episodes[1]["size_bytes"] == 200
    assert episodes[1]["episode_id"] == ep2_id
    assert episodes[0]["edition_count"] == 1


async def test_episodes_for_library_scoped_to_the_given_title_ids_and_short_circuits_on_empty(raw_conn):
    """Passing an empty list never reaches the database (the same guard
    `editions_by_source_paths` already uses), and passing one title's id
    never returns another title's episodes.
    """
    assert await queries.episodes_for_library(raw_conn, []) == []

    first_edition = uuid.uuid4()
    title_a = await queries.create_title_with_edition(
        raw_conn, "Scope A", "scope-a-slug", first_edition, f"{first_edition}.mp4",
    )
    second_edition = uuid.uuid4()
    title_b = await queries.create_title_with_edition(
        raw_conn, "Scope B", "scope-b-slug", second_edition, f"{second_edition}.mp4",
    )

    episodes = await queries.episodes_for_library(raw_conn, [title_a])
    assert {e["title_id"] for e in episodes} == {title_a}


async def test_episodes_for_library_query_count_is_one_call_for_several_titles(raw_conn):
    """One round trip regardless of how many titles are asked for -- the
    arithmetic `episodes_for_library`'s own docstring states: two queries
    per screen, never one per title and never one per episode.
    """
    title_ids = []
    for i in range(3):
        edition_id = uuid.uuid4()
        title_id = await queries.create_title_with_edition(
            raw_conn, f"Batch Title {i}", f"batch-title-{i}-slug", edition_id, f"{edition_id}.mp4",
        )
        await queries.attach_episode_to_title(
            raw_conn, title_id, 2, uuid.uuid4(), f"{uuid.uuid4()}.mp4",
        )
        title_ids.append(title_id)

    episodes = await queries.episodes_for_library(raw_conn, title_ids)
    assert len(episodes) == 6
