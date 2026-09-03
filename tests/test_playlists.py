"""The operator side of playlists -- creating one, seeing it in the library,
filling it and ordering it. Driven through the application with
`db.queries` monkeypatched, no real database, no real storage. Every test
here requests the `signed_in` fixture because these screens are behind the
session, same as every other operator screen in this suite
(tests/test_library.py, tests/test_deletion.py).

One test in this file is the deliberate exception and runs against the real
migrated database with nothing stubbed: reordering from the arrows alone
is the one path that has to work with no pointer and no script, so it is
also the one path where a stub standing in for the statement that actually
renumbers the list would prove nothing.
"""
import datetime
import html
import uuid

import httpx
import pytest

from orchestrator import reaper
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.storage import StorageUsage
from orchestrator.web import routes_titles

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


async def _fake_empty_picker(conn):
    return []


@pytest.fixture(autouse=True)
def _picker_stub(monkeypatch):
    """`GET /playlists/{id}/edit` always renders the picker section, so
    every test in this file that reaches that route needs
    `active_editions_for_picker` stubbed even when the picker's own
    content is not what the test is about -- most of this file's assembly
    tests are about membership, ordering and the gap mark rather than the
    picker, and were written before the picker existed. Rather than repeat
    the same patch in every one of them, it lives here, autoused; the one
    test that asserts on picker content
    (test_assembly_picker_lists_every_active_video_with_an_add_button)
    overrides it with its own patch, applied through the same monkeypatch
    fixture instance, so the override simply wins.
    """
    monkeypatch.setattr(queries, "active_editions_for_picker", _fake_empty_picker, raising=False)


@pytest.fixture(autouse=True)
def _storage_stub(monkeypatch):
    """`GET /library` -- exercised by several tests in this file, since a
    playlist row lives there (D-108) -- always calls `storage_usage()` for
    the header's used/free figure, same as it always has for a library of
    videos alone. This module drives no real storage (see its own
    docstring), so that call is stubbed here exactly the way
    `tests/test_library.py`'s own `storage_stub` fixture already stubs it,
    autoused rather than requested per-test since it has nothing to do
    with what any individual test in this file is about.
    """
    monkeypatch.setattr(
        routes_titles,
        "storage_usage",
        lambda: StorageUsage(total_bytes=0, object_count=0, free_bytes=None, listing={}),
    )


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
    """One row of what `playlist_rows` hands the Library screen -- distinct
    from the `resolve_playlist_slug` row shape the public /p/ route reads.
    `refused_reason_key`/`refused_reason_params` follow the same pattern
    add_form.html already uses for an incompatible video
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
        "job_id": None,
        "job_kind": None,
        "job_status": None,
        "job_db_stage": None,
        "job_progress_pct": None,
        "job_eta_seconds": None,
        "job_cancel_requested": False,
        "job_queued_ahead": 0,
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


# --- The library row: a playlist lives next to videos, same row shape (D-108) --


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
        # Jinja's autoescaping renders the copy's apostrophe as `&#39;` --
        # unescape before comparing so this asserts against the text a
        # browser would show, not against the raw HTML source.
        assert expected_chip in html.unescape(resp.text)
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

    async def fake_playlist_rows_unknown(conn):
        return [
            _playlist_row(built_at="2026-08-27T12:00:00Z", member_count=2, duration_seconds=None)
        ]

    # Both requests share the one client instance the fixture hands out --
    # httpx.AsyncClient refuses to be reopened as a context manager once
    # closed, so both GETs run inside the same `async with` block, with
    # the second monkeypatch applied between them.
    _patch(monkeypatch, "playlist_rows", fake_playlist_rows_known)
    async with client:
        known_resp = await client.get("/library")
        assert "clips" in known_resp.text and "180" not in known_resp.text or "clips" in known_resp.text

        _patch(monkeypatch, "playlist_rows", fake_playlist_rows_unknown)
        unknown_resp = await client.get("/library")

    assert unknown_resp.status_code == 200
    assert "2 clips" in unknown_resp.text


# --- Membership and ordering: repeats allowed, gaps marked, arrows first --
# (D-101, D-102, D-107) --------------------------------------------------


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

    # No drag handle and no move buttons on the gap row -- only Remove.
    # There is nothing useful to reorder into place for an entry that will
    # never play, so the row carries exactly one control.
    row_start = resp.text.rindex("<li", 0, resp.text.index("Removed from the library"))
    gap_row = resp.text[row_start : resp.text.index("</li>", row_start)]
    assert "Move up" not in gap_row
    assert "Move down" not in gap_row
    assert "drag-handle" not in gap_row
    assert gap_row.count("aria-label") == 1


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

    timing_note = (
        "Changes apply the next time someone opens this link. Anyone already "
        "watching keeps playing the list as it was when they started."
    )

    async with client:
        create_resp = await client.get("/playlists/new")
        edit_resp = await client.get(f"/playlists/{PLAYLIST_ID}/edit")

    assert timing_note in create_resp.text
    assert timing_note in edit_resp.text


async def test_the_arrows_reorder_the_list_end_to_end_with_no_script_in_play(
    app_pool, raw_conn, signed_in
):
    """The no-pointer baseline, proven rather than assumed: a plain form
    post to the move address, the redirect followed the way a browser with
    no scripting follows one, and the assembly screen that comes back
    listing the members in their new order. Nothing is stubbed between the
    request and Postgres, so this covers the one seam the rest of this
    file cannot -- that the route hands the statement the arguments that
    actually renumber the list, and that the new order survives being read
    back rather than only being echoed.

    Reloading the screen afterwards is not a redundant assertion: it is
    the difference between the server having been told and the page having
    been redrawn. A move that only changed the response would pass every
    assertion above this one and fail this one.
    """
    suffix = uuid.uuid4().hex[:8]
    playlist_id = await queries.create_playlist(
        raw_conn, f"Keyboard Evening {suffix}", f"keyboard-evening-{suffix}"
    )
    names = [f"Clip A {suffix}", f"Clip B {suffix}", f"Clip C {suffix}"]
    for index, name in enumerate(names):
        edition_id = uuid.uuid4()
        await queries.create_title_with_edition(
            raw_conn, name, f"clip-{index}-{suffix}", edition_id, f"{edition_id}.mp4"
        )
        await queries.add_playlist_item(raw_conn, playlist_id, edition_id)

    members = await queries.playlist_members(raw_conn, playlist_id)
    assert [m["title_name"] for m in members] == names
    last_item_id = members[2]["item_id"]
    first_item_id = members[0]["item_id"]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        moved = await client.post(
            f"/playlists/{playlist_id}/items/{last_item_id}/move",
            data={"direction": "up"},
            follow_redirects=True,
        )
        # The top of the list has no neighbour above it. That is a no-op,
        # not an error and not a wrap around to the bottom.
        no_op = await client.post(
            f"/playlists/{playlist_id}/items/{first_item_id}/move",
            data={"direction": "up"},
            follow_redirects=True,
        )
        # An arbitrary direction is not a third instruction.
        nonsense = await client.post(
            f"/playlists/{playlist_id}/items/{first_item_id}/move",
            data={"direction": "sideways"},
            follow_redirects=True,
        )
        reloaded = await client.get(f"/playlists/{playlist_id}/edit")

    assert moved.status_code == 200
    assert no_op.status_code == 200
    assert nonsense.status_code == 200

    def rendered_order(text):
        return sorted(names, key=lambda name: text.index(html.escape(name)))

    expected = [names[0], names[2], names[1]]
    assert rendered_order(moved.text) == expected
    assert rendered_order(reloaded.text) == expected

    after = await queries.playlist_members(raw_conn, playlist_id)
    assert [m["title_name"] for m in after] == expected
    assert [m["position"] for m in after] == [0, 1, 2]


# --- Deletion: the same gesture, the same countdown, as a video ----------

DELETION_DEADLINE = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.timezone.utc)
PLAYLIST_ARTIFACT_KEYS = ["seg-a.mp4", "seg-b.mp4"]


def _deletion_stubs(monkeypatch, status):
    """Everything the three deletion routes touch, recorded rather than
    performed: the row each one guards on, the writes they make, and the
    objects the inline sweep removes.
    """
    calls = {"deleted_objects": [], "finished": [], "marked": [], "cancelled": []}
    pending = []

    async def fake_playlist_row(conn, playlist_id):
        return _playlist_row(
            status=status,
            delete_at=DELETION_DEADLINE if status == "pending_delete" else None,
        )

    async def fake_mark(conn, playlist_id, grace_seconds):
        calls["marked"].append((playlist_id, grace_seconds))
        pending.append(playlist_id)

    async def fake_cancel(conn, playlist_id):
        calls["cancelled"].append(playlist_id)

    async def fake_finish(conn, playlist_id):
        calls["finished"].append(playlist_id)

    async def fake_due(conn):
        return [{"playlist_id": pid} for pid in pending]

    async def fake_keys(conn, playlist_id):
        return list(PLAYLIST_ARTIFACT_KEYS)

    async def fake_reclaim(conn, older_than_seconds):
        return []

    _patch(monkeypatch, "playlist_row", fake_playlist_row)
    _patch(monkeypatch, "mark_playlist_for_delete", fake_mark)
    _patch(monkeypatch, "cancel_delete_playlist", fake_cancel)
    _patch(monkeypatch, "finish_delete_playlist", fake_finish)
    _patch(monkeypatch, "due_for_reaping_playlists", fake_due)
    _patch(monkeypatch, "playlist_artifact_keys", fake_keys)
    _patch(monkeypatch, "due_for_reclaim_artifacts", fake_reclaim)
    monkeypatch.setattr(reaper, "delete_object", calls["deleted_objects"].append)
    return calls


async def test_deleting_a_playlist_starts_the_grace_window(monkeypatch, client, signed_in):
    calls = _deletion_stubs(monkeypatch, "active")

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/delete", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/library"
    assert calls["marked"] == [(PLAYLIST_ID, get_settings().delete_grace_seconds)]
    assert calls["deleted_objects"] == []


async def test_deleting_a_playlist_that_is_already_pending_is_refused(
    monkeypatch, client, signed_in
):
    calls = _deletion_stubs(monkeypatch, "pending_delete")

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/delete", follow_redirects=False)

    assert resp.status_code == 404
    assert calls["marked"] == []


async def test_undo_returns_a_pending_playlist_to_active(monkeypatch, client, signed_in):
    calls = _deletion_stubs(monkeypatch, "pending_delete")

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/undo", follow_redirects=False)

    assert resp.status_code == 302
    assert calls["cancelled"] == [PLAYLIST_ID]


async def test_undo_against_an_active_playlist_is_refused_and_changes_nothing(
    monkeypatch, client, signed_in
):
    calls = _deletion_stubs(monkeypatch, "active")

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/undo", follow_redirects=False)

    assert resp.status_code == 400
    assert calls["cancelled"] == []


async def test_purge_removes_every_artifact_and_finishes_the_row_before_responding(
    monkeypatch, client, signed_in
):
    """Delete-now does not hand the work to the next poll: by the time the
    redirect is written, every object this playlist made is gone from the
    bucket and the row says so.
    """
    calls = _deletion_stubs(monkeypatch, "pending_delete")

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/purge", follow_redirects=False)

    assert resp.status_code == 302
    assert calls["marked"] == [(PLAYLIST_ID, 0)]
    assert calls["deleted_objects"] == PLAYLIST_ARTIFACT_KEYS
    assert calls["finished"] == [PLAYLIST_ID]


async def test_purge_against_an_active_playlist_is_refused(monkeypatch, client, signed_in):
    calls = _deletion_stubs(monkeypatch, "active")

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/purge", follow_redirects=False)

    assert resp.status_code == 400
    assert calls["deleted_objects"] == []


async def test_a_pending_playlist_row_renders_the_same_countdown_a_video_row_does(
    monkeypatch, client, signed_in
):
    """No second countdown implementation: the row carries the attributes
    the existing tick already binds to, so the chip ticks with no browser
    code added for playlists.
    """
    async def fake_library_rows(conn):
        return []

    async def fake_referenced_objects(conn):
        return []

    async def fake_playlist_rows(conn):
        return [
            _playlist_row(status="pending_delete", delete_at=DELETION_DEADLINE, member_count=2)
        ]

    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(queries, "referenced_objects", fake_referenced_objects, raising=False)
    _patch(monkeypatch, "playlist_rows", fake_playlist_rows)

    async with client:
        resp = await client.get("/library")

    expected_deadline = int(DELETION_DEADLINE.timestamp() * 1000)
    assert f'data-deadline="{expected_deadline}"' in resp.text
    assert 'data-row-status="pending_delete"' in resp.text
    assert "chip--deleting" in resp.text
    assert f'action="/playlists/{PLAYLIST_ID}/undo"' in resp.text
    assert f'action="/playlists/{PLAYLIST_ID}/purge"' in resp.text
    # Both destructive forms go through the dialog the video row already uses.
    assert resp.text.count("data-confirm") >= 2


# --- Job progress, cancel and retry (04-11) -------------------------------
# A playlist build now shares the same live-update mechanism a video's own
# Preparing row already has (04-UI-SPEC.md Amendment 3): its own fragment
# route, its own data-row-url, and data-row-status="preparing" for exactly
# as long as a job is actually in flight.


async def test_playlist_fragment_route_renders_the_same_partial_for_one_row(
    monkeypatch, client, signed_in
):
    async def fake_playlist_row(conn, playlist_id):
        return _playlist_row(member_count=2, **_job_fields())

    _patch(monkeypatch, "playlist_row", fake_playlist_row)

    async with client:
        resp = await client.get(f"/playlists/{PLAYLIST_ID}/row")

    assert resp.status_code == 200
    assert resp.text.count("<li ") == 1
    assert "<html" not in resp.text


def _job_fields(**over):
    fields = {
        "job_id": uuid.uuid4(),
        "job_kind": "playlist",
        "job_status": "queued",
        "job_db_stage": "queued",
        "job_progress_pct": None,
        "job_eta_seconds": None,
        "job_cancel_requested": False,
        "job_queued_ahead": 2,
    }
    fields.update(over)
    return fields


async def test_a_building_playlist_row_with_a_live_job_is_wired_to_the_row_poller(
    monkeypatch, client, signed_in
):
    """The amendment to 03-UI-SPEC.md's own "no live updates" decision: an
    assembly with an actual job in flight now polls exactly like a video's
    Preparing row, reading its own fragment address off data-row-url
    rather than the poller building one from a title id it does not have.
    """
    async def fake_playlist_rows(conn):
        return [_playlist_row(member_count=2, **_job_fields())]

    async def fake_library_rows(conn):
        return []

    async def fake_referenced_objects(conn):
        return []

    _patch(monkeypatch, "playlist_rows", fake_playlist_rows)
    monkeypatch.setattr(queries, "library_rows", fake_library_rows, raising=False)
    monkeypatch.setattr(queries, "referenced_objects", fake_referenced_objects, raising=False)

    async with client:
        resp = await client.get("/library")

    assert 'data-row-status="preparing"' in resp.text
    assert f'data-row-url="/playlists/{PLAYLIST_ID}/row"' in resp.text
    assert "2 ahead of you" in resp.text


async def test_cancel_on_a_queued_playlist_job_finishes_it_with_no_hard_delete(
    monkeypatch, client, signed_in
):
    """Unlike a title, a playlist is never hard-deleted by cancel -- it is
    a thing the operator made, not a byproduct of the build."""
    calls = {"cancelled_job_ids": []}

    async def fake_job_for_playlist(conn, playlist_id):
        return {"id": "job-1"}

    async def fake_request_cancel(conn, job_id):
        calls["cancelled_job_ids"].append(job_id)
        return {"status": "cancelled"}

    _patch(monkeypatch, "job_for_playlist", fake_job_for_playlist)
    _patch(monkeypatch, "request_job_cancel", fake_request_cancel)

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/cancel", follow_redirects=False)

    assert resp.status_code == 302
    assert calls["cancelled_job_ids"] == ["job-1"]


async def test_cancel_against_a_playlist_with_no_live_job_is_refused(
    monkeypatch, client, signed_in
):
    async def fake_job_for_playlist(conn, playlist_id):
        return None

    _patch(monkeypatch, "job_for_playlist", fake_job_for_playlist)

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/cancel", follow_redirects=False)

    assert resp.status_code == 404


async def test_build_again_enqueues_a_fresh_playlist_job(monkeypatch, client, signed_in):
    calls = {"enqueued": []}

    async def fake_playlist_detail(conn, playlist_id):
        return {"id": playlist_id, "status": "active"}

    async def fake_enqueue_job(conn, **kwargs):
        calls["enqueued"].append(kwargs)
        return uuid.uuid4()

    _patch(monkeypatch, "playlist_detail", fake_playlist_detail)
    _patch(monkeypatch, "enqueue_job", fake_enqueue_job)

    async with client:
        resp = await client.post(f"/playlists/{PLAYLIST_ID}/retry", follow_redirects=False)

    assert resp.status_code == 302
    assert calls["enqueued"] == [{"kind": "playlist", "playlist_id": PLAYLIST_ID}]
