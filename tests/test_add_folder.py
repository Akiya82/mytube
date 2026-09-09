"""Route tests for the folder confirmation screen (`GET /add-folder/{path}`)
-- 05-UI-SPEC.md Screen A/B. Follows `test_add_form.py`'s own shape: no
real Postgres or subprocess, `ffprobe` and the database layer both
monkeypatched at their module boundary.
"""
import json
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
def folder_share_root(tmp_path):
    """A season folder with three episodes, plus a subfolder holding a
    fourth video that must never be listed or counted -- the direct-
    files-only rule (D-166/D-167) this whole screen rests on.
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
    return root


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
    class _Stub:
        already_added_rows: list[dict] = []
        resolve_slug_result = None
        taken_slugs_result: set = set()
        title_by_current_slug_result = None
        taken_episode_numbers_result: set = set()

    stub = _Stub()
    stub.already_added_rows = []
    stub.taken_slugs_result = set()
    stub.taken_episode_numbers_result = set()

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

    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)
    monkeypatch.setattr(queries, "resolve_slug", _resolve_slug)
    monkeypatch.setattr(queries, "taken_slugs", _taken_slugs)
    monkeypatch.setattr(queries, "title_by_current_slug", _title_by_current_slug)
    monkeypatch.setattr(queries, "taken_episode_numbers", _taken_episode_numbers)
    return stub


@pytest.fixture
def web_client(_web_env, db_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


# --- Task 1: the button inside the opened folder -------------------------


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
    assert "mode=edition" in resp.text


async def test_plain_label_shows_when_files_resolve_to_several_titles(web_client, db_stub):
    db_stub.already_added_rows = [
        {"source_path": "Horimiya/Horimiya - 01.mkv", "title_id": "title-a"},
        {"source_path": "Horimiya/Horimiya - 02.mkv", "title_id": "title-b"},
    ]

    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add folder as series" in resp.text
    assert "mode=edition" not in resp.text


async def test_plain_label_shows_when_no_files_are_already_added(web_client):
    async with web_client:
        resp = await web_client.get("/add/Horimiya")

    assert "Add folder as series" in resp.text


async def test_folder_route_refuses_a_path_outside_the_share(web_client, monkeypatch):
    """A literal `../` traversal cannot be exercised through httpx/ASGI --
    both normalize dot segments client-side before the request is ever
    sent (see 02-02-PLAN.md's own note on this). `safe_join` is the real
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


# --- Task 2: recognising and probing a whole folder -----------------------


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
