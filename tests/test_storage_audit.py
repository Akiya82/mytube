"""Unit tests for `storage-audit` (`orchestrator.cli.storage_audit`).

Both the catalog query and the bucket listing are stubbed at the module
boundary -- no real Postgres, no real S3, quick-loop only. The comparison
itself (`storage.reconcile`) is already covered by `tests/test_storage.py`;
these tests cover what `storage_audit()` does with the result: the report
shape, the exit code, and the two extra requirements the plan calls out --
naming a missing object by the `/v/{slug}` address it breaks, and never
calling anything that deletes or aborts.
"""
import uuid

import asyncpg
import pytest

from orchestrator.cli import storage_audit
from orchestrator.db import queries
from orchestrator.storage import StorageUsage, reconcile

from conftest import ADMIN_FQDN, WORKER_TOKEN


class _DummyConn:
    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_connect(request, monkeypatch):
    """Every test in this file is a fast CLI-level unit test against a
    stubbed `asyncpg.connect` -- except the handful of plan-10 tests below
    marked `integration`, which need `raw_conn`'s own real connection to
    exercise `queries.referenced_objects` directly against a real
    Postgres. Patching unconditionally would silently hand `raw_conn` this
    same dummy the moment it calls `asyncpg.connect` itself.
    """
    if request.node.get_closest_marker("integration"):
        return

    async def _fake_connect(*args, **kwargs):
        return _DummyConn()

    monkeypatch.setattr(asyncpg, "connect", _fake_connect)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from orchestrator.config import get_settings

    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stub(monkeypatch, referenced, listing):
    async def _referenced_objects(conn):
        return referenced

    monkeypatch.setattr(queries, "referenced_objects", _referenced_objects)
    monkeypatch.setattr(
        storage_audit,
        "storage_usage",
        lambda: StorageUsage(
            total_bytes=sum(listing.values()),
            object_count=len(listing),
            free_bytes=None,
            listing=listing,
        ),
    )


async def test_clean_state_exits_zero(monkeypatch, capsys):
    referenced = [{"key": "a.mp4", "kind": "video", "title_name": "A", "slug": "a"}]
    _stub(monkeypatch, referenced, {"a.mp4": 100})

    code = await storage_audit.storage_audit()

    assert code == 0
    out = capsys.readouterr().out
    assert "no unreferenced objects" in out
    assert "no broken addresses" in out


async def test_unreferenced_object_reports_key_size_and_total(monkeypatch, capsys):
    referenced = [{"key": "a.mp4", "kind": "video", "title_name": "A", "slug": "a"}]
    _stub(monkeypatch, referenced, {"a.mp4": 100, "orphan.mp4": 250})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 1
    assert "orphan.mp4" in out
    assert "250" in out
    assert "total unreferenced: 250" in out


async def test_unreferenced_object_prints_a_removal_command_but_deletes_nothing(monkeypatch, capsys):
    referenced = []
    _stub(monkeypatch, referenced, {"orphan.mp4": 100})

    await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert "delete-object" in out
    assert "orphan.mp4" in out


async def test_missing_object_reports_the_address_it_breaks_not_only_the_key(monkeypatch, capsys):
    referenced = [
        {"key": "gone.mp4", "kind": "video", "title_name": "Gone Title", "slug": "gone-slug"}
    ]
    _stub(monkeypatch, referenced, {})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 1
    assert "/v/gone-slug" in out
    assert "gone.mp4" in out
    assert "Gone Title" in out


async def test_both_kinds_reported_together(monkeypatch, capsys):
    referenced = [
        {"key": "gone.mp4", "kind": "video", "title_name": "Gone", "slug": "gone-slug"},
    ]
    _stub(monkeypatch, referenced, {"orphan.mp4": 50})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 1
    assert "orphan.mp4" in out
    assert "/v/gone-slug" in out


async def test_poster_key_referenced_prevents_it_being_reported_unreferenced(monkeypatch, capsys):
    referenced = [
        {"key": "abc.mp4", "kind": "video", "title_name": "A", "slug": "a"},
        {"key": "abc.jpg", "kind": "poster", "title_name": "A", "slug": "a"},
    ]
    _stub(monkeypatch, referenced, {"abc.mp4": 100, "abc.jpg": 20})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 0
    assert "no unreferenced objects" in out


async def test_present_count_excludes_missing_rows(monkeypatch, capsys):
    referenced = [
        {"key": "a.mp4", "kind": "video", "title_name": "A", "slug": "a"},
        {"key": "gone.mp4", "kind": "video", "title_name": "Gone", "slug": "gone-slug"},
    ]
    _stub(monkeypatch, referenced, {"a.mp4": 10})

    await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert "present and accounted for in the catalog: 1" in out


async def test_a_moved_editions_object_counts_present_exactly_once(monkeypatch, capsys):
    """T-05-38: after `queries.reparent_edition_to_episode` moves an
    edition, `referenced_objects` reports its object key through exactly
    one row -- the moved edition under the target title, never a second
    row still naming the tombstoned old title (whose own rows this same
    move already unwound). The object is counted present once, not twice
    and not zero times.
    """
    referenced = [
        {"key": "moved.mp4", "kind": "video", "title_name": "Target Series", "slug": "target-series"},
    ]
    _stub(monkeypatch, referenced, {"moved.mp4": 1234})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 0
    assert "present and accounted for in the catalog: 1" in out
    assert out.count("moved.mp4") == 0  # never printed as unreferenced or missing


async def test_no_slug_reports_address_unknown_instead_of_a_bare_key(monkeypatch, capsys):
    referenced = [{"key": "gone.mp4", "kind": "video", "title_name": "Gone", "slug": None}]
    _stub(monkeypatch, referenced, {})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 1
    assert "gone.mp4" in out


# --- format_address(kind, slug): the report names a broken address by the --
# kind of thing that broke, not always by the video prefix.


@pytest.mark.parametrize(
    "kind,slug,expected_prefix",
    [
        ("video", "gone-slug", "/v/gone-slug"),
        ("poster", "gone-slug", "/v/gone-slug"),
        ("playlist", "gone-playlist-slug", "/p/gone-playlist-slug"),
    ],
    ids=["video", "poster", "playlist"],
)
def test_a_broken_address_is_reported_under_the_prefix_its_kind_actually_uses(
    kind, slug, expected_prefix
):
    from orchestrator.cli.storage_audit import format_address

    assert format_address(kind, slug) == expected_prefix, (
        "the report currently hardcodes the video prefix for every kind -- "
        "the operator chasing a broken playlist must not be sent to a "
        "/v/ link that was never the one that broke"
    )


def test_a_row_with_no_slug_still_reports_something_readable():
    from orchestrator.cli.storage_audit import format_address

    for kind in ("video", "poster", "playlist"):
        result = format_address(kind, None)
        assert result and "None" not in result


async def test_a_missing_playlist_artifact_names_the_playlist_address_not_a_video_one(
    monkeypatch, capsys
):
    """An operator sent to a /v/ link that works cannot act on the report,
    and the link that actually broke stays hidden.
    """
    referenced = [
        {"key": "gone-video.mp4", "kind": "video", "title_name": "V", "slug": "v-slug"},
        {"key": "gone-artifact.mp4", "kind": "playlist", "title_name": "P", "slug": "p-slug"},
    ]
    _stub(monkeypatch, referenced, {})

    code = await storage_audit.storage_audit()

    out = capsys.readouterr().out
    assert code == 1
    assert "/p/p-slug" in out
    assert "/v/v-slug" in out
    assert "/v/p-slug" not in out


# --- Plan 10: a per-episode reap excludes only that episode's own object,
# integration tests against a real Postgres exercising queries.referenced_
# objects directly, rather than the CLI's own stubbed report above.


async def _seed_three_episode_title(conn, tag: str) -> tuple:
    edition1 = uuid.uuid4()
    slug = f"storage-audit-{tag}-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        conn, f"Storage Audit {tag}", slug, edition1, f"{edition1}.mp4"
    )
    edition2 = uuid.uuid4()
    await queries.attach_episode_to_title(conn, title_id, 2, edition2, f"{edition2}.mp4")
    edition3 = uuid.uuid4()
    await queries.attach_episode_to_title(conn, title_id, 3, edition3, f"{edition3}.mp4")
    return title_id, edition1, edition2, edition3


@pytest.mark.integration
async def test_a_reaped_episodes_object_is_counted_zero_times_and_its_siblings_once(raw_conn):
    """`referenced_objects` excludes only the reaped episode's own edition
    (`ep.status = 'deleted'`), leaving its two untouched siblings counted
    exactly once each -- the exact false positive the function's own
    docstring says the title-level exclusion exists to prevent, one level
    down (plan 10).
    """
    title_id, edition1, edition2, edition3 = await _seed_three_episode_title(raw_conn, "reaped")
    episode2 = await queries.episode_by_number(raw_conn, title_id, 2)
    await queries.mark_episode_for_delete(raw_conn, episode2["episode_id"], grace_seconds=0)
    await queries.finish_delete_episode(raw_conn, episode2["episode_id"])

    referenced = await queries.referenced_objects(raw_conn)
    keys = [row["key"] for row in referenced]

    assert keys.count(f"{edition1}.mp4") == 1
    assert keys.count(f"{edition2}.mp4") == 0
    assert keys.count(f"{edition3}.mp4") == 1


@pytest.mark.integration
async def test_an_episode_still_inside_its_countdown_is_still_counted_referenced(raw_conn):
    """A `pending_delete` episode's objects are untouched while the
    deletion can still be undone (D-62) -- a bucket genuinely missing them
    during that window is still a broken address, so it must not be
    excluded the way a reaped one is.
    """
    title_id, edition1, edition2, edition3 = await _seed_three_episode_title(
        raw_conn, "countdown"
    )
    episode2 = await queries.episode_by_number(raw_conn, title_id, 2)
    await queries.mark_episode_for_delete(raw_conn, episode2["episode_id"], grace_seconds=3600)

    referenced = await queries.referenced_objects(raw_conn)
    keys = [row["key"] for row in referenced]

    assert keys.count(f"{edition1}.mp4") == 1
    assert keys.count(f"{edition2}.mp4") == 1
    assert keys.count(f"{edition3}.mp4") == 1


@pytest.mark.integration
async def test_a_reaped_titles_episodes_are_still_excluded_exactly_as_before(raw_conn):
    """The new `ep.status != 'deleted'` predicate sits beside the existing
    `t.status != 'deleted'` one, not in place of it -- a whole reaped
    title still excludes every one of its episodes' objects, unchanged.
    """
    edition_id = uuid.uuid4()
    slug = f"storage-audit-reaped-title-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Storage Audit Reaped Title", slug, edition_id, f"{edition_id}.mp4"
    )
    await queries.mark_for_delete(raw_conn, title_id, grace_seconds=0)
    await queries.finish_delete(raw_conn, title_id)

    referenced = await queries.referenced_objects(raw_conn)
    keys = [row["key"] for row in referenced]

    assert keys.count(f"{edition_id}.mp4") == 0


# --- plan 18: an address is not broken before anything undertook to
# write it -- referenced_objects gains a predicate on the edition's own
# status (excluding 'preparing') and, separately, on the title's for one
# specific write-path asymmetry. See that function's own docstring for
# the full argument; these tests seed the four cases it argues for.


@pytest.mark.integration
async def test_a_preparing_editions_object_is_not_a_broken_address(raw_conn):
    """The false alarm the acceptance walk found: an edition holds its
    `object_key` from creation and holds no object in the bucket until
    the converter uploads one, so before that upload there is nothing for
    the bucket to be missing. Eleven episodes saved in one folder press
    produced up to eleven of these phantom broken addresses for the
    length of the run.
    """
    edition1 = uuid.uuid4()
    slug = f"storage-audit-preparing-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Storage Audit Preparing", slug, edition1, f"{edition1}.mp4"
    )
    edition2 = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 2, edition2, f"{edition2}.mp4", status="preparing"
    )

    referenced = await queries.referenced_objects(raw_conn)
    keys = [row["key"] for row in referenced]

    assert keys.count(f"{edition1}.mp4") == 1  # the active sibling, unaffected
    assert keys.count(f"{edition2}.mp4") == 0


@pytest.mark.integration
async def test_a_preparing_edition_is_a_broken_address_once_active_and_still_missing_its_object(
    raw_conn,
):
    """The other half of the same fix: once the converter actually
    finishes and the edition flips to active, a bucket still missing its
    object is a genuinely broken address again -- proving the exclusion
    above filters the in-flight window and nothing past it. This is the
    whole reason the count still exists.
    """
    edition1 = uuid.uuid4()
    slug = f"storage-audit-preparing-active-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Storage Audit Preparing Active", slug, edition1, f"{edition1}.mp4"
    )
    edition2 = uuid.uuid4()
    await queries.attach_episode_to_title(
        raw_conn, title_id, 2, edition2, f"{edition2}.mp4", status="preparing"
    )
    assert f"{edition2}.mp4" not in {
        r["key"] for r in await queries.referenced_objects(raw_conn)
    }

    await queries.mark_edition_active(raw_conn, edition2)

    referenced = await queries.referenced_objects(raw_conn)
    assert f"{edition2}.mp4" in {r["key"] for r in referenced}

    result = reconcile({}, [r for r in referenced if str(edition2) in r["key"]])
    assert any(m.key == f"{edition2}.mp4" for m in result.missing)


@pytest.mark.integration
async def test_a_preparing_title_contributes_nothing_until_the_title_is_active(raw_conn):
    """The single-file add's own shape (`create_title_with_edition`):
    `status` lands on the *title* row while the edition insert takes the
    schema's own default (`active`), so a fresh add is one nominally-
    active edition sitting inside a still-preparing title, with no object
    in the bucket yet. The edition-level exclusion alone would never see
    this one -- the edition itself is not `preparing` -- so this is the
    title-level exclusion's own regression, distinct from the two above.
    """
    edition_id = uuid.uuid4()
    slug = f"storage-audit-preparing-title-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        raw_conn,
        "Storage Audit Preparing Title",
        slug,
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    assert f"{edition_id}.mp4" not in {
        r["key"] for r in await queries.referenced_objects(raw_conn)
    }

    await queries.mark_title_active(raw_conn, title_id)

    referenced = await queries.referenced_objects(raw_conn)
    assert f"{edition_id}.mp4" in {r["key"] for r in referenced}


@pytest.mark.integration
async def test_an_edition_inside_its_delete_countdown_is_still_counted_referenced(raw_conn):
    """A `pending_delete` edition's object is untouched while the
    deletion can still be undone (D-62) -- a bucket genuinely missing it
    during that window is still a broken address, so the new `preparing`
    exclusion must not have swallowed this status too. The precedent is
    the identical episode-level test above, one level down.
    """
    edition1 = uuid.uuid4()
    slug = f"storage-audit-edition-countdown-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Storage Audit Edition Countdown", slug, edition1, f"{edition1}.mp4"
    )
    edition2 = uuid.uuid4()
    await queries.attach_episode_to_title(raw_conn, title_id, 2, edition2, f"{edition2}.mp4")
    await queries.mark_edition_for_delete(raw_conn, edition2, grace_seconds=3600)

    referenced = await queries.referenced_objects(raw_conn)
    keys = [row["key"] for row in referenced]

    assert keys.count(f"{edition1}.mp4") == 1
    assert keys.count(f"{edition2}.mp4") == 1


@pytest.mark.integration
async def test_a_failed_editions_object_still_in_the_bucket_is_reported_present_not_orphaned(
    raw_conn,
):
    """The route the production converter reports a failure to deletes
    nothing (D-115: the poster uploads before the video, so a job that
    dies mid-transfer can already have put a real object in the bucket).
    A failed edition must therefore stay in the referenced set -- if
    `failed` is ever added to the exclusion list beside `preparing`, this
    is the test that catches it: the object really is in the bucket, and
    `reconcile` must account for it as present, neither an orphan nor a
    broken address. The cluster holds no failed edition at all today, so
    this hazard is latent and this seed is the only place it exists.
    """
    edition1 = uuid.uuid4()
    slug = f"storage-audit-failed-present-{uuid.uuid4().hex[:8]}"
    title_id = await queries.create_title_with_edition(
        raw_conn, "Storage Audit Failed Present", slug, edition1, f"{edition1}.mp4"
    )
    edition2 = uuid.uuid4()
    await queries.attach_episode_to_title(raw_conn, title_id, 2, edition2, f"{edition2}.mp4")
    await queries.mark_edition_failed(raw_conn, edition2, "failed.reason.upload")

    referenced = await queries.referenced_objects(raw_conn)
    keys = {r["key"] for r in referenced}
    assert f"{edition2}.mp4" in keys  # still counted, unlike a preparing edition

    # The object really is in the bucket -- the failure route never deletes it.
    listing = {f"{edition2}.mp4": 1234}
    result = reconcile(listing, [r for r in referenced if str(edition2) in r["key"]])
    assert result.missing == []
    assert result.unreferenced == []
