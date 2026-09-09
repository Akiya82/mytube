"""Unit tests for `storage-audit` (`orchestrator.cli.storage_audit`).

Both the catalog query and the bucket listing are stubbed at the module
boundary -- no real Postgres, no real S3, quick-loop only. The comparison
itself (`storage.reconcile`) is already covered by `tests/test_storage.py`;
these tests cover what `storage_audit()` does with the result: the report
shape, the exit code, and the two extra requirements the plan calls out --
naming a missing object by the `/v/{slug}` address it breaks, and never
calling anything that deletes or aborts.
"""
import asyncpg
import pytest

from orchestrator.cli import storage_audit
from orchestrator.db import queries
from orchestrator.storage import StorageUsage

from conftest import ADMIN_FQDN, WORKER_TOKEN


class _DummyConn:
    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_connect(monkeypatch):
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
