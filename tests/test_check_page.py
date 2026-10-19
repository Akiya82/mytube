"""Route tests for GET /check/{slug}.

The database layer is monkeypatched at the `orchestrator.db.queries`
module boundary, the same shape test_library.py and test_add_form.py
already use -- no real Postgres, quick-loop only. `resolve_slug` answers
what /v/{slug} itself would answer (title id + status); `library_row`
supplies the title name the same way the Library screen already reads it,
so this page needs no new query.
"""
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN


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


@pytest.fixture
def _web_env(monkeypatch):
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


@dataclass
class _QueriesStub:
    resolved_by_slug: dict = field(default_factory=dict)
    row_by_title_id: dict = field(default_factory=dict)
    # The episode/edition check routes' own reads, keyed exactly
    # the way routes_episodes.py calls each function.
    episode_resolved: dict = field(default_factory=dict)  # (slug, number, edition_slug) -> row
    title_edition_resolved: dict = field(default_factory=dict)  # (slug, edition_slug) -> row
    episode_with_title_by_id: dict = field(default_factory=dict)  # episode_id -> row
    neighbours_by_key: dict = field(default_factory=dict)  # (title_id, number, edition_slug) -> row


@pytest.fixture
def queries_stub(monkeypatch):
    stub = _QueriesStub()

    async def _resolve_slug(conn, slug):
        return stub.resolved_by_slug.get(slug)

    async def _library_row(conn, title_id):
        return stub.row_by_title_id.get(title_id)

    async def _resolve_episode_slug(conn, slug, number, edition_slug=None):
        return stub.episode_resolved.get((slug, number, edition_slug))

    async def _resolve_title_edition_slug(conn, slug, edition_slug):
        return stub.title_edition_resolved.get((slug, edition_slug))

    async def _episode_with_title(conn, episode_id):
        return stub.episode_with_title_by_id.get(episode_id)

    async def _episode_neighbours(conn, title_id, number, edition_slug=None):
        return stub.neighbours_by_key.get((title_id, number, edition_slug))

    monkeypatch.setattr(queries, "resolve_slug", _resolve_slug)
    monkeypatch.setattr(queries, "library_row", _library_row)
    monkeypatch.setattr(queries, "resolve_episode_slug", _resolve_episode_slug)
    monkeypatch.setattr(queries, "resolve_title_edition_slug", _resolve_title_edition_slug)
    monkeypatch.setattr(queries, "episode_with_title", _episode_with_title)
    monkeypatch.setattr(queries, "episode_neighbours", _episode_neighbours)
    return stub


@pytest.fixture
def client(_web_env, queries_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


@pytest.fixture
def anon_client(_web_env, queries_stub, operator_present):
    """The identical client, minus `signed_in` and minus the session
    cookie -- the gate test: the two new check routes must
    refuse exactly like every other operator-only screen, not gain a
    public exemption.
    """
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _resolved(**over) -> dict:
    row = {"title_id": uuid.uuid4(), "status": "active", "default_edition_id": uuid.uuid4()}
    row.update(over)
    return row


def _row(**over) -> dict:
    row = {"title_id": uuid.uuid4(), "title_name": "Checked Clip", "episode_count": 1}
    row.update(over)
    return row


def _episode_row(**over) -> dict:
    """One `resolve_episode_slug`/`resolve_title_edition_slug` row, the
    shape `_render_episode_check` (routes_episodes.py) reads.
    """
    row = {
        "title_id": uuid.uuid4(),
        "status": "active",
        "episode_id": uuid.uuid4(),
        "episode_status": "active",
        "edition_id": uuid.uuid4(),
        "edition_status": "active",
        "stereo_layout": None,
        "number": None,
    }
    row.update(over)
    return row


def _neighbours(**over) -> dict:
    row = {
        "previous_number": None,
        "next_number": None,
        "previous_has_edition_slug": False,
        "next_has_edition_slug": False,
    }
    row.update(over)
    return row


async def test_video_source_is_exactly_the_public_address_for_the_slug(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["kontrolnyi-rolik"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Kontrolnyi Rolik")

    async with client:
        resp = await client.get("/check/kontrolnyi-rolik")

    assert resp.status_code == 200
    assert '<video class="check-page__video" controls src="https://mytube.example/v/kontrolnyi-rolik">' in resp.text
    assert "/blob/" not in resp.text
    assert "sig=" not in resp.text


async def test_unknown_slug_is_404(client, queries_stub):
    async with client:
        resp = await client.get("/check/does-not-exist")

    assert resp.status_code == 404


async def test_pending_deletion_title_renders_no_video_element(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["going-away"] = _resolved(title_id=title_id, status="pending_delete")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Going Away")

    async with client:
        resp = await client.get("/check/going-away")

    assert resp.status_code == 200
    assert "<video" not in resp.text
    assert "This address is not playing right now." in resp.text


async def test_no_related_or_other_title_list_on_the_page(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["solo-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Solo Clip")

    async with client:
        resp = await client.get("/check/solo-clip")

    assert "<ul" not in resp.text
    assert "<ol" not in resp.text
    assert "library-list" not in resp.text


async def test_hairpin_note_names_the_same_address_and_is_always_present(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["hairpin-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(title_id=title_id, title_name="Hairpin Clip")

    async with client:
        resp = await client.get("/check/hairpin-clip")

    assert "https://mytube.example/v/hairpin-clip" in resp.text
    # The note is a standing explanation, not an error state -- present
    # for every active title's check page, not conditioned on anything a
    # server-rendered response could ever know about playback success.
    assert "router" in resp.text.lower()


# --- The stereo-mode sentence ----------------------------------------------


async def test_layout_hint_sbs_names_the_side_by_side_mode(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["sbs-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="SBS Clip", stereo_layout="sbs_half"
    )

    async with client:
        resp = await client.get("/check/sbs-clip")

    assert "Side by Side 3D" in resp.text


async def test_layout_hint_ou_names_the_over_under_mode(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["ou-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="OU Clip", stereo_layout="ou_full"
    )

    async with client:
        resp = await client.get("/check/ou-clip")

    assert "Over/Under 3D" in resp.text


async def test_layout_hint_says_nothing_for_a_flat_edition(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["flat-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Flat Clip", stereo_layout="2d"
    )

    async with client:
        resp = await client.get("/check/flat-clip")

    assert "check-page__layout-hint" not in resp.text
    assert "3D" not in resp.text


async def test_layout_hint_sits_below_the_video_not_above_it(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["order-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Order Clip", stereo_layout="sbs_full"
    )

    async with client:
        resp = await client.get("/check/order-clip")

    assert resp.text.index('class="check-page__video"') < resp.text.index(
        "check-page__layout-hint"
    )


async def test_layout_hint_sits_below_the_inactive_note_when_not_active(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["inactive-3d-clip"] = _resolved(
        title_id=title_id, status="pending_delete"
    )
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Inactive 3D Clip", stereo_layout="ou_half"
    )

    async with client:
        resp = await client.get("/check/inactive-3d-clip")

    assert "<video" not in resp.text
    assert resp.text.index("check-page__inactive") < resp.text.index("check-page__layout-hint")


async def test_copy_control_still_copies_only_the_address_on_a_3d_title(client, queries_stub):
    """The copy control's own copied value is unchanged by this plan --
    it copies the address and nothing else, never an instruction."""
    title_id = uuid.uuid4()
    queries_stub.resolved_by_slug["copy-3d-clip"] = _resolved(title_id=title_id, status="active")
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Copy 3D Clip", stereo_layout="sbs_full"
    )

    async with client:
        resp = await client.get("/check/copy-3d-clip")

    assert 'data-copy-text="https://mytube.example/v/copy-3d-clip"' in resp.text


# --- An episode's own check page, and an edition's -------------------------


async def test_episode_check_page_renders_its_own_heading_address_player_and_detail(
    client, queries_stub
):
    title_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    queries_stub.episode_resolved[("horimiya", 5, None)] = _episode_row(
        title_id=title_id, episode_id=episode_id
    )
    queries_stub.episode_with_title_by_id[episode_id] = {
        "episode_id": episode_id,
        "episode_status": "active",
        "title_id": title_id,
        "title_name": "Horimiya",
        "number": 5,
    }
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Horimiya", episode_count=1
    )

    async with client:
        resp = await client.get("/check/horimiya/5")

    assert resp.status_code == 200
    assert "Horimiya 5" in resp.text
    assert '<video class="check-page__video" controls src="https://mytube.example/v/horimiya/5">' in resp.text
    assert "https://mytube.example/v/horimiya/5" in resp.text


async def test_edition_check_page_renders_its_own_heading_and_address(client, queries_stub):
    title_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    queries_stub.episode_resolved[("horimiya", 5, "dub-en")] = _episode_row(
        title_id=title_id, episode_id=episode_id
    )
    queries_stub.episode_with_title_by_id[episode_id] = {
        "episode_id": episode_id,
        "episode_status": "active",
        "title_id": title_id,
        "title_name": "Horimiya",
        "number": 5,
    }
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Horimiya", episode_count=1
    )

    async with client:
        resp = await client.get("/check/horimiya/5/dub-en")

    assert resp.status_code == 200
    assert "Horimiya 5 (dub-en)" in resp.text
    assert "https://mytube.example/v/horimiya/5/dub-en" in resp.text


async def test_film_check_page_unaffected_for_a_one_episode_title(client, queries_stub):
    """A film's own alternative edition (a two-segment address whose
    second segment is not made only of digits) resolves through
    `resolve_title_edition_slug`, not `resolve_episode_slug` -- exercised
    here to prove that branch renders too, distinctly from the bare
    `/check/{slug}` film route this plan never touches.
    """
    title_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    queries_stub.title_edition_resolved[("mushoku-tensei-movie", "dub-en")] = _episode_row(
        title_id=title_id, episode_id=episode_id, number=1
    )
    queries_stub.episode_with_title_by_id[episode_id] = {
        "episode_id": episode_id,
        "episode_status": "active",
        "title_id": title_id,
        "title_name": "Mushoku Tensei Movie",
        "number": 1,
    }
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Mushoku Tensei Movie", episode_count=1
    )

    async with client:
        resp = await client.get("/check/mushoku-tensei-movie/dub-en")

    assert resp.status_code == 200
    assert "Mushoku Tensei Movie 1 (dub-en)" in resp.text
    # A one-episode title's nav never renders -- there is nothing
    # to walk to.
    assert "check.cta.next_episode" not in resp.text
    assert 'href="/check/mushoku-tensei-movie/2"' not in resp.text


async def test_unknown_episode_answers_the_same_way_the_title_check_page_answers_for_an_unknown_slug(
    client, queries_stub
):
    async with client:
        resp = await client.get("/check/does-not-exist/5")

    assert resp.status_code == 404


async def test_unknown_edition_slug_answers_not_found(client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.episode_resolved[("horimiya", 5, None)] = _episode_row(title_id=title_id)

    async with client:
        resp = await client.get("/check/horimiya/5/does-not-exist")

    assert resp.status_code == 404


# --- The previous/next navigation ------------------------------------------


def _seed_episode_page(queries_stub, *, slug, number, title_id, episode_id, episode_count, **over):
    queries_stub.episode_resolved[(slug, number, None)] = _episode_row(
        title_id=title_id, episode_id=episode_id, **over
    )
    queries_stub.episode_with_title_by_id[episode_id] = {
        "episode_id": episode_id,
        "episode_status": "active",
        "title_id": title_id,
        "title_name": "Nav Series",
        "number": number,
    }
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Nav Series", episode_count=episode_count
    )


async def test_check_nav_absent_for_a_one_episode_title(client, queries_stub):
    title_id = uuid.uuid4()
    _seed_episode_page(
        queries_stub, slug="one-ep", number=1, title_id=title_id,
        episode_id=uuid.uuid4(), episode_count=1,
    )

    async with client:
        resp = await client.get("/check/one-ep/1")

    assert "check.cta.next_episode" not in resp.text
    assert "check.cta.previous_episode" not in resp.text


async def test_check_nav_appears_only_for_a_multi_episode_title(client, queries_stub):
    title_id = uuid.uuid4()
    _seed_episode_page(
        queries_stub, slug="multi-ep", number=1, title_id=title_id,
        episode_id=uuid.uuid4(), episode_count=2,
    )
    queries_stub.neighbours_by_key[(title_id, 1, None)] = _neighbours(next_number=2)

    async with client:
        resp = await client.get("/check/multi-ep/1")

    assert 'href="/check/multi-ep/2"' in resp.text


async def test_check_nav_previous_and_next_reach_the_right_neighbours_in_number_order(
    client, queries_stub
):
    title_id = uuid.uuid4()
    _seed_episode_page(
        queries_stub, slug="ordered-series", number=5, title_id=title_id,
        episode_id=uuid.uuid4(), episode_count=10,
    )
    queries_stub.neighbours_by_key[(title_id, 5, None)] = _neighbours(
        previous_number=4, next_number=6
    )

    async with client:
        resp = await client.get("/check/ordered-series/5")

    assert 'href="/check/ordered-series/4"' in resp.text
    assert 'href="/check/ordered-series/6"' in resp.text


async def test_check_nav_reaches_a_not_ready_neighbour_and_renders_its_own_state(
    client, queries_stub
):
    """The navigation never computes a ready range -- it links to
    every neighbour, ready or not, and a not-ready neighbour's own page
    renders `check.inactive`, exactly like a not-yet-active title's bare
    check page already does.
    """
    title_id = uuid.uuid4()
    ready_episode_id = uuid.uuid4()
    _seed_episode_page(
        queries_stub, slug="mixed-readiness", number=1, title_id=title_id,
        episode_id=ready_episode_id, episode_count=2,
    )
    queries_stub.neighbours_by_key[(title_id, 1, None)] = _neighbours(next_number=2)

    not_ready_episode_id = uuid.uuid4()
    queries_stub.episode_resolved[("mixed-readiness", 2, None)] = _episode_row(
        title_id=title_id, episode_id=not_ready_episode_id,
        edition_status="preparing",
    )
    queries_stub.episode_with_title_by_id[not_ready_episode_id] = {
        "episode_id": not_ready_episode_id,
        "episode_status": "active",
        "title_id": title_id,
        "title_name": "Nav Series",
        "number": 2,
    }
    queries_stub.neighbours_by_key[(title_id, 2, None)] = _neighbours(previous_number=1)

    async with client:
        first = await client.get("/check/mixed-readiness/1")
        second = await client.get("/check/mixed-readiness/2")

    assert 'href="/check/mixed-readiness/2"' in first.text
    assert second.status_code == 200
    assert "This address is not playing right now." in second.text


async def test_check_nav_keeps_the_edition_slug_when_the_neighbour_has_a_matching_edition(
    client, queries_stub
):
    title_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    queries_stub.episode_resolved[("dubbed-series", 3, "dub-en")] = _episode_row(
        title_id=title_id, episode_id=episode_id
    )
    queries_stub.episode_with_title_by_id[episode_id] = {
        "episode_id": episode_id, "episode_status": "active",
        "title_id": title_id, "title_name": "Dubbed Series", "number": 3,
    }
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Dubbed Series", episode_count=5
    )
    queries_stub.neighbours_by_key[(title_id, 3, "dub-en")] = _neighbours(
        previous_number=2, next_number=4,
        previous_has_edition_slug=True, next_has_edition_slug=True,
    )

    async with client:
        resp = await client.get("/check/dubbed-series/3/dub-en")

    assert 'href="/check/dubbed-series/2/dub-en"' in resp.text
    assert 'href="/check/dubbed-series/4/dub-en"' in resp.text


async def test_check_nav_falls_back_to_default_edition_when_the_neighbour_lacks_a_matching_edition(
    client, queries_stub
):
    title_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    queries_stub.episode_resolved[("dubbed-series-2", 3, "dub-en")] = _episode_row(
        title_id=title_id, episode_id=episode_id
    )
    queries_stub.episode_with_title_by_id[episode_id] = {
        "episode_id": episode_id, "episode_status": "active",
        "title_id": title_id, "title_name": "Dubbed Series 2", "number": 3,
    }
    queries_stub.row_by_title_id[title_id] = _row(
        title_id=title_id, title_name="Dubbed Series 2", episode_count=5
    )
    queries_stub.neighbours_by_key[(title_id, 3, "dub-en")] = _neighbours(
        previous_number=2, next_number=4,
        previous_has_edition_slug=False, next_has_edition_slug=False,
    )

    async with client:
        resp = await client.get("/check/dubbed-series-2/3/dub-en")

    assert 'href="/check/dubbed-series-2/2"' in resp.text
    assert 'href="/check/dubbed-series-2/4"' in resp.text
    assert 'href="/check/dubbed-series-2/2/dub-en"' not in resp.text
    assert 'href="/check/dubbed-series-2/4/dub-en"' not in resp.text


async def test_check_nav_first_boundary_renders_disabled_link_with_its_reason(
    client, queries_stub
):
    title_id = uuid.uuid4()
    _seed_episode_page(
        queries_stub, slug="boundary-series", number=1, title_id=title_id,
        episode_id=uuid.uuid4(), episode_count=3,
    )
    queries_stub.neighbours_by_key[(title_id, 1, None)] = _neighbours(next_number=2)

    async with client:
        resp = await client.get("/check/boundary-series/1")

    assert 'aria-disabled="true"' in resp.text
    assert "This is the first episode." in resp.text
    assert 'href="/check/boundary-series/0"' not in resp.text


async def test_check_nav_last_boundary_renders_disabled_link_with_its_reason(
    client, queries_stub
):
    title_id = uuid.uuid4()
    _seed_episode_page(
        queries_stub, slug="boundary-series-2", number=3, title_id=title_id,
        episode_id=uuid.uuid4(), episode_count=3,
    )
    queries_stub.neighbours_by_key[(title_id, 3, None)] = _neighbours(previous_number=2)

    async with client:
        resp = await client.get("/check/boundary-series-2/3")

    assert 'aria-disabled="true"' in resp.text
    assert "This is the last episode." in resp.text
    assert 'href="/check/boundary-series-2/4"' not in resp.text


# --- Both new routes stay behind the session gate --------------------------


async def test_episode_check_page_gate_redirects_when_unauthenticated(anon_client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.episode_resolved[("gated-series", 1, None)] = _episode_row(title_id=title_id)

    async with anon_client:
        resp = await anon_client.get("/check/gated-series/1", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


async def test_edition_check_page_gate_redirects_when_unauthenticated(anon_client, queries_stub):
    title_id = uuid.uuid4()
    queries_stub.episode_resolved[("gated-series", 1, "dub-en")] = _episode_row(title_id=title_id)

    async with anon_client:
        resp = await anon_client.get("/check/gated-series/1/dub-en", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")
