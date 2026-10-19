"""Route tests for the folder confirmation screen (`GET /add-folder/{path}`)
-- it follows `test_add_form.py`'s own shape: no
real Postgres or subprocess, `ffprobe` and the database layer both
monkeypatched at their module boundary.
"""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from mytube_media.compat import ProbeError
from mytube_media.share import ShareEntry
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.recognize import recognize_filename
from orchestrator.web import routes_folder, routes_share

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _probe_returning(fixture_name: str):
    data = _fixture(fixture_name)

    def _probe(path):
        return data

    return _probe


def _probe_raising():
    def _probe(path):
        raise ProbeError("simulated unreadable file")

    return _probe


class _DummyTxCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


class _DummyConn:
    """A stand-in `asyncpg.Connection` for the save route's own
    transaction/advisory-lock statements, the identical shape
    `test_add_form.py` already uses for the single-file save -- every
    `queries.*` call the write path makes is stubbed by `db_stub` below,
    so nothing here ever reaches real SQL.
    """

    def transaction(self):
        return _DummyTxCtx()

    async def execute(self, *args, **kwargs):
        return None


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
def folder_share_root(tmp_path):
    """A season folder with three episodes, plus a subfolder holding a
    fourth video that must never be listed or counted -- the direct-
    files-only rule this whole screen rests on.
    """
    root = tmp_path / "share"
    root.mkdir()
    season = root / "Horimiya"
    season.mkdir()
    for n in (1, 2, 3):
        (season / f"Horimiya - {n:02d}.mkv").write_bytes(b"stub video bytes")
    (season / "notes.txt").write_text("hello", encoding="utf-8")
    dubs = season / "RUS Sound" / "AniStar"
    dubs.mkdir(parents=True)
    (dubs / "bonus.mkv").write_bytes(b"stub video bytes")

    single = root / "OneFile"
    single.mkdir()
    (single / "a-video.mp4").write_bytes(b"stub video bytes")

    # A second, plain two-file folder the row-state tests reuse under
    # different probe/db_stub configurations per test, kept separate from
    # "Horimiya" so those files' own three-episode shape stays fixed for
    # the other tests.
    mixed = root / "Mixed"
    mixed.mkdir()
    (mixed / "Mixed - 01.mkv").write_bytes(b"stub video bytes")
    (mixed / "Mixed - 02.mkv").write_bytes(b"stub video bytes")

    # A twelve-episode season, every file already an
    # edition of the same title -- the edition-only mode's own folder,
    # kept separate so a studio-folder-covers-eleven-of-twelve test can
    # tick 12 files without perturbing the fixed three-episode
    # "Horimiya" shape above.
    season = root / "Season"
    season.mkdir()
    for n in range(1, 13):
        (season / f"Season - {n:02d}.mkv").write_bytes(b"stub video bytes")
    return root


@pytest.fixture(autouse=True)
def _fresh_probe_semaphore(monkeypatch):
    """`routes_folder._PROBE_SEMAPHORE` is a module global, and an
    `asyncio.Semaphore` binds itself to the first event loop that has to
    *wait* on it -- which only happens in a folder holding more files than
    its own limit. One process, one loop in production; one loop per test
    here, so the binding has to be dropped between tests or the second
    large-folder test in a run raises instead of probing.
    """
    monkeypatch.setattr(
        routes_folder,
        "_PROBE_SEMAPHORE",
        asyncio.Semaphore(routes_folder.MAX_CONCURRENT_FOLDER_PROBES),
    )


@pytest.fixture
def _web_env(folder_share_root, tmp_path, monkeypatch):
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(folder_share_root))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_stub(monkeypatch):
    import uuid as _uuid

    class _Stub:
        already_added_rows: list[dict] = []
        resolve_slug_result = None
        taken_slugs_result: set = set()
        title_by_current_slug_result = None
        taken_episode_numbers_result: set = set()
        create_title_with_episodes_title_id = None
        create_title_with_episodes_calls: list[dict] = []
        attach_episodes_to_title_calls: list[dict] = []
        reparent_edition_to_episode_calls: list[dict] = []
        enqueue_job_calls: list[dict] = []
        touch_title_changed_calls: list = []
        # The edition-only folder mode's own reads/writes.
        default_editions_technical_result: list = []
        audio_languages_on_episodes_result: set = set()
        slug_taken_episode_ids: set = set()
        episode_with_title_result = None
        add_edition_to_episode_calls: list[dict] = []

    stub = _Stub()
    stub.already_added_rows = []
    stub.taken_slugs_result = set()
    stub.taken_episode_numbers_result = set()
    stub.create_title_with_episodes_title_id = _uuid.uuid4()
    stub.create_title_with_episodes_calls = []
    stub.attach_episodes_to_title_calls = []
    stub.reparent_edition_to_episode_calls = []
    stub.enqueue_job_calls = []
    stub.touch_title_changed_calls = []
    stub.default_editions_technical_result = []
    stub.audio_languages_on_episodes_result = set()
    stub.slug_taken_episode_ids = set()
    stub.episode_with_title_result = None
    stub.add_edition_to_episode_calls = []

    async def _editions_by_source_paths(conn, paths):
        return [row for row in stub.already_added_rows if row["source_path"] in paths]

    async def _resolve_slug(conn, slug):
        return stub.resolve_slug_result

    async def _taken_slugs(conn):
        return stub.taken_slugs_result

    async def _title_by_current_slug(conn, slug):
        return stub.title_by_current_slug_result

    async def _taken_episode_numbers(conn, title_id):
        return stub.taken_episode_numbers_result

    async def _create_title_with_episodes(conn, name, slug, episodes, **kwargs):
        stub.create_title_with_episodes_calls.append(
            {"name": name, "slug": slug, "episodes": episodes, **kwargs}
        )
        return stub.create_title_with_episodes_title_id, [_uuid.uuid4() for _ in episodes]

    async def _attach_episodes_to_title(conn, title_id, episodes, **kwargs):
        stub.attach_episodes_to_title_calls.append(
            {"title_id": title_id, "episodes": episodes, **kwargs}
        )
        return [_uuid.uuid4() for _ in episodes]

    async def _reparent_edition_to_episode(conn, **kwargs):
        stub.reparent_edition_to_episode_calls.append(kwargs)
        return _uuid.uuid4()

    async def _enqueue_job(conn, **kwargs):
        stub.enqueue_job_calls.append(kwargs)
        return _uuid.uuid4()

    async def _touch_title_changed(conn, title_id):
        stub.touch_title_changed_calls.append(title_id)

    async def _default_editions_technical(conn, episode_ids):
        return [
            row for row in stub.default_editions_technical_result
            if row["episode_id"] in episode_ids
        ]

    async def _audio_languages_on_episodes(conn, episode_ids):
        return stub.audio_languages_on_episodes_result

    async def _edition_slug_taken_on_episode(conn, episode_id, slug):
        return episode_id in stub.slug_taken_episode_ids

    async def _episode_with_title(conn, episode_id):
        if stub.episode_with_title_result is not None:
            return stub.episode_with_title_result
        if stub.already_added_rows:
            return {"title_id": stub.already_added_rows[0]["title_id"]}
        return None

    async def _add_edition_to_episode(conn, episode_id, slug, edition_id, object_key, **kwargs):
        stub.add_edition_to_episode_calls.append(
            {
                "episode_id": episode_id,
                "slug": slug,
                "edition_id": edition_id,
                "object_key": object_key,
                **kwargs,
            }
        )

    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)
    monkeypatch.setattr(queries, "resolve_slug", _resolve_slug)
    monkeypatch.setattr(queries, "taken_slugs", _taken_slugs)
    monkeypatch.setattr(queries, "title_by_current_slug", _title_by_current_slug)
    monkeypatch.setattr(queries, "taken_episode_numbers", _taken_episode_numbers)
    monkeypatch.setattr(queries, "create_title_with_episodes", _create_title_with_episodes)
    monkeypatch.setattr(queries, "attach_episodes_to_title", _attach_episodes_to_title)
    monkeypatch.setattr(queries, "reparent_edition_to_episode", _reparent_edition_to_episode)
    monkeypatch.setattr(queries, "enqueue_job", _enqueue_job)
    monkeypatch.setattr(queries, "touch_title_changed", _touch_title_changed)
    monkeypatch.setattr(queries, "default_editions_technical", _default_editions_technical)
    monkeypatch.setattr(queries, "audio_languages_on_episodes", _audio_languages_on_episodes)
    monkeypatch.setattr(queries, "edition_slug_taken_on_episode", _edition_slug_taken_on_episode)
    monkeypatch.setattr(queries, "episode_with_title", _episode_with_title)
    monkeypatch.setattr(queries, "add_edition_to_episode", _add_edition_to_episode)
    return stub


@pytest.fixture
def web_client(_web_env, db_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


# --- The button inside the opened folder ----------------------------------


async def test_button_renders_on_a_listing_with_two_video_files(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert resp.status_code == 200
    assert "share.cta.add_folder" not in resp.text  # a real translated label, not the key
    assert 'action="/add-folder/Horimiya"' in resp.text
    assert "Add folder as series" in resp.text


async def test_button_does_not_render_on_a_listing_with_one_video_file(web_client):
    async with web_client:
        resp = await web_client.get("/add/OneFile")

    assert resp.status_code == 200
    assert "/add-folder/" not in resp.text


async def test_button_does_not_render_on_a_listing_with_zero_video_files(web_client, tmp_path):
    async with web_client:
        resp = await web_client.get("/add")  # the root: two folders, no direct video

    assert resp.status_code == 200
    assert "/add-folder" not in resp.text


async def test_button_does_not_render_inside_a_folder_row_of_a_parent_listing(web_client):
    async with web_client:
        resp = await web_client.get("/add")

    assert resp.status_code == 200
    assert "Horimiya" in resp.text  # the folder row itself is still listed
    assert "/add-folder/" not in resp.text


async def test_button_is_not_nested_inside_the_row_link(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    # The folder-add form sits in its own action row, never inside
    # .share-row__link -- a real nesting bug would place the <form> between
    # <a class="share-row__link"...> and its own closing tag.
    listing_start = resp.text.index('<ul class="share-listing"')
    assert 'action="/add-folder' not in resp.text[listing_start:]


async def test_edition_label_shows_when_every_file_resolves_to_one_title(web_client, db_stub):
    db_stub.already_added_rows = [
        {"source_path": f"Horimiya/Horimiya - {n:02d}.mkv", "title_id": "same-title"}
        for n in (1, 2, 3)
    ]

    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add edition for these episodes" in resp.text


async def test_folder_button_form_submits_a_url_that_still_selects_edition_mode(
    web_client, db_stub
):
    """A page-wide `"mode=edition" in resp.text` check would have passed
    against a button that could not work -- the href used to carry the
    query string, but a GET form discards it and rebuilds one from its
    own fields on submit, so the query being present *anywhere on the
    page* proved nothing about what the browser actually sends. This
    slices the form element itself out of the page: the action must carry
    no query string at all, and the mode has to be a field inside the
    form, or clicking this button lands on the ordinary series screen
    instead of the edition one it is labelled for.
    """
    db_stub.already_added_rows = [
        {"source_path": f"Horimiya/Horimiya - {n:02d}.mkv", "title_id": "same-title"}
        for n in (1, 2, 3)
    ]

    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    form_start = resp.text.index('<form method="get"')
    form_end = resp.text.index("</form>", form_start)
    form_html = resp.text[form_start:form_end]

    action_start = form_html.index('action="') + len('action="')
    action_end = form_html.index('"', action_start)
    assert "?" not in form_html[action_start:action_end]

    assert 'name="mode"' in form_html
    assert 'value="edition"' in form_html


async def test_plain_label_shows_when_files_resolve_to_several_titles(web_client, db_stub):
    db_stub.already_added_rows = [
        {"source_path": "Horimiya/Horimiya - 01.mkv", "title_id": "title-a"},
        {"source_path": "Horimiya/Horimiya - 02.mkv", "title_id": "title-b"},
    ]

    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add folder as series" in resp.text
    assert 'name="mode"' not in resp.text  # no mode field anywhere in the form


async def test_plain_label_shows_when_no_files_are_already_added(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add folder as series" in resp.text


async def test_folder_route_refuses_a_path_outside_the_share(web_client, monkeypatch):
    """A literal `../` traversal cannot be exercised through httpx/ASGI --
    both normalize dot segments client-side before the request is ever
    sent. `safe_join` is the real
    control and has its own direct tests in test_share.py; here the
    contract under test is that this route calls it and answers 400 when
    it raises, exactly like every other share route.
    """
    from mytube_media.share import ShareEscape

    def _raise(root, rel):
        raise ShareEscape("simulated escape")

    monkeypatch.setattr(routes_share, "safe_join", _raise)

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    assert resp.status_code == 400


async def test_folder_route_refuses_a_file_path(web_client):
    async with web_client:
        resp = await web_client.get("/add-folder/OneFile/a-video.mp4")

    assert resp.status_code == 400


async def test_folder_route_refuses_a_folder_with_fewer_than_two_video_files(web_client):
    async with web_client:
        resp = await web_client.get("/add-folder/OneFile")

    assert resp.status_code == 400


async def test_both_catalogs_hold_the_folder_button_keys():
    import json as _json

    en = _json.loads(
        (Path(__file__).parent.parent / "orchestrator" / "locales" / "en.json").read_text(
            encoding="utf-8"
        )
    )
    ru = _json.loads(
        (Path(__file__).parent.parent / "orchestrator" / "locales" / "ru.json").read_text(
            encoding="utf-8"
        )
    )
    for key in ("share.cta.add_folder", "share.cta.add_edition_folder", "folder.cta.probing"):
        assert key in en
        assert key in ru


# --- Recognising and probing a whole folder -------------------------------


def _probe_dispatch(mapping: dict):
    """Answer a different frozen fixture depending on which file is being
    probed -- keyed by filename, since a folder's own files are usually
    the same show at different bitrates/tracks rather than byte-identical.
    """

    def _probe(path):
        name = Path(path).name
        if name not in mapping:
            raise ProbeError(f"no fixture wired for {name}")
        return _fixture(mapping[name])

    return _probe


@pytest.fixture(autouse=True)
def _default_moov_first(monkeypatch):
    monkeypatch.setattr(routes_folder, "moov_before_mdat", lambda path: False)


def _entry(name: str, rel_path: str) -> ShareEntry:
    return ShareEntry(name=name, is_dir=False, size=17, mtime=0.0, rel_path=rel_path)


async def _run_build_rows(entries, monkeypatch, probe_map: dict):
    monkeypatch.setattr(routes_folder, "probe", _probe_dispatch(probe_map))
    recognised_by_path = {e.rel_path: recognize_filename(e.name) for e in entries}
    root = Path(FIXTURES_DIR).parent  # any real directory; nothing under it is opened
    return recognised_by_path, await routes_folder._build_rows(
        conn=object(), root=root, active_entries=entries, recognised_by_path=recognised_by_path
    )


async def test_direct_only_lists_files_directly_in_the_folder(folder_share_root):
    from mytube_media.share import list_dir

    entries = list_dir(folder_share_root / "Horimiya", folder_share_root)
    names = {e.name for e in entries if not e.is_dir}
    assert names == {"Horimiya - 01.mkv", "Horimiya - 02.mkv", "Horimiya - 03.mkv", "notes.txt"}
    assert "bonus.mkv" not in names  # lives under RUS Sound/AniStar, a subfolder


async def test_subfolder_video_is_absent_from_the_video_list(folder_share_root):
    from mytube_media.share import list_dir

    entries = list_dir(folder_share_root / "Horimiya", folder_share_root)
    video_names = {e.name for e in entries if not e.is_dir and routes_share._is_video_file(e.name)}
    assert video_names == {"Horimiya - 01.mkv", "Horimiya - 02.mkv", "Horimiya - 03.mkv"}


async def test_sort_order_is_by_filename(folder_share_root):
    from mytube_media.share import list_dir

    entries = sorted(
        (e for e in list_dir(folder_share_root / "Horimiya", folder_share_root) if not e.is_dir and routes_share._is_video_file(e.name)),
        key=lambda e: e.name.casefold(),
    )
    assert [e.name for e in entries] == [
        "Horimiya - 01.mkv",
        "Horimiya - 02.mkv",
        "Horimiya - 03.mkv",
    ]


async def test_recognised_numbers_are_read_from_the_filename():
    r = recognize_filename("Horimiya - 05.mkv")
    assert r.episode == 5
    assert r.name == "Horimiya"


async def test_an_unparsed_number_gives_an_empty_unticked_row(monkeypatch, db_stub):
    entries = [_entry("Horimiya - Bonus Feature.mkv", "Horimiya - Bonus Feature.mkv")]
    _recognised, built = await _run_build_rows(
        entries, monkeypatch, {"Horimiya - Bonus Feature.mkv": "compatible.json"}
    )
    row = built["rows"][0]
    assert row["number"] is None
    assert row["checked"] is False
    assert row["number_missing"] is True


async def test_provenance_season_from_the_tag(monkeypatch, db_stub):
    entries = [_entry("Mushoku Tensei S2 - 01.mkv", "Mushoku Tensei S2 - 01.mkv")]
    _r, built = await _run_build_rows(
        entries, monkeypatch, {"Mushoku Tensei S2 - 01.mkv": "compatible.json"}
    )
    assert built["season"] == 2
    assert built["season_provenance"] == "detected_tag"


async def test_provenance_season_from_a_roman_numeral(monkeypatch, db_stub):
    entries = [_entry("Horimiya II - 01.mkv", "Horimiya II - 01.mkv")]
    _r, built = await _run_build_rows(entries, monkeypatch, {"Horimiya II - 01.mkv": "compatible.json"})
    assert built["season"] == 2
    assert built["season_provenance"] == "detected_numeral"


async def test_provenance_season_from_an_ordinal():
    # anitopy already resolves the common "Nth Season" phrasing through its
    # own anime_season tag (recognize.py's own docstring) -- reaching the
    # ordinal fallback through a real filename would only ever exercise
    # detected_tag, so this goes straight at _effective_season with a
    # Recognised carrying the ordinal provenance directly, the same way
    # test_recognize.py's own test_ordinal_season_split calls
    # normalize_season rather than recognize_filename for the same reason.
    from orchestrator.recognize import Recognised

    recognised = Recognised(
        name="Grand Blue Dreaming", season=3, season_provenance="detected_ordinal",
        episode=1, raw_name="Grand Blue Dreaming 3rd Season - 01.mkv",
    )
    season, provenance = routes_folder._effective_season([recognised], "irrelevant")
    assert season == 3
    assert provenance == "detected_ordinal"


async def test_provenance_season_from_a_trailing_digit(monkeypatch, db_stub):
    entries = [_entry("Horimiya 2 - 01.mkv", "Horimiya 2 - 01.mkv")]
    _r, built = await _run_build_rows(entries, monkeypatch, {"Horimiya 2 - 01.mkv": "compatible.json"})
    assert built["season"] == 2
    assert built["season_provenance"] == "detected_trailing_digit"


async def test_provenance_season_from_the_folder_name_when_no_file_carries_one(monkeypatch, db_stub):
    entries = [
        _entry("Ep 01.mkv", "Foo II/Ep 01.mkv"),
        _entry("Ep 02.mkv", "Foo II/Ep 02.mkv"),
    ]
    monkeypatch.setattr(routes_folder, "probe", _probe_dispatch({"Ep 01.mkv": "compatible.json", "Ep 02.mkv": "compatible.json"}))
    recognised_by_path = {e.rel_path: recognize_filename(e.name) for e in entries}
    root = Path(FIXTURES_DIR).parent
    fake_folder_root = root / "Foo II"
    built = await routes_folder._build_rows(
        conn=object(),
        root=root,
        active_entries=entries,
        recognised_by_path=recognised_by_path,
    )
    # _effective_season reads root.name for the folder-name fallback --
    # exercised directly here since _build_rows takes the share root, not
    # the opened folder, in its own signature.
    season, provenance = routes_folder._effective_season(
        [recognised_by_path[e.rel_path] for e in entries], "Foo II"
    )
    assert season == 2
    assert provenance == "detected_folder"


async def test_provenance_season_none_defaults_to_season_one():
    season, provenance = routes_folder._effective_season(
        [recognize_filename("Something - 01.mkv")], "Something"
    )
    assert season == 1
    assert provenance == "none"


async def test_multi_season_grouping_splits_by_name_and_season():
    entries = [
        _entry("Horimiya - 01.mkv", "Horimiya - 01.mkv"),
        _entry("Horimiya II - 01.mkv", "Horimiya II - 01.mkv"),
    ]
    recognised_by_path = {e.rel_path: recognize_filename(e.name) for e in entries}
    groups = routes_folder._group_files(entries, recognised_by_path)
    assert len(groups) == 2


async def test_group_scope_parameter_is_validated_against_the_computed_groups(web_client):
    async with web_client:
        ok = await web_client.get("/add-folder/Horimiya?group=0")
        bad = await web_client.get("/add-folder/Horimiya?group=99")
        garbage = await web_client.get("/add-folder/Horimiya?group=not-a-number")

    assert ok.status_code == 200
    assert bad.status_code == 400
    assert garbage.status_code == 400


async def test_a_failed_probe_keeps_its_row(monkeypatch, db_stub):
    entries = [_entry("Horimiya - 01.mkv", "Horimiya - 01.mkv")]

    def _raise(path):
        raise ProbeError("simulated")

    monkeypatch.setattr(routes_folder, "probe", _raise)
    recognised_by_path = {e.rel_path: recognize_filename(e.name) for e in entries}
    built = await routes_folder._build_rows(
        conn=object(), root=Path(FIXTURES_DIR).parent, active_entries=entries,
        recognised_by_path=recognised_by_path,
    )
    row = built["rows"][0]
    assert row["probe_failed"] is True
    assert row["verdict"] is None
    assert row["number"] == 1  # still recognised and still listed


async def test_the_concurrency_bound_is_a_named_semaphore():
    assert routes_folder.MAX_CONCURRENT_FOLDER_PROBES == routes_folder._PROBE_SEMAPHORE._value


async def test_audio_mismatch_flags_only_the_file_missing_the_chosen_track(monkeypatch, db_stub):
    entries = [
        _entry("Horimiya - 01.mkv", "Horimiya - 01.mkv"),
        _entry("Horimiya - 02.mkv", "Horimiya - 02.mkv"),
    ]
    _r, built = await _run_build_rows(
        entries,
        monkeypatch,
        {"Horimiya - 01.mkv": "compatible.json", "Horimiya - 02.mkv": "multitrack.json"},
    )
    row1, row2 = built["rows"]
    assert row1["audio_mismatch"] is False
    assert row2["audio_mismatch"] is True
    assert row2["highlighted"] is True
    assert len(row2["audio_fallback"]) == 2  # jpn and eng, this file's own tracks


async def test_highlighted_row_carries_its_fallback_vocabulary(monkeypatch, db_stub):
    entries = [
        _entry("Horimiya - 01.mkv", "Horimiya - 01.mkv"),
        _entry("Horimiya - 02.mkv", "Horimiya - 02.mkv"),
    ]
    _r, built = await _run_build_rows(
        entries,
        monkeypatch,
        {"Horimiya - 01.mkv": "compatible.json", "Horimiya - 02.mkv": "multitrack.json"},
    )
    mismatched = built["rows"][1]
    fallback_languages = {opt["language"] for opt in mismatched["audio_fallback"]}
    assert fallback_languages == {"jpn", "eng"}


def test_track_mismatch_helper_covers_both_kinds():
    assert routes_folder._matches_default(None, frozenset()) is True
    sig = ("embedded", "rus", None)
    assert routes_folder._matches_default(sig, frozenset({sig})) is True
    assert routes_folder._matches_default(sig, frozenset({("embedded", "eng", None)})) is False


async def test_already_added_rows_are_never_highlighted_and_take_no_part_in_options(
    monkeypatch, db_stub
):
    db_stub.already_added_rows = [
        {
            "source_path": "Horimiya - 01.mkv",
            "title_id": "t1",
            "title_name": "Horimiya",
            "status": "active",
            "slug": "horimiya",
            "episode_id": "e1",
            "number": 1,
            "edition_kind": "remux",
        }
    ]
    entries = [
        _entry("Horimiya - 01.mkv", "Horimiya - 01.mkv"),
        _entry("Horimiya - 02.mkv", "Horimiya - 02.mkv"),
    ]
    _r, built = await _run_build_rows(
        entries,
        monkeypatch,
        {"Horimiya - 01.mkv": "compatible.json", "Horimiya - 02.mkv": "multitrack.json"},
    )
    moved_row, other_row = built["rows"]
    assert moved_row["moved"] is True
    assert moved_row["highlighted"] is False
    assert moved_row["already_added"]["slug"] == "horimiya"
    # Only the un-moved file's own tracks feed the folder-wide options, so
    # with one file left, nothing is left to mismatch against.
    assert other_row["highlighted"] is False


async def test_exact_slug_match_preselects_the_existing_title(monkeypatch, db_stub):
    db_stub.title_by_current_slug_result = {"title_id": "t1", "title_name": "Horimiya", "title_status": "active"}
    entries = [_entry("Horimiya - 01.mkv", "Horimiya - 01.mkv")]
    _r, built = await _run_build_rows(entries, monkeypatch, {"Horimiya - 01.mkv": "compatible.json"})
    assert built["title_match"] is not None
    assert built["title_match"]["title_id"] == "t1"


async def test_a_taken_number_is_reported(monkeypatch, db_stub):
    db_stub.title_by_current_slug_result = {"title_id": "t1", "title_name": "Horimiya", "title_status": "active"}
    db_stub.taken_episode_numbers_result = {1}
    entries = [_entry("Horimiya - 01.mkv", "Horimiya - 01.mkv")]
    _r, built = await _run_build_rows(entries, monkeypatch, {"Horimiya - 01.mkv": "compatible.json"})
    assert built["rows"][0]["number_taken"] is True


async def test_the_estimate_sums_only_ticked_rows(monkeypatch, db_stub):
    entries = [
        _entry("Horimiya - 01.mkv", "Horimiya - 01.mkv"),
        _entry("Horimiya - Bonus.mkv", "Horimiya - Bonus.mkv"),
    ]
    _r, built = await _run_build_rows(
        entries,
        monkeypatch,
        {"Horimiya - 01.mkv": "compatible.json", "Horimiya - Bonus.mkv": "compatible.json"},
    )
    ticked_row, unticked_row = built["rows"]
    assert unticked_row["checked"] is False
    assert built["estimate_size_bytes"] == ticked_row["size_bytes"]
    assert built["estimate_size_bytes"] > 0


# --- The confirmation screen ----------------------------------------------


async def test_rendered_order_matches_the_contracts_numbering(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    text = resp.text

    markers = [
        'class="breadcrumb"',
        'name="title_mode"',
        ">Tracks for every file<",
        ">Files<",
        'class="folder-file-row"',
        'id="preview-panel"',
        'class="cost-line"',
        'class="add-form__actions"',
    ]
    positions = [text.index(m) for m in markers]
    assert positions == sorted(positions)


async def test_grouping_banner_absent_for_a_single_group_folder(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    assert "Continue with" not in resp.text


async def test_grouping_banner_shows_one_line_per_group(
    monkeypatch, folder_share_root, db_stub, _web_env, signed_in
):
    # A folder whose files split into two names -- two distinct groups,
    # each with its own "Continue with N files as {title}" link.
    folder = folder_share_root / "Mixed2"
    folder.mkdir(parents=True)
    (folder / "ShowA - 01.mkv").write_bytes(b"x")
    (folder / "ShowB - 01.mkv").write_bytes(b"x")
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        resp = await client.get("/add-folder/Mixed2")
    assert resp.text.count("Continue with") == 2


async def test_title_mode_preselects_the_existing_title_on_an_exact_match(
    web_client, monkeypatch, db_stub
):
    db_stub.title_by_current_slug_result = {
        "title_id": "t1", "title_name": "Horimiya", "title_status": "active",
    }
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    existing_tag = resp.text.split('value="existing"')[1].split(">")[0]
    assert "checked" in existing_tag
    new_tag = resp.text.split('value="new"')[1].split(">")[0]
    assert "checked" not in new_tag


async def test_title_mode_stays_on_new_title_when_only_similar(web_client, monkeypatch, db_stub):
    db_stub.taken_slugs_result = {"hori-miya"}
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    new_tag = resp.text.split('value="new"')[1].split(">")[0]
    assert "checked" in new_tag
    assert 'name="target_title_id"' not in resp.text


async def test_name_slug_and_provenance_caption_render(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    assert 'value="Horimiya"' in resp.text
    assert 'value="horimiya"' in resp.text
    assert "No season detected, treated as season 1." in resp.text


async def test_the_three_fieldsets_render_once_each_not_per_file(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    assert resp.text.count(">Audio track<") == 1
    assert resp.text.count(">Subtitles<") == 1
    assert resp.text.count(">3D layout<") == 1
    assert resp.text.count('class="folder-file-row"') == 3  # one per file, unlike the fieldsets


async def test_row_state_plain_ticked_and_numbered(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    assert "checked" in resp.text.split('name="include_0"')[1].split(">")[0]
    assert 'value="1"' in resp.text.split('name="episode_number_0"')[1].split(">")[0]


async def test_row_state_empty_and_unticked(monkeypatch, db_stub, folder_share_root, _web_env, signed_in):
    # "Mixed - Special.mkv" recognises to the same name ("Mixed") as the
    # other two files in this folder, with no episode number -- same
    # group, so it renders in the default (unscoped) view alongside them.
    (folder_share_root / "Mixed" / "Mixed - Special.mkv").write_bytes(b"x")
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        resp = await client.get("/add-folder/Mixed")
    row_start = resp.text.index('data-preview-path="Mixed/Mixed - Special.mkv"')
    row_html = resp.text[row_start: row_start + 900]
    assert 'value=""' in row_html
    assert "No episode number recognized" in row_html


async def test_row_state_already_added_with_move_checkbox(web_client, monkeypatch, db_stub):
    db_stub.already_added_rows = [
        {
            "source_path": "Horimiya/Horimiya - 01.mkv",
            "title_id": "t1",
            "title_name": "Horimiya",
            "status": "active",
            "slug": "horimiya",
            "episode_id": "e1",
            "number": 1,
            "edition_kind": "remux",
        }
    ]
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")

    assert "Already added as horimiya" in resp.text
    assert 'name="move_0"' in resp.text
    assert "checked" in resp.text.split('name="move_0"')[1].split(">")[0]
    assert "The old address keeps working." in resp.text


async def test_row_state_highlighted_with_fallback_group(monkeypatch, db_stub, folder_share_root, _web_env, signed_in):
    monkeypatch.setattr(
        routes_folder,
        "probe",
        _probe_dispatch(
            {"Mixed - 01.mkv": "compatible.json", "Mixed - 02.mkv": "multitrack.json"}
        ),
    )
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        resp = await client.get("/add-folder/Mixed")

    assert "folder-file-row--highlighted" in resp.text
    assert "This file&#39;s track differs from the folder&#39;s choice" in resp.text or (
        "differs from the folder" in resp.text
    )
    assert 'name="audio_fallback_1"' in resp.text


async def test_row_state_unreadable(monkeypatch, db_stub, folder_share_root, _web_env, signed_in):
    def _mixed_probe(path):
        name = Path(path).name
        if name == "Mixed - 01.mkv":
            raise ProbeError("simulated")
        return _fixture("compatible.json")

    monkeypatch.setattr(routes_folder, "probe", _mixed_probe)
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        resp = await client.get("/add-folder/Mixed")
    assert "This file could not be read" in resp.text


async def test_empty_state_nothing_recognized(monkeypatch, db_stub, folder_share_root, _web_env, signed_in):
    unparsed = folder_share_root / "AllUnparsed"
    unparsed.mkdir()
    (unparsed / "Random Movie File.mkv").write_bytes(b"x")
    (unparsed / "Another Bonus Clip.mkv").write_bytes(b"x")

    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        resp = await client.get("/add-folder/AllUnparsed")
    assert "No episode numbers were recognized" in resp.text


async def test_empty_state_all_added_one_title(web_client, monkeypatch, db_stub):
    db_stub.already_added_rows = [
        {"source_path": f"Horimiya/Horimiya - {n:02d}.mkv", "title_id": "t1",
         "title_name": "Horimiya", "status": "active", "slug": "horimiya",
         "episode_id": f"e{n}", "number": n, "edition_kind": "remux"}
        for n in (1, 2, 3)
    ]
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    assert "Every file here is already added" in resp.text


async def test_empty_state_all_added_several_titles(web_client, monkeypatch, db_stub):
    db_stub.already_added_rows = [
        {"source_path": "Horimiya/Horimiya - 01.mkv", "title_id": "t1",
         "title_name": "Horimiya", "status": "active", "slug": "horimiya",
         "episode_id": "e1", "number": 1, "edition_kind": "remux"},
        {"source_path": "Horimiya/Horimiya - 02.mkv", "title_id": "t2",
         "title_name": "Other", "status": "active", "slug": "other",
         "episode_id": "e2", "number": 1, "edition_kind": "remux"},
        {"source_path": "Horimiya/Horimiya - 03.mkv", "title_id": "t1",
         "title_name": "Horimiya", "status": "active", "slug": "horimiya",
         "episode_id": "e3", "number": 3, "edition_kind": "remux"},
    ]
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    assert "Every file here is already added" in resp.text


async def test_preview_control_names_the_shared_panel(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    assert 'aria-controls="preview-panel"' in resp.text
    assert 'id="preview-panel"' in resp.text


async def test_preview_row_two_rows_carry_two_different_preview_paths(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    paths = re.findall(r'data-preview-path="([^"]+)"', resp.text)
    row_paths = [p for p in paths if "Horimiya - 0" in p]
    assert len(set(row_paths)) >= 2


async def test_preview_row_paths_address_two_distinct_render_requests(web_client, monkeypatch):
    """Each row's own `data-preview-path` is a real, distinct address the
    render route accepts -- the two rows this test reads off the rendered
    screen are provably not the same request once encoded, matching what
    the delegated click handler in static/app.js would send for each.
    """
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
        paths = sorted(set(re.findall(r'data-preview-path="([^"]+)"', resp.text)))
        row_paths = [p for p in paths if "Horimiya - 0" in p]
        assert len(row_paths) >= 2
        encoded = [
            "/preview/" + "/".join(part for part in p.split("/")) for p in row_paths[:2]
        ]
        assert encoded[0] != encoded[1]
        for target in encoded:
            resp2 = await web_client.post(target, data={"subtitle_choice": "none"})
            assert resp2.status_code == 200


async def test_action_row_all_compatible_add_is_primary(web_client, monkeypatch):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    add_tag = resp.text.split('name="add"')[1].split(">")[0]
    assert "btn--primary" in add_tag
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "btn--secondary" in convert_tag


async def test_action_row_one_incompatible_file_makes_convert_primary(
    monkeypatch, db_stub, folder_share_root, _web_env, signed_in
):
    monkeypatch.setattr(
        routes_folder,
        "probe",
        _probe_dispatch(
            {"Mixed - 01.mkv": "compatible.json", "Mixed - 02.mkv": "hevc.json"}
        ),
    )
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        resp = await client.get("/add-folder/Mixed")
    add_tag = resp.text.split('name="add"')[1].split(">")[0]
    assert "btn--secondary" in add_tag
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "btn--primary" in convert_tag
    assert "can&#39;t be added as-is" in resp.text or "can't be added as-is" in resp.text


async def test_not_predisabled_no_button_is_disabled_for_a_reason_a_box_could_change(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    add_tag = resp.text.split('name="add"')[1].split(">")[0]
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" not in add_tag
    assert "disabled" not in convert_tag


# --- The save: POST /add-folder/{path} ------------------------------------


def _horimiya_form(**overrides) -> dict:
    """A minimal, otherwise-valid submission for the three-file Horimiya
    fixture folder -- new title, all three files ticked and numbered, the
    folder's own single audio track chosen, no subtitle burn, 2D. Each
    test overrides only the fields its own scenario cares about.

    A real browser never submits an unchecked checkbox's name at all; an
    override of `""` for an `include_N`/`move_N` field reproduces that
    (the key is dropped from the posted form) rather than sending an
    empty value the route would still read as present.
    """
    form = {
        "group": "0",
        "title_mode": "new",
        "name": "Horimiya",
        "slug": "horimiya",
        "audio_choice": "embedded:0",
        "subtitle_choice": "none",
        "stereo_layout": "2d",
        "include_0": "1",
        "episode_number_0": "1",
        "include_1": "1",
        "episode_number_1": "2",
        "include_2": "1",
        "episode_number_2": "3",
        "add": "1",
    }
    form.update(overrides)
    for key, value in list(form.items()):
        if value == "" and (key.startswith("include_") or key.startswith("move_")):
            del form[key]
    return form


async def test_refusal_when_no_file_is_ticked(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(include_0="", include_1="", include_2="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert "No files selected." in resp.text


async def test_refusal_when_a_ticked_row_has_no_number(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(episode_number_0="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert "Fix the highlighted rows before saving" in resp.text
    assert "No episode number recognized" in resp.text


async def test_refusal_when_two_ticked_rows_share_a_number(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(episode_number_1="1")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert "Fix the highlighted rows before saving" in resp.text
    assert "Episode 1 is used by another checked file." in resp.text


async def test_refusal_when_a_ticked_rows_number_is_already_taken_in_existing_mode(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.title_by_current_slug_result = {
        "title_id": "t1", "title_name": "Horimiya", "title_status": "active",
    }
    db_stub.taken_episode_numbers_result = {1}
    form = _horimiya_form(title_mode="existing", target_title_id="t1")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert "Fix the highlighted rows before saving" in resp.text
    assert "Episode 1 already exists in this title." in resp.text
    assert db_stub.attach_episodes_to_title_calls == []


async def test_refusal_when_existing_mode_names_a_target_that_no_longer_matches(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    # No title_by_current_slug_result configured -- the fresh read finds no
    # match at all, so a submission still claiming "existing" mode names a
    # target this request cannot confirm.
    form = _horimiya_form(title_mode="existing", target_title_id="stale-id")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert "Fix the highlighted rows before saving" in resp.text
    assert db_stub.attach_episodes_to_title_calls == []
    assert db_stub.create_title_with_episodes_calls == []


async def test_refusal_when_a_highlighted_row_has_no_track_choice(
    monkeypatch, db_stub, folder_share_root, _web_env, signed_in
):
    monkeypatch.setattr(
        routes_folder,
        "probe",
        _probe_dispatch({"Mixed - 01.mkv": "compatible.json", "Mixed - 02.mkv": "multitrack.json"}),
    )
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        form = {
            "group": "0",
            "title_mode": "new",
            "name": "Mixed",
            "slug": "mixed",
            "audio_choice": "embedded:0",
            "subtitle_choice": "none",
            "stereo_layout": "2d",
            "include_0": "1", "episode_number_0": "1",
            "include_1": "1", "episode_number_1": "2",
            "add": "1",
        }
        resp = await client.post("/add-folder/Mixed", data=form)
    assert resp.status_code == 200
    assert "Fix the highlighted rows before saving" in resp.text
    assert 'name="audio_fallback_1"' in resp.text


async def test_refusal_when_add_as_is_is_clicked_with_an_incompatible_file_ticked(
    monkeypatch, db_stub, folder_share_root, _web_env, signed_in
):
    monkeypatch.setattr(
        routes_folder,
        "probe",
        _probe_dispatch({"Mixed - 01.mkv": "compatible.json", "Mixed - 02.mkv": "hevc.json"}),
    )
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    ) as client:
        form = {
            "group": "0",
            "title_mode": "new",
            "name": "Mixed",
            "slug": "mixed",
            "audio_choice": "embedded:0",
            "subtitle_choice": "none",
            "stereo_layout": "2d",
            "include_0": "1", "episode_number_0": "1",
            "include_1": "1", "episode_number_1": "2",
            "add": "1",
        }
        resp = await client.post("/add-folder/Mixed", data=form)
    assert resp.status_code == 200
    assert "can&#39;t be added as-is" in resp.text or "can't be added as-is" in resp.text
    assert db_stub.create_title_with_episodes_calls == []


async def test_a_refused_folder_save_writes_no_rows_written_to_the_catalog(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(episode_number_0="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert db_stub.create_title_with_episodes_calls == []
    assert db_stub.attach_episodes_to_title_calls == []
    assert db_stub.reparent_edition_to_episode_calls == []


async def test_a_refused_folder_save_enqueues_no_job(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(episode_number_1="1")  # duplicate -> refused
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert db_stub.enqueue_job_calls == []


async def test_a_refusal_preserves_every_ticked_box(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(include_1="", episode_number_0="")  # row 0 refuses the save
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    # Row 1 was unticked by the operator -- it must come back unticked,
    # not reset to whatever _build_rows freshly recognised (which would
    # tick it, since its own filename carries a number).
    assert "checked" not in resp.text.split('name="include_1"')[1].split(">")[0]
    assert "checked" in resp.text.split('name="include_2"')[1].split(">")[0]


async def test_a_refusal_preserves_typed_numbers(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(episode_number_2="99", episode_number_0="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert 'value="99"' in resp.text.split('name="episode_number_2"')[1].split(">")[0]


async def test_a_refusal_preserves_the_title_mode_and_name(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(name="My Own Name", slug="my-own-slug", episode_number_0="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert 'value="My Own Name"' in resp.text
    assert 'value="my-own-slug"' in resp.text


async def test_the_buttons_are_enabled_again_after_a_refusal(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form(episode_number_0="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    add_tag = resp.text.split('name="add"')[1].split(">")[0]
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" not in add_tag
    assert "disabled" not in convert_tag


async def test_whole_folder_save_creates_one_title_with_the_right_episodes_and_numbers(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=_horimiya_form())
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library"
    assert len(db_stub.create_title_with_episodes_calls) == 1
    call = db_stub.create_title_with_episodes_calls[0]
    assert call["name"] == "Horimiya"
    assert call["slug"] == "horimiya"
    assert sorted(spec["number"] for spec in call["episodes"]) == [1, 2, 3]
    assert db_stub.touch_title_changed_calls == [db_stub.create_title_with_episodes_title_id]


async def test_one_job_per_edition_carries_the_right_kind_and_payload(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        await web_client.post("/add-folder/Horimiya", data=_horimiya_form())
    assert len(db_stub.enqueue_job_calls) == 3
    for job in db_stub.enqueue_job_calls:
        assert job["kind"] == "remux"
        assert job["title_id"] == db_stub.create_title_with_episodes_title_id
    sources = {job["source_path"] for job in db_stub.enqueue_job_calls}
    assert sources == {
        "Horimiya/Horimiya - 01.mkv",
        "Horimiya/Horimiya - 02.mkv",
        "Horimiya/Horimiya - 03.mkv",
    }


async def test_convert_action_enqueues_transcode_jobs(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    form = _horimiya_form()
    del form["add"]
    form["convert"] = "1"
    async with web_client:
        await web_client.post("/add-folder/Horimiya", data=form)
    assert len(db_stub.enqueue_job_calls) == 3
    for job in db_stub.enqueue_job_calls:
        assert job["kind"] == "transcode"
        assert job["subtitle_kind"] == "none"


async def test_whole_folder_attach_to_an_existing_title_does_not_touch_its_own_address(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.title_by_current_slug_result = {
        "title_id": "existing-t1", "title_name": "Horimiya", "title_status": "active",
    }
    form = _horimiya_form(title_mode="existing", target_title_id="existing-t1")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    assert db_stub.create_title_with_episodes_calls == []
    assert len(db_stub.attach_episodes_to_title_calls) == 1
    assert db_stub.attach_episodes_to_title_calls[0]["title_id"] == "existing-t1"
    assert db_stub.touch_title_changed_calls == ["existing-t1"]


async def test_the_save_redirects_to_the_library(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=_horimiya_form())
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library"


# --- The move: an already-added row joins the series ----------------------


def _already_added_row(rel_path: str, *, number: int) -> dict:
    return {
        "source_path": rel_path,
        "title_id": f"old-title-{number}",
        "title_name": "Old Standalone",
        "status": "active",
        "slug": f"old-standalone-{number}",
        "episode_id": f"old-episode-{number}",
        "edition_id": f"old-edition-{number}",
        "number": number,
        "edition_kind": "remux",
    }


async def test_move_ticked_row_calls_reparent_with_the_existing_edition_id(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    assert len(db_stub.reparent_edition_to_episode_calls) == 1
    call = db_stub.reparent_edition_to_episode_calls[0]
    assert call["edition_id"] == "old-edition-1"


async def test_move_object_key_unchanged_since_reparent_reuses_the_existing_edition(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1")
    async with web_client:
        await web_client.post("/add-folder/Horimiya", data=form)
    call = db_stub.reparent_edition_to_episode_calls[0]
    # No object_key argument exists on this call at all -- the move never
    # constructs one, proving structurally that no fresh object is ever
    # named for a moved row.
    assert "object_key" not in call


async def test_move_old_address_argument_matches_the_already_added_slug(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1")
    async with web_client:
        await web_client.post("/add-folder/Horimiya", data=form)
    call = db_stub.reparent_edition_to_episode_calls[0]
    assert call["old_slug"] == "old-standalone-1"
    assert call["old_title_id"] == "old-title-1"
    assert call["old_episode_id"] == "old-episode-1"


async def test_move_enqueues_no_job_for_a_moved_row(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1")
    async with web_client:
        await web_client.post("/add-folder/Horimiya", data=form)
    sources = {job["source_path"] for job in db_stub.enqueue_job_calls}
    assert "Horimiya/Horimiya - 01.mkv" not in sources
    assert len(db_stub.enqueue_job_calls) == 2  # rows 1 and 2 only


async def test_move_leaves_an_unticked_already_added_row_untouched(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="")  # explicitly unticked
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    assert db_stub.reparent_edition_to_episode_calls == []


async def test_move_uses_the_submitted_number_for_the_target_episode(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1", episode_number_0="11")
    async with web_client:
        await web_client.post("/add-folder/Horimiya", data=form)
    call = db_stub.reparent_edition_to_episode_calls[0]
    assert call["number"] == 11


async def test_move_only_folder_creates_the_target_title_with_zero_new_episodes(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [
        _already_added_row("Horimiya/Horimiya - 01.mkv", number=1),
    ]
    form = _horimiya_form(move_0="1", include_1="", include_2="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    assert len(db_stub.create_title_with_episodes_calls) == 1
    assert db_stub.create_title_with_episodes_calls[0]["episodes"] == []
    assert len(db_stub.reparent_edition_to_episode_calls) == 1
    assert db_stub.reparent_edition_to_episode_calls[0]["target_title_id"] == (
        db_stub.create_title_with_episodes_title_id
    )


async def test_move_and_new_files_together_is_one_atomic_transaction(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    # One title created (carrying the two new files), one reparent for the
    # moved file, two jobs (never three) -- new and moved rows land
    # together, from one save.
    assert len(db_stub.create_title_with_episodes_calls) == 1
    assert len(db_stub.create_title_with_episodes_calls[0]["episodes"]) == 2
    assert len(db_stub.reparent_edition_to_episode_calls) == 1
    assert len(db_stub.enqueue_job_calls) == 2


async def test_atomic_refusal_leaves_both_new_and_moved_rows_unwritten(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    # Row 1 and the move both ticked, but row 1's own number collides with
    # the move's -- one bad row refuses the whole save, mixed or not.
    form = _horimiya_form(move_0="1", episode_number_1="1")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    assert db_stub.create_title_with_episodes_calls == []
    assert db_stub.reparent_edition_to_episode_calls == []
    assert db_stub.enqueue_job_calls == []


# --- A whole season gains an edition in one press -------------------------


def _edition_mode_rows(
    title_id: str = "same-title", *, count: int = 3, prefix: str = "Horimiya"
) -> list[dict]:
    return [
        {
            "source_path": f"{prefix}/{prefix} - {n:02d}.mkv",
            "title_id": title_id,
            "title_name": prefix,
            "status": "active",
            "slug": prefix.lower(),
            "episode_id": f"ep{n}",
            "edition_id": f"ed{n}",
            "number": n,
            "edition_kind": "remux",
        }
        for n in range(1, count + 1)
    ]


def _edition_form(**overrides) -> dict:
    """A minimal, otherwise-valid edition-mode submission for the three-
    file Horimiya fixture folder -- every row ticked, the folder's own
    single audio track chosen, no subtitle burn, 2D, as-is. Each test
    overrides only the fields its own scenario cares about, the same
    convention `_horimiya_form` already establishes.
    """
    form = {
        "group": "0",
        "edition_mode": "1",
        "edition_slug": "dub-ru",
        "audio_choice": "embedded:0",
        "subtitle_choice": "none",
        "stereo_layout": "2d",
        "include_0": "1",
        "include_1": "1",
        "include_2": "1",
        "add": "1",
    }
    form.update(overrides)
    for key, value in list(form.items()):
        if value == "" and key.startswith("include_"):
            del form[key]
    return form


async def test_edition_mode_reached_only_when_every_file_resolves_to_one_title(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert resp.status_code == 200
    assert "New edition for these episodes" in resp.text
    assert 'name="edition_mode"' in resp.text


async def test_edition_mode_falls_back_to_ordinary_screen_when_titles_differ(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    rows = _edition_mode_rows()
    rows[1]["title_id"] = "a-different-title"
    db_stub.already_added_rows = rows

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert resp.status_code == 200
    assert 'name="edition_mode"' not in resp.text
    assert "Confirm this series" in resp.text


async def test_edition_mode_removes_season_banner_title_radio_and_name_fields(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert 'name="title_mode"' not in resp.text
    assert 'id="folder-name"' not in resp.text
    assert 'id="folder-slug"' not in resp.text
    assert 'id="edition-slug"' in resp.text
    assert 'name="make_default"' in resp.text


async def test_edition_mode_numbers_render_read_only(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert resp.text.count("readonly") >= 3


async def test_edition_mode_every_row_ticked_by_default(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert resp.text.count("checked") >= 3  # three include boxes, no problem row


async def test_edition_mode_mismatched_audio_row_is_highlighted_and_unticked(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(
        routes_folder,
        "probe",
        _probe_dispatch(
            {
                "Horimiya - 01.mkv": "compatible.json",
                "Horimiya - 02.mkv": "multitrack.json",
                "Horimiya - 03.mkv": "compatible.json",
            }
        ),
    )
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert "folder-file-row--highlighted" in resp.text
    assert "track differs from the folder" in resp.text


async def test_edition_mode_incompatible_row_shows_its_own_reason(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(
        routes_folder,
        "probe",
        _probe_dispatch(
            {
                "Horimiya - 01.mkv": "compatible.json",
                "Horimiya - 02.mkv": "hevc.json",
                "Horimiya - 03.mkv": "compatible.json",
            }
        ),
    )
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    section = re.search(
        r'data-preview-path="Horimiya/Horimiya - 02\.mkv".*?</div>\s*</div>',
        resp.text,
        re.S,
    ).group(0)
    assert "Only H.264 can be added as-is" in section


async def test_edition_mode_slug_taken_row_shows_its_own_reason_and_is_unticked(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()
    db_stub.slug_taken_episode_ids = {"ep2"}

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert "This episode already has an edition" in resp.text


async def test_edition_mode_slug_is_checked_against_every_target_episode_at_render_time(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()
    db_stub.slug_taken_episode_ids = {"ep1", "ep3"}

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    assert resp.text.count("This episode already has an edition") == 2


async def test_edition_mode_default_when_ready_checkbox_off_by_default(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()

    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya?mode=edition")

    make_default_tag = resp.text.split('name="make_default"')[1].split(">")[0]
    assert "checked" not in make_default_tag


async def test_edition_mode_save_writes_one_edition_and_one_job_per_ticked_row(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()
    form = _edition_form(include_1="")  # row 2 (episode 2) unticked

    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)

    assert resp.status_code == 302
    assert len(db_stub.add_edition_to_episode_calls) == 2
    assert len(db_stub.enqueue_job_calls) == 2
    written_episodes = {c["episode_id"] for c in db_stub.add_edition_to_episode_calls}
    assert written_episodes == {"ep1", "ep3"}
    for call in db_stub.add_edition_to_episode_calls:
        assert call["slug"] == "dub-ru"
        assert call["status"] == "preparing"


async def test_edition_mode_default_when_ready_applies_per_episode_not_at_save(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()
    form = _edition_form(make_default="1")

    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)

    assert resp.status_code == 302
    # The pointer request rides on the job, not on the edition write --
    # add_edition_to_episode never touches default_edition_id.
    assert len(db_stub.enqueue_job_calls) == 3
    for call in db_stub.enqueue_job_calls:
        assert call["make_default_when_ready"] is True
    for call in db_stub.add_edition_to_episode_calls:
        assert "default_edition_id" not in call


async def test_edition_mode_eleven_of_twelve_produces_eleven_editions_and_leaves_one_out(
    web_client, monkeypatch, db_stub
):
    probe_map = {f"Season - {n:02d}.mkv": "compatible.json" for n in range(1, 13)}
    monkeypatch.setattr(routes_folder, "probe", _probe_dispatch(probe_map))
    db_stub.already_added_rows = _edition_mode_rows(count=12, prefix="Season")

    form = {
        "group": "0",
        "edition_mode": "1",
        "edition_slug": "dub-ru",
        "audio_choice": "embedded:0",
        "subtitle_choice": "none",
        "stereo_layout": "2d",
        "add": "1",
        **{f"include_{i}": "1" for i in range(11)},  # every row but the twelfth
    }

    async with web_client:
        get_resp = await web_client.get("/add-folder/Season?mode=edition")
        resp = await web_client.post("/add-folder/Season", data=form)

    assert get_resp.status_code == 200
    assert "New edition for these episodes" in get_resp.text
    assert resp.status_code == 302
    assert len(db_stub.add_edition_to_episode_calls) == 11
    assert len(db_stub.enqueue_job_calls) == 11
    written_episodes = {c["episode_id"] for c in db_stub.add_edition_to_episode_calls}
    assert "ep12" not in written_episodes
    assert len(written_episodes) == 11


async def test_edition_mode_refusal_preserves_the_batchs_choices(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = _edition_mode_rows()
    # Nothing ticked at all -- the simplest save-blocked case -- with a
    # hand-typed slug that must survive the re-render.
    form = _edition_form(include_0="", include_1="", include_2="", edition_slug="dub-ru-signed")

    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)

    assert resp.status_code == 200
    assert db_stub.add_edition_to_episode_calls == []
    assert 'value="dub-ru-signed"' in resp.text
    assert "No files selected." in resp.text


# --- A file already in the very series this screen writes into ------------
#
# The wizard is opened a second time on a folder it was already run on,
# because more files have landed in it since. The files from the first pass
# are already episodes of the matched series, and reparenting them into the
# series they are already in is not a move at all -- it used to be offered
# as one, ticked by default, and the save raised out of the database.


def _target_title(title_id: str = "target-t1") -> dict:
    return {"title_id": title_id, "title_name": "Horimiya", "title_status": "active"}


def _row_in_title(rel_path: str, *, number: int, title_id: str) -> dict:
    """An `already_added` row whose edition lives in `title_id` -- the same
    shape `_already_added_row` builds, with the title said out loud because
    that is the whole point of these tests.
    """
    row = _already_added_row(rel_path, number=number)
    row["title_id"] = title_id
    return row


async def test_a_file_already_in_the_matched_title_is_not_a_move_candidate(
    monkeypatch, db_stub
):
    db_stub.title_by_current_slug_result = _target_title()
    db_stub.taken_episode_numbers_result = {1}
    db_stub.already_added_rows = [
        _row_in_title("Horimiya - 01.mkv", number=1, title_id="target-t1")
    ]
    entries = [
        _entry("Horimiya - 01.mkv", "Horimiya - 01.mkv"),
        _entry("Horimiya - 02.mkv", "Horimiya - 02.mkv"),
    ]
    _r, built = await _run_build_rows(
        entries,
        monkeypatch,
        {"Horimiya - 01.mkv": "compatible.json", "Horimiya - 02.mkv": "compatible.json"},
    )
    here_row, new_row = built["rows"]
    assert here_row["already_here"] is True
    assert here_row["moved"] is False
    assert here_row["checked"] is False
    # Its own number is not a collision to report back at it.
    assert here_row["number_taken"] is False
    assert new_row["already_here"] is False


async def test_a_file_added_to_a_different_title_is_still_a_move_candidate(
    monkeypatch, db_stub
):
    db_stub.title_by_current_slug_result = _target_title()
    db_stub.already_added_rows = [
        _row_in_title("Horimiya - 01.mkv", number=1, title_id="some-other-title")
    ]
    entries = [_entry("Horimiya - 01.mkv", "Horimiya - 01.mkv")]
    _r, built = await _run_build_rows(
        entries, monkeypatch, {"Horimiya - 01.mkv": "compatible.json"}
    )
    assert built["rows"][0]["moved"] is True
    assert built["rows"][0]["already_here"] is False


async def test_a_move_whose_number_is_taken_in_the_target_is_reported(monkeypatch, db_stub):
    """The gap that let the crash through: a moved row's number was never
    compared with the target title's own taken numbers, so the collision
    was left for the database to discover.
    """
    db_stub.title_by_current_slug_result = _target_title()
    db_stub.taken_episode_numbers_result = {1}
    db_stub.already_added_rows = [
        _row_in_title("Horimiya - 01.mkv", number=1, title_id="some-other-title")
    ]
    entries = [_entry("Horimiya - 01.mkv", "Horimiya - 01.mkv")]
    _r, built = await _run_build_rows(
        entries, monkeypatch, {"Horimiya - 01.mkv": "compatible.json"}
    )
    assert built["rows"][0]["moved"] is True
    assert built["rows"][0]["number_taken"] is True


async def test_the_row_already_here_offers_no_checkbox_at_all(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.title_by_current_slug_result = _target_title()
    db_stub.taken_episode_numbers_result = {1}
    db_stub.already_added_rows = [
        _row_in_title("Horimiya/Horimiya - 01.mkv", number=1, title_id="target-t1")
    ]
    async with web_client:
        resp = await web_client.get("/add-folder/Horimiya")
    assert resp.status_code == 200
    assert "Already an episode of this series." in resp.text
    # Neither control is rendered for that row: no move offer, and no
    # include box that would sit there doing nothing.
    assert "Move into this series" not in resp.text
    assert 'name="move_0"' not in resp.text
    assert 'name="include_0"' not in resp.text
    # The rows that are genuinely still being decided keep theirs.
    assert 'name="include_1"' in resp.text
    assert 'name="include_2"' in resp.text


async def test_saving_a_folder_whose_row_is_already_here_never_reparents(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.title_by_current_slug_result = _target_title()
    db_stub.taken_episode_numbers_result = {1}
    db_stub.already_added_rows = [
        _row_in_title("Horimiya/Horimiya - 01.mkv", number=1, title_id="target-t1")
    ]
    form = _horimiya_form(
        title_mode="existing", target_title_id="target-t1", include_0="", move_0="1"
    )
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    assert db_stub.reparent_edition_to_episode_calls == []
    # The two genuinely new files land, and only those two.
    assert len(db_stub.attach_episodes_to_title_calls) == 1
    attached = db_stub.attach_episodes_to_title_calls[0]
    assert attached["title_id"] == "target-t1"
    assert {e["number"] for e in attached["episodes"]} == {2, 3}


async def test_unticking_the_include_box_cancels_the_move(web_client, monkeypatch, db_stub):
    """The bypass that did not bypass: the visible left-hand tick came off,
    the row read as excluded, and the move happened anyway.
    """
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    form = _horimiya_form(move_0="1", include_0="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 302
    assert db_stub.reparent_edition_to_episode_calls == []


async def test_the_move_box_comes_back_disabled_when_its_include_box_is_off(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [_already_added_row("Horimiya/Horimiya - 01.mkv", number=1)]
    # Nothing ticked anywhere, so the screen refuses and re-renders with the
    # submission's own state -- the move box among it.
    form = _horimiya_form(move_0="1", include_0="", include_1="", include_2="")
    async with web_client:
        resp = await web_client.post("/add-folder/Horimiya", data=form)
    assert resp.status_code == 200
    move_input = re.search(r'<input[^>]*name="move_0"[^>]*>', resp.text)
    assert move_input is not None
    assert "disabled" in move_input.group(0)


async def test_reparenting_a_title_into_itself_is_refused_by_the_query():
    """The last line of defence, below every screen. The unwind at the end
    of this call deletes the old title, so a title adopting its own edition
    would delete the one it was joining -- and the failure it used to reach
    first was "episode number taken", the edition's own number read back as
    a stranger's. No connection is touched: the guard runs before the
    transaction opens.
    """
    with pytest.raises(ValueError, match="itself"):
        await queries.reparent_edition_to_episode(
            None,
            edition_id="e1",
            old_title_id="t1",
            old_episode_id="ep1",
            old_slug="slug",
            target_title_id="t1",
            number=1,
        )


async def test_the_second_pass_over_a_half_added_folder_adds_only_the_new_files(
    web_client, monkeypatch, db_stub
):
    """The whole failure as it happened: a folder whose first four files
    were added on an earlier pass (episodes 1, 2, 3 and 5), the rest copied
    in afterwards, the wizard opened again on the same folder to pick them
    up. Every already-added file is already in the matched series.
    """
    monkeypatch.setattr(routes_folder, "probe", _probe_returning("compatible.json"))
    db_stub.title_by_current_slug_result = _target_title()
    db_stub.taken_episode_numbers_result = {1, 2, 3, 5}
    db_stub.already_added_rows = [
        _row_in_title(f"Season/Season - {n:02d}.mkv", number=n, title_id="target-t1")
        for n in (1, 2, 3, 5)
    ]

    form = {
        "group": "0",
        "title_mode": "existing",
        "target_title_id": "target-t1",
        "name": "Season",
        "slug": "season",
        "audio_choice": "embedded:0",
        "subtitle_choice": "none",
        "stereo_layout": "2d",
        "add": "1",
    }
    # Twelve files in name order, indexes 0, 1, 2 and 4 being the already-
    # added ones. Those four rows are submitted exactly as the screen used
    # to render them -- include box ticked, "move into this series" ticked
    # too, both on by default -- which is what reached the database as a
    # request for the series to adopt its own episodes.
    for index, number in enumerate(range(1, 13), start=0):
        form[f"episode_number_{index}"] = str(number)
        form[f"include_{index}"] = "1"
        if number in (1, 2, 3, 5):
            form[f"move_{index}"] = "1"

    async with web_client:
        resp = await web_client.post("/add-folder/Season", data=form)

    assert resp.status_code == 302
    assert db_stub.reparent_edition_to_episode_calls == []
    assert db_stub.create_title_with_episodes_calls == []
    assert len(db_stub.attach_episodes_to_title_calls) == 1
    attached = db_stub.attach_episodes_to_title_calls[0]
    assert attached["title_id"] == "target-t1"
    assert {e["number"] for e in attached["episodes"]} == {4, 6, 7, 8, 9, 10, 11, 12}
