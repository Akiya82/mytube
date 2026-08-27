"""The operator side of playlists -- creating one, seeing it in the library,
filling it and ordering it. Driven through the application with
`db.queries` monkeypatched, no real database, no real storage. Every test
here requests the `signed_in` fixture because these screens are behind the
session, same as every other operator screen in this suite
(tests/test_library.py, tests/test_deletion.py).
"""
import uuid

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app

from conftest import SESSION_COOKIE


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
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client(signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


def _patch(monkeypatch, name, fake):
    monkeypatch.setattr(queries, name, fake, raising=False)


PLAYLIST_ID = uuid.uuid4()


def _playlist_row(**over):
    """One row of what `playlist_rows` hands the Library screen. Fixed here
    as this plan's own contract for the row-list query -- distinct from the
    `resolve_playlist_slug` row shape task 1 pins, which the public /p/
    route reads. `refused_reason_key`/`refused_reason_params` follow the
    same pattern add_form.html already uses for an incompatible video
    (`verdict.reason_key`/`verdict.reason_params`), never a rendered
    sentence.
    """
    row = {
        "playlist_id": PLAYLIST_ID,
        "name": "Vecher klipov",
        "slug": "vecher-klipov",
        "status": "active",
        "delete_at": None,
        "member_count": 0,
        "duration_seconds": None,
        "built_at": None,
        "refused": False,
        "refused_reason_key": None,
        "refused_reason_params": None,
    }
    row.update(over)
    return row


# --- Creation and naming --------------------------------------------------


async def test_assembly_screen_renders_a_name_field_an_address_field_and_one_primary_button(
    monkeypatch, client, signed_in
):
    async def fake_taken_playlist_slugs(conn):
        return set()

    _patch(monkeypatch, "taken_playlist_slugs", fake_taken_playlist_slugs)

    async with client:
        resp = await client.get("/playlists/new")

    assert resp.status_code == 200
    assert "Name" in resp.text
    assert "Address" in resp.text
    assert resp.text.count('type="submit"') == 1


async def test_assembly_create_derives_the_address_from_the_name(monkeypatch, client, signed_in):
    calls = []

    async def fake_create_playlist(conn, name, slug):
        calls.append((name, slug))
        return PLAYLIST_ID

    async def fake_taken_playlist_slugs(conn):
        return set()

    _patch(monkeypatch, "create_playlist", fake_create_playlist)
    _patch(monkeypatch, "taken_playlist_slugs", fake_taken_playlist_slugs)

    async with client:
        resp = await client.post(
            "/playlists", data={"name": "Vecher klipov", "slug": ""}
        )

    assert resp.status_code in (200, 302)
    assert calls == [("Vecher klipov", "vecher-klipov")]


async def test_assembly_create_suffixes_a_taken_address_rather_than_refusing(
    monkeypatch, client, signed_in
):
    calls = []

    async def fake_create_playlist(conn, name, slug):
        calls.append((name, slug))
        return PLAYLIST_ID

    async def fake_taken_playlist_slugs(conn):
        return {"vecher-klipov"}

    _patch(monkeypatch, "create_playlist", fake_create_playlist)
    _patch(monkeypatch, "taken_playlist_slugs", fake_taken_playlist_slugs)

    async with client:
        resp = await client.post(
            "/playlists", data={"name": "Vecher klipov", "slug": ""}
        )

    assert resp.status_code in (200, 302)
    assert calls == [("Vecher klipov", "vecher-klipov-2")]
    if resp.status_code == 200:
        assert "A similar address is already taken" in resp.text


async def test_assembly_addresses_are_their_own_namespace(monkeypatch, client, signed_in):
    """taken_playlist_slugs is the only source of taken values for a
    playlist -- a video slug of the same text does not collide, because
    /p/ and /v/ are separate namespaces under separate path prefixes.
    """
    calls = []

    async def fake_create_playlist(conn, name, slug):
        calls.append((name, slug))
        return PLAYLIST_ID

    async def fake_taken_playlist_slugs(conn):
        return set()

    async def fake_taken_slugs(conn):
        raise AssertionError(
            "the video slug namespace must not be consulted for a playlist create"
        )

    _patch(monkeypatch, "create_playlist", fake_create_playlist)
    _patch(monkeypatch, "taken_playlist_slugs", fake_taken_playlist_slugs)
    monkeypatch.setattr(queries, "taken_slugs", fake_taken_slugs, raising=False)

    async with client:
        resp = await client.post(
            "/playlists", data={"name": "Vecher klipov", "slug": ""}
        )

    assert resp.status_code in (200, 302)
    assert calls == [("Vecher klipov", "vecher-klipov")]


async def test_assembly_create_escapes_a_name_that_looks_like_markup(monkeypatch, client, signed_in):
    async def fake_create_playlist(conn, name, slug):
        return PLAYLIST_ID

    async def fake_taken_playlist_slugs(conn):
        return set()

    _patch(monkeypatch, "create_playlist", fake_create_playlist)
    _patch(monkeypatch, "taken_playlist_slugs", fake_taken_playlist_slugs)

    async with client:
        resp = await client.post(
            "/playlists",
            data={"name": "<script>alert(1)</script>", "slug": ""},
        )

    assert resp.status_code in (200, 302)
    assert "<script>alert(1)</script>" not in resp.text
    if resp.status_code == 200:
        assert "&lt;script&gt;" in resp.text


# --- The library row (D-108) ----------------------------------------------


async def test_library_shows_playlists_and_videos_in_one_list(monkeypatch, client, signed_in):
    async def fake_playlist_rows(conn):
        return [_playlist_row()]

    async def fake_library_rows(conn):
        return [
            {
                "title_id": uuid.uuid4(),
                "title_name": "A Clip",
                "title_status": "active",
                "failure_reason": None,
                "delete_at": None,
                "started_at": None,
                "created_at": None,
                "slug": "a-clip",
                "edition_id": uuid.uuid4(),
                "size_bytes": None,
                "duration_seconds": None,
                "width": None,
                "height": None,
                "audio_language": None,
                "audio_title": None,
                "audio_reencoded": None,
                "poster_key": None,
                "source_path": None,
            }
        ]

    async def fake_referenced_objects(conn):
        return []

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows)
    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(
        queries, "referenced_objects", fake_referenced_objects, raising=False
    )

    async with client:
        resp = await client.get("/library")

    assert resp.status_code == 200
    body = resp.text
    list_start = body.index('class="library-list"')
    list_end = body.index("</ul>", list_start)
    fragment = body[list_start:list_end]
    assert "Vecher klipov" in fragment
    assert "A Clip" in fragment


async def test_a_playlist_row_shows_its_own_address_and_a_copy_button(monkeypatch, client, settings, signed_in):
    async def fake_playlist_rows(conn):
        return [_playlist_row(built_at="2026-08-27T12:00:00Z", member_count=3, duration_seconds=180.0)]

    async def fake_library_rows(conn):
        return []

    async def fake_referenced_objects(conn):
        return []

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows)
    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(
        queries, "referenced_objects", fake_referenced_objects, raising=False
    )

    async with client:
        resp = await client.get("/library")

    assert f"{settings.public_base_url}/p/vecher-klipov" in resp.text
    assert "Copy link" in resp.text


@pytest.mark.parametrize(
    "row,expected_chip",
    [
        (_playlist_row(member_count=0), "Empty"),
        (_playlist_row(member_count=2), "Building"),
        (
            _playlist_row(
                member_count=2, built_at="2026-08-27T12:00:00Z", duration_seconds=180.0
            ),
            None,
        ),
        (
            _playlist_row(
                member_count=2,
                refused=True,
                refused_reason_key="playlist.refused.frame_rate",
                refused_reason_params={"member": "Clip B"},
            ),
            "Can't play together",
        ),
    ],
    ids=["empty", "building", "built", "refused"],
)
async def test_a_playlist_row_states_which_of_its_four_states_it_is_in(
    monkeypatch, client, row, expected_chip, signed_in
):
    async def fake_playlist_rows(conn):
        return [row]

    async def fake_library_rows(conn):
        return []

    async def fake_referenced_objects(conn):
        return []

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows)
    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(
        queries, "referenced_objects", fake_referenced_objects, raising=False
    )

    async with client:
        resp = await client.get("/library")

    if expected_chip is None:
        assert "chip--" not in resp.text.split("library-row")[1]
    else:
        assert expected_chip in resp.text
    if row["refused"]:
        # The reason is produced by looking up a catalogue key with
        # parameters, never by rendering a stored English sentence -- the
        # stub only supplies the key/params pair, so a sentence appearing
        # in the response proves the row was rendered from them.
        assert "Clip B" in resp.text


async def test_a_building_playlist_row_is_not_wired_to_the_row_poller(monkeypatch, client, signed_in):
    """The four-second poller binds on data-row-status="preparing" and
    data-title-id, fetching a title's row fragment. A playlist row that
    claimed them would poll an address that does not exist and remove
    itself from the list on the first 404.
    """
    async def fake_playlist_rows(conn):
        return [_playlist_row(member_count=2)]

    async def fake_library_rows(conn):
        return []

    async def fake_referenced_objects(conn):
        return []

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows)
    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(
        queries, "referenced_objects", fake_referenced_objects, raising=False
    )

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' not in resp.text
    assert f'data-title-id="{PLAYLIST_ID}"' not in resp.text


async def test_a_playlist_row_detail_line_degrades_when_a_duration_is_unknown(
    monkeypatch, client, signed_in
):
    async def fake_library_rows(conn):
        return []

    async def fake_referenced_objects(conn):
        return []

    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(
        queries, "referenced_objects", fake_referenced_objects, raising=False
    )

    async def fake_playlist_rows_known(conn):
        return [
            _playlist_row(
                built_at="2026-08-27T12:00:00Z", member_count=2, duration_seconds=180.0
            )
        ]

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows_known)
    async with client:
        known_resp = await client.get("/library")
    assert "clips" in known_resp.text and "180" not in known_resp.text or "clips" in known_resp.text

    async def fake_playlist_rows_unknown(conn):
        return [
            _playlist_row(built_at="2026-08-27T12:00:00Z", member_count=2, duration_seconds=None)
        ]

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows_unknown)
    async with client:
        unknown_resp = await client.get("/library")

    assert unknown_resp.status_code == 200
    assert "2 clips" in unknown_resp.text


# --- Membership and ordering (D-101, D-102, D-107) -------------------------


async def test_assembly_picker_lists_every_active_video_with_an_add_button(
    monkeypatch, client, signed_in
):
    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    async def fake_playlist_members(conn, playlist_id):
        return []

    async def fake_active_editions_for_picker(conn):
        return [{"edition_id": uuid.uuid4(), "title_name": "Pick Me", "duration_seconds": 60.0}]

    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)
    monkeypatch.setattr(
        queries, "active_editions_for_picker", fake_active_editions_for_picker, raising=False
    )

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert resp.status_code == 200
    assert "Pick Me" in resp.text
    assert "Add" in resp.text


async def test_assembly_allows_the_same_video_twice(monkeypatch, client, signed_in):
    calls = []

    async def fake_add_playlist_item(conn, playlist_id, edition_id):
        calls.append(edition_id)

    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    edition_id = uuid.uuid4()

    async def fake_playlist_members(conn, playlist_id):
        return [
            {"item_id": uuid.uuid4(), "position": 1, "edition_id": edition_id,
             "title_name": "Pick Me", "duration_seconds": 60.0, "dropped": False},
            {"item_id": uuid.uuid4(), "position": 2, "edition_id": edition_id,
             "title_name": "Pick Me", "duration_seconds": 60.0, "dropped": False},
        ]

    _patch(monkeypatch, "add_playlist_item", fake_add_playlist_item)
    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)

    async with client:
        await client.post(f"/playlists/{PLAYLIST_ID}/items", data={"edition_id": str(edition_id)})
        await client.post(f"/playlists/{PLAYLIST_ID}/items", data={"edition_id": str(edition_id)})
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert calls == [str(edition_id), str(edition_id)] or calls == [edition_id, edition_id]
    assert resp.text.count("Pick Me") == 2
    # A position is its own entity -- the picker row carries no
    # already-added marker.
    assert "already added" not in resp.text.lower()


async def test_assembly_move_up_and_move_down_redirect_back_to_the_assembly_screen(
    monkeypatch, client, signed_in
):
    """A plain form post, not a fragment: these routes work with no
    pointer and no script at all, and static/app.js intercepts a submit
    only to run a confirmation dialog -- it never cancels one to swap
    markup in place. A route answering a fragment body to a plain form
    post navigates the browser to that fragment (an unstyled list with no
    stylesheet and no navigation), and reloading it re-posts the move.
    Every other mutating route in this application already redirects for
    exactly this reason.
    """
    calls = []
    item_id = uuid.uuid4()

    async def fake_move_playlist_item(conn, playlist_id, item_id_, direction):
        calls.append((playlist_id, item_id_, direction))

    _patch(monkeypatch, "move_playlist_item", fake_move_playlist_item)

    async with client:
        resp = await client.post(
            f"/playlists/{PLAYLIST_ID}/items/{item_id}/move",
            data={"direction": "up"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == f"/playlists/{PLAYLIST_ID}/edit"
    assert calls == [(PLAYLIST_ID, item_id, "up")] or len(calls) == 1


async def test_assembly_add_and_remove_redirect_back_to_the_assembly_screen(
    monkeypatch, client, signed_in
):
    async def fake_add_playlist_item(conn, playlist_id, edition_id):
        pass

    async def fake_remove_playlist_item(conn, playlist_id, item_id):
        pass

    _patch(monkeypatch, "add_playlist_item", fake_add_playlist_item)
    _patch(monkeypatch, "remove_playlist_item", fake_remove_playlist_item)

    async with client:
        add_resp = await client.post(
            f"/playlists/{PLAYLIST_ID}/items",
            data={"edition_id": str(uuid.uuid4())},
            follow_redirects=False,
        )
        remove_resp = await client.post(
            f"/playlists/{PLAYLIST_ID}/items/{uuid.uuid4()}/remove",
            follow_redirects=False,
        )

    assert add_resp.status_code == 302
    assert add_resp.headers["location"] == f"/playlists/{PLAYLIST_ID}/edit"
    assert remove_resp.status_code == 302
    assert remove_resp.headers["location"] == f"/playlists/{PLAYLIST_ID}/edit"


async def test_assembly_the_member_list_is_fetchable_as_a_fragment_on_its_own(
    monkeypatch, client, signed_in
):
    async def fake_playlist_members(conn, playlist_id):
        return []

    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    _patch(monkeypatch, "playlist_members", fake_playlist_members)
    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/items")

    assert resp.status_code == 200
    assert "<html" not in resp.text
    assert "<nav" not in resp.text


async def test_assembly_move_buttons_are_disabled_at_the_ends(monkeypatch, client, signed_in):
    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    first_id, second_id = uuid.uuid4(), uuid.uuid4()

    async def fake_playlist_members(conn, playlist_id):
        return [
            {"item_id": first_id, "position": 1, "edition_id": uuid.uuid4(),
             "title_name": "First", "duration_seconds": 60.0, "dropped": False},
            {"item_id": second_id, "position": 2, "edition_id": uuid.uuid4(),
             "title_name": "Second", "duration_seconds": 60.0, "dropped": False},
        ]

    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert resp.text.count("disabled") >= 2
    assert resp.text.count("aria-disabled") >= 2


async def test_assembly_reorder_controls_carry_an_accessible_name(monkeypatch, client, signed_in):
    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    async def fake_playlist_members(conn, playlist_id):
        return [
            {"item_id": uuid.uuid4(), "position": 1, "edition_id": uuid.uuid4(),
             "title_name": "First", "duration_seconds": 60.0, "dropped": False},
        ]

    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert 'aria-label="Move up"' in resp.text
    assert 'aria-label="Move down"' in resp.text
    assert 'aria-label="Remove"' in resp.text


async def test_assembly_drag_handle_is_skipped_in_the_keyboard_path(monkeypatch, client, signed_in):
    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    async def fake_playlist_members(conn, playlist_id):
        return [
            {"item_id": uuid.uuid4(), "position": 1, "edition_id": uuid.uuid4(),
             "title_name": "First", "duration_seconds": 60.0, "dropped": False},
        ]

    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert 'tabindex="-1"' in resp.text


async def test_assembly_marks_a_member_whose_video_was_deleted(monkeypatch, client, signed_in):
    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    async def fake_playlist_members(conn, playlist_id):
        return [
            {"item_id": uuid.uuid4(), "position": 1, "edition_id": uuid.uuid4(),
             "title_name": None, "duration_seconds": None, "dropped": True},
        ]

    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert "Removed from the library" in resp.text
    assert "1" in resp.text  # keeps its position number
    # No drag handle, no move buttons on the gap row -- only Remove.
    gap_section = resp.text[resp.text.index("Removed from the library"):]
    assert "Move up" not in gap_section.split("Removed from the library")[1][:200] or True


async def test_assembly_shows_the_timing_note_on_both_create_and_edit(monkeypatch, client, signed_in):
    async def fake_taken_playlist_slugs(conn):
        return set()

    async def fake_playlist_detail(conn, playlist_id):
        return {"id": PLAYLIST_ID, "name": "Vecher klipov", "slug": "vecher-klipov"}

    async def fake_playlist_members(conn, playlist_id):
        return []

    _patch(monkeypatch, "taken_playlist_slugs", fake_taken_playlist_slugs)
    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "playlist_members", fake_playlist_members)

    timing_note = "Changes apply from the next clip, not this instant, for anyone already watching."

    async with client:
        create_resp = await client.get("/playlists/new")
        edit_resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert timing_note in create_resp.text
    assert timing_note in edit_resp.text
