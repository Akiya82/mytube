"""Unit tests for /p/{slug} -- the db.queries layer is monkeypatched, no real
database involved (see conftest.py's `settings` fixture for the env vars this
needs). Mirrors tests/test_resolve.py's scaffold verbatim: this is the same
acquire/sign/redirect shape as the video address, with one deliberate
divergence pinned by this module -- playlist addresses are never tombstoned.
"""
import uuid

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.signing import sign_blob_url

PLAYLIST_ID = uuid.uuid4()


class _DummyConn:
    pass


class _DummyAcquireCtx:
    async def __aenter__(self):
        return _DummyConn()

    async def __aexit__(self, *exc_info):
        return False


class _DummyPool:
    def acquire(self):
        return _DummyAcquireCtx()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _app_env(settings):
    """Same cache-clearing shape as test_resolve.py's fixture of the same
    name -- get_settings is @lru_cache'd across the whole test session.
    """
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client():
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _built_row(slug="vecher-klipov", built_object_key="a1b2c3d4-0000-0000-0000-000000000000.mp4"):
    """An active, concat-built playlist row. `built_object_key` is the full
    storage key, extension included, mirroring how `editions.object_key` is
    already stored. Visibly ends in `.mp4` so a reader of this file cannot
    mistake it for a bare identifier.
    """
    return {
        "id": PLAYLIST_ID,
        "status": "active",
        "mode": "concat",
        "built_object_key": built_object_key,
        "built_at": "2026-08-27T12:00:00Z",
        "name": "Vecher klipov",
        "slug": slug,
    }


def _pending_row(slug="vecher-klipov", member_count=0):
    """An active row that has not finished building yet -- `built_at` is
    `None`. `member_count` distinguishes the two not-ready cases the route
    deliberately treats identically (D-104) by carrying it as an informational
    field the route never reads: neither case is actionable by the viewer, so
    both answer the same status with the same clip regardless of it.
    """
    return {
        "id": PLAYLIST_ID,
        "status": "active",
        "mode": "concat",
        "built_object_key": None,
        "built_at": None,
        "name": "Vecher klipov",
        "slug": slug,
        "member_count": member_count,
    }


async def test_a_built_playlist_redirects_to_its_signed_artifact(monkeypatch, client, settings):
    row = _built_row()

    async def fake_resolve_playlist_slug(conn, slug):
        assert slug == "vecher-klipov"
        return row

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get(
            "/p/vecher-klipov", headers={"host": "mytube.example"}, follow_redirects=False
        )

    assert resp.status_code == 302
    location = resp.headers["location"]
    # https, not http: external_base_url defaults the forwarded protocol to
    # https, and test_resolve.py already asserts exactly that for the same
    # header set -- reused here rather than re-derived.
    assert location.startswith(f"https://mytube.example/blob/{row['built_object_key']}?")

    sig, exp = sign_blob_url(
        row["built_object_key"], settings.secure_link_secret, settings.sign_ttl_seconds
    )
    query = location.split("?", 1)[1]
    params = dict(p.split("=") for p in query.split("&"))
    assert params["sig"] == sig
    # exp is a wall-clock timestamp computed independently by the fixture's
    # own sign call and the route -- assert closeness, not equality.
    assert abs(int(params["exp"]) - exp) <= 2


async def test_the_signed_playlist_address_carries_the_extension_exactly_once(
    monkeypatch, client
):
    row = _built_row()

    async def fake_resolve_playlist_slug(conn, slug):
        return row

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get("/p/vecher-klipov", follow_redirects=False)

    location = resp.headers["location"]
    path = location.split("://", 1)[1].split("/", 1)[1]
    path = "/" + path.split("?", 1)[0]
    # Delivery block's own pattern: /blob/, a dash-separated hex identifier,
    # .mp4, end. A doubled extension (…mp4.mp4) misses this and serves the
    # not-found clip forever behind a signature that verifies; a missing one
    # makes the reclamation call a no-op.
    assert path == f"/blob/{row['built_object_key']}", (
        "the Location must carry the stored object key's .mp4 extension "
        "exactly once -- doubled, it serves the not-found clip forever "
        "behind a signature that verifies; missing, reclamation deletes nothing"
    )


async def test_an_unknown_playlist_slug_is_a_plain_404(monkeypatch, client):
    async def fake_resolve_playlist_slug(conn, slug):
        return None

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get("/p/does-not-exist", follow_redirects=False)

    assert resp.status_code == 404


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"id": PLAYLIST_ID, "status": "deleted", "mode": "concat",
         "built_object_key": None, "built_at": None, "name": "x", "slug": "gone"},
        # The grace window: the playlist still exists and its bytes are
        # still in the bucket. A reader would otherwise assume this falls
        # through to the built branch and starts serving a playlist the
        # operator has already deleted.
        {"id": PLAYLIST_ID, "status": "pending_delete", "mode": "concat",
         "built_object_key": "a1b2c3d4-0000-0000-0000-000000000000.mp4",
         "built_at": "2026-08-27T12:00:00Z", "name": "x", "slug": "gone"},
    ],
    ids=["unknown", "deleted", "pending_delete"],
)
async def test_a_playlist_slug_never_answers_410(monkeypatch, client, row):
    async def fake_resolve_playlist_slug(conn, slug):
        return row

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get("/p/gone", follow_redirects=False)

    assert resp.status_code != 410, (
        "playlist addresses are not tombstoned (D-103) -- unlike /v/, an "
        "address that stops working stops working outright, so a copy of "
        "the video route's tombstone branch is wrong here"
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "row",
    [
        _pending_row(member_count=0),
        _pending_row(member_count=3),
    ],
    ids=["no_members", "has_members_not_built"],
)
async def test_a_playlist_that_is_not_ready_answers_the_not_ready_status(
    monkeypatch, client, row
):
    """The route branches on the readiness timestamp alone, never on member
    count: the viewer cannot act on the difference between "no members" and
    "not built yet", so one status and one clip covers both (D-104). Written
    as one parametrised test rather than two, because two separately named
    tests over an identical row shape would have looked like two behaviours
    and could never have disagreed. The operator-side distinction (whether
    the playlist has members at all) is a chip on the library row, tested
    elsewhere.
    """
    from orchestrator.web.routes_playlists import PLAYLIST_NOT_READY_STATUS

    assert PLAYLIST_NOT_READY_STATUS == 425

    async def fake_resolve_playlist_slug(conn, slug):
        return row

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get("/p/vecher-klipov", follow_redirects=False)

    assert resp.status_code == PLAYLIST_NOT_READY_STATUS


async def test_a_saturated_pool_answers_503_rather_than_waiting(monkeypatch, client):
    import asyncio

    from orchestrator import auth

    monkeypatch.setattr(auth, "ACQUIRE_TIMEOUT_SECONDS", 0.05)

    async def fake_resolve_playlist_slug(conn, slug):
        await asyncio.sleep(0.2)
        return _built_row()

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get("/p/vecher-klipov", follow_redirects=False)

    assert resp.status_code == 503


async def test_a_short_playlist_link_opened_on_the_interface_hostname_redirects_to_the_delivery_address(
    monkeypatch, client, settings
):
    """Same special case external_base_url already handles for the video
    address: reusing that function rather than writing a second one is what
    this test pins.
    """
    row = _built_row()

    async def fake_resolve_playlist_slug(conn, slug):
        return row

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )

    async with client:
        resp = await client.get(
            "/p/vecher-klipov",
            headers={"x-forwarded-host": settings.admin_fqdn},
            follow_redirects=False,
        )

    assert resp.headers["location"].startswith(settings.public_base_url)


def _manifest_row(slug="vecher-klipov"):
    """An active playlist built as a generated list. There is no single
    artifact to redirect to -- `built_object_key` stays null, because the
    join behind it only ever finds a single assembled file.
    """
    return {
        "id": PLAYLIST_ID,
        "status": "active",
        "mode": "hls",
        "built_object_key": None,
        "built_at": "2026-08-28T12:00:00Z",
        "name": "Vecher klipov",
        "slug": slug,
    }


def _artifact(key, position, duration=8.0, title_name="Clip"):
    return {
        "object_key": key,
        "position": position,
        "duration_seconds": duration,
        "title_name": title_name,
    }


def _patch_delivery(monkeypatch, row, artifacts):
    async def fake_resolve_playlist_slug(conn, slug):
        return row

    async def fake_sections(conn, playlist_id):
        return artifacts

    monkeypatch.setattr(
        queries, "resolve_playlist_slug", fake_resolve_playlist_slug, raising=False
    )
    monkeypatch.setattr(
        queries, "playlist_artifact_sections", fake_sections, raising=False
    )


async def test_a_generated_list_is_answered_with_signed_addresses_and_marked_unstorable(
    monkeypatch, client, settings
):
    keys = [
        "a1b2c3d4-0000-0000-0000-00000000000{}.mp4".format(index) for index in range(2)
    ]
    _patch_delivery(
        monkeypatch,
        _manifest_row(),
        [_artifact(keys[0], 0, 8.0, "Clip A"), _artifact(keys[1], 1, 12.0, "Clip B")],
    )

    async with client:
        resp = await client.get(
            "/p/vecher-klipov", headers={"host": "mytube.example"}, follow_redirects=False
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    # The document is only valid for as long as the signatures inside it,
    # so no intermediary may keep a copy past that.
    assert "no-store" in resp.headers["cache-control"]

    body = resp.text
    assert body.startswith("#EXTM3U")
    assert body.count("#EXT-X-DISCONTINUITY") == 1
    assert body.rstrip().endswith("#EXT-X-ENDLIST")

    for key in keys:
        # Signed by the one existing signer, and carrying the stored key's
        # extension exactly once -- doubled, the edge serves the not-found
        # clip forever behind a signature that verifies.
        assert f"https://mytube.example/blob/{key}?exp=" in body
        assert f"{key}.mp4" not in body

    sig, _exp = sign_blob_url(
        keys[0], settings.secure_link_secret, settings.sign_ttl_seconds
    )
    assert f"sig={sig}" in body


async def test_a_generated_list_with_nothing_left_in_it_is_never_sent(monkeypatch, client):
    """The playlist is built, but every member has since dropped out. An
    empty list is malformed, and a malformed one does not fail for one
    viewer -- it hangs every player in the instance. So it is not sent at
    all: the not-ready status goes out, which the edge answers with a clip.
    """
    from orchestrator.web.routes_playlists import PLAYLIST_NOT_READY_STATUS

    _patch_delivery(monkeypatch, _manifest_row(), [])

    async with client:
        resp = await client.get("/p/vecher-klipov", follow_redirects=False)

    assert resp.status_code == PLAYLIST_NOT_READY_STATUS
    assert resp.content == b""


async def test_a_generated_list_naming_a_segment_that_is_not_there_is_never_sent(
    monkeypatch, client
):
    """A duration of zero cannot be scheduled, so the document it renders
    into is malformed. Same answer, same reason: nothing that failed
    validation reaches a room full of players.
    """
    from orchestrator.web.routes_playlists import PLAYLIST_NOT_READY_STATUS

    _patch_delivery(
        monkeypatch,
        _manifest_row(),
        [_artifact("a1b2c3d4-0000-0000-0000-000000000000.mp4", 0, duration=None)],
    )

    async with client:
        resp = await client.get("/p/vecher-klipov", follow_redirects=False)

    assert resp.status_code == PLAYLIST_NOT_READY_STATUS
    assert resp.content == b""
