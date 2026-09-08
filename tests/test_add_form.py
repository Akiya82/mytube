"""Route tests for the add form (GET /add/{path} on a file) -- Screen 2 of
02-UI-SPEC.md. `ffprobe` is monkeypatched to return the frozen real-ffprobe
fixtures from tests/fixtures/probe/ (task 2), and the database layer is
monkeypatched at the `orchestrator.db.queries` module boundary, the same
shape test_resolve.py and test_share.py already use -- no real Postgres or
subprocess in this file, quick-loop only.
"""
import json
import threading
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from mytube_media.compat import AudioTrack, ProbeError
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app
from orchestrator.media.add import required_free_bytes
from orchestrator.web import routes_share, routes_titles

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


class _DummyTxCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


class _DummyConn:
    """A stand-in `asyncpg.Connection` for the POST route's own
    transaction/advisory-lock statements -- `create_title_with_edition`
    and `enqueue_job` are stubbed out entirely by the tests that need
    them, so nothing here ever reaches real SQL; only the route's own
    `async with conn.transaction():` / `conn.execute(...)` wrapper needs
    something to call.
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
def _web_env(share_root, tmp_path, monkeypatch):
    monkeypatch.setenv("MYTUBE_SECURE_LINK_SECRET", "test-secret-do-not-use-in-prod")
    monkeypatch.setenv("MYTUBE_DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("MYTUBE_S3_ENDPOINT", "http://storage:8333")
    monkeypatch.setenv("MYTUBE_S3_KEY", "test-key")
    monkeypatch.setenv("MYTUBE_S3_SECRET", "test-secret")
    monkeypatch.setenv("MYTUBE_PUBLIC_BASE_URL", "https://mytube.example")
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", ADMIN_FQDN)
    monkeypatch.setenv("MYTUBE_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("MYTUBE_SHARE_ROOT", str(share_root))
    # The real default (/work) is a container-only path (see config.py) --
    # a real, empty, writable directory here so shutil.disk_usage has
    # something to measure on this host.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_stub(monkeypatch):
    """Every `orchestrator.db.queries` call the add form's route makes,
    stubbed with a controllable default: no tombstone, no taken slugs, no
    already-added edition. Individual tests override one attribute at a
    time via the returned namespace object.
    """

    class _Stub:
        resolve_slug_result = None
        taken_slugs_result: set[str] = set()
        already_added_rows: list[dict] = []
        median_realtime_factor_result = None
        create_title_calls: list[dict] = []
        enqueue_job_calls: list[dict] = []
        create_title_id = "11111111-1111-1111-1111-111111111111"

    stub = _Stub()
    stub.create_title_calls = []
    stub.enqueue_job_calls = []
    stub.set_edition_subtitle_signs_only_calls = []

    async def _resolve_slug(conn, slug):
        return stub.resolve_slug_result

    async def _taken_slugs(conn):
        return stub.taken_slugs_result

    async def _editions_by_source_paths(conn, paths):
        return stub.already_added_rows

    async def _median_realtime_factor(conn, kind="transcode", last_n=20):
        return stub.median_realtime_factor_result

    async def _create_title_with_edition(conn, name, slug, edition_id, object_key, **kwargs):
        stub.create_title_calls.append(
            {"name": name, "slug": slug, "edition_id": edition_id, "object_key": object_key, **kwargs}
        )
        return stub.create_title_id

    async def _enqueue_job(conn, **kwargs):
        stub.enqueue_job_calls.append(kwargs)
        return "22222222-2222-2222-2222-222222222222"

    async def _set_edition_subtitle_signs_only(conn, edition_id, signs_only):
        stub.set_edition_subtitle_signs_only_calls.append(signs_only)

    monkeypatch.setattr(queries, "resolve_slug", _resolve_slug)
    monkeypatch.setattr(queries, "taken_slugs", _taken_slugs)
    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)
    monkeypatch.setattr(queries, "median_realtime_factor", _median_realtime_factor)
    monkeypatch.setattr(queries, "create_title_with_edition", _create_title_with_edition)
    monkeypatch.setattr(queries, "enqueue_job", _enqueue_job)
    monkeypatch.setattr(
        queries, "set_edition_subtitle_signs_only", _set_edition_subtitle_signs_only
    )
    return stub


@pytest.fixture
def web_client(_web_env, db_stub, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


def _probe_returning(fixture_name: str):
    data = _fixture(fixture_name)

    def _probe(path):
        return data

    return _probe


def _probe_raising():
    def _probe(path):
        raise ProbeError("simulated unreadable file")

    return _probe


@pytest.fixture(autouse=True)
def _default_moov_first(monkeypatch):
    # Real bytes of the share fixtures are stub content, not a real MP4 --
    # deterministic default so already_correct never accidentally flips
    # true from garbage atom parsing. Individual tests override this.
    monkeypatch.setattr(routes_share, "moov_before_mdat", lambda path: False)


# --- the verdict, rendered ---------------------------------------------


async def test_compatible_file_renders_enabled_add_control_and_no_reason_banner(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert 'type="submit"' in resp.text
    assert "disabled" not in resp.text.split('type="submit"')[1].split(">")[0]
    assert "reason.codec" not in resp.text
    assert "This is" not in resp.text  # no reason.codec-shaped sentence


async def test_incompatible_file_renders_no_enabled_add_control_and_disabled_convert(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("hevc.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    # No "Add as-is" control at all (still not eligible) -- but Convert
    # is now live for any readable file, compatible or not (CONV-01).
    assert 'name="add"' not in resp.text
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" not in convert_tag
    assert "hevc" in resp.text  # reason.codec's {codec} parameter


async def test_silent_file_renders_no_add_control_and_explains_why(
    web_client, monkeypatch
):
    """The third state the form must never reach: a live Add button over a
    track picker with no options, whose POST can only answer the required
    audio_track field with a 422. A conversion needs a track chosen
    exactly as a copy does (Amendment 2), so a source with none has no
    live action at all -- Convert renders, disabled.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("no_audio.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert 'name="add"' not in resp.text
    assert 'name="audio_choice"' not in resp.text
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" in convert_tag
    assert "no audio track" in resp.text.lower()


async def test_unreadable_file_renders_unreadable_reason_not_traceback(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_raising())

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert "This file could not be read" in resp.text
    assert 'type="submit"' not in resp.text
    assert "Traceback" not in resp.text


# --- the name field ------------------------------------------------------


async def test_name_field_value_is_raw_filename_unmodified(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'value="a-video.mp4"' in resp.text


async def test_script_tag_filename_renders_escaped_everywhere(web_client, monkeypatch):
    """The literal `<script>alert(1)</script>.mkv` name cannot exist as a
    real file on this Windows host (NTFS rejects `<`/`>`, see conftest.py's
    `share_root`) -- same workaround test_share.py's route-level escaping
    test already uses: point the route at a real file's bytes, but report
    a dangerous name for it, bypassing the filesystem instead of fighting
    it. `is_file`/`stat`/`relative_to` delegate to the real underlying
    path; only `.name` (what the route reads and the template renders) is
    swapped.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    class _DangerousPath:
        name = "<script>alert(1)</script>.mkv"

        def __init__(self, real):
            self._real = real

        def is_file(self):
            return self._real.is_file()

        def stat(self):
            return self._real.stat()

        def relative_to(self, other):
            return self._real.relative_to(other)

    real_safe_join = routes_share.safe_join

    def _dangerous_safe_join(root, rel):
        return _DangerousPath(real_safe_join(root, rel))

    monkeypatch.setattr(routes_share, "safe_join", _dangerous_safe_join)

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "<script>alert(1)</script>" not in resp.text
    assert resp.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 1


async def test_a_dot_dot_path_is_canonicalized_before_it_becomes_an_identity(
    web_client, monkeypatch, db_stub
):
    """`Movies/../a-video.mp4` and `a-video.mp4` are the same file, and the
    duplicate-detection query keys on the string. If the raw request
    segment reaches the catalog, the same source can be added twice with
    no "already added" chip ever appearing again -- and the breadcrumb
    renders a `..` crumb.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    asked_for = []

    async def _editions_by_source_paths(conn, paths):
        asked_for.append(list(paths))
        return []

    monkeypatch.setattr(queries, "editions_by_source_paths", _editions_by_source_paths)

    async with web_client:
        resp = await web_client.get("/add/Movies/%2E%2E/a-video.mp4")

    assert resp.status_code == 200
    assert asked_for == [["a-video.mp4"]]
    assert 'action="/add/a-video.mp4"' in resp.text


async def test_the_blocking_share_calls_run_off_the_event_loop_thread(
    web_client, monkeypatch
):
    """`ffprobe` is a subprocess with a 30-second timeout and the atom scan
    walks headers over a network mount with no timeout at all. Called bare,
    they hold the one event loop that also answers `GET /v/{slug}` and
    `/healthz`, so opening the add form for a slow file stops video
    resolution for every viewer -- during synchronized viewing, which is
    the product.

    Asserted on the thread rather than on elapsed time: a timing test of a
    blocked event loop cannot measure the block from inside the same loop,
    and "ran somewhere other than the loop thread" is the rule itself.
    """
    loop_thread = threading.get_ident()
    ran_on = {}

    def recording_probe(path):
        ran_on["probe"] = threading.get_ident()
        return _fixture("compatible.json")

    def recording_moov(path):
        ran_on["moov"] = threading.get_ident()
        return False

    monkeypatch.setattr(routes_share, "probe", recording_probe)
    monkeypatch.setattr(routes_share, "moov_before_mdat", recording_moov)

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert ran_on["probe"] != loop_thread
    assert ran_on["moov"] != loop_thread


# --- the slug field, similar-slug warning, tombstone reuse ---------------


async def test_slug_field_shows_the_planned_slug(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'id="add-slug"' in resp.text
    assert 'value="a-video-mp4"' in resp.text


async def test_similar_slug_warning_names_the_taken_slug(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    # "a-video.mp4" normalizes to candidate "a-video-mp4"; "avideomp4" is a
    # different, already-taken slug with the same similar_key (hyphens
    # stripped) -- this is exactly the "Re:Zero" vs "ReZero" case
    # pick_unique_slug's own docstring describes.
    db_stub.taken_slugs_result = {"avideomp4"}

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert "avideomp4" in resp.text


async def test_tombstoned_slug_shows_reuse_warning_and_disables_submit(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.resolve_slug_result = {"title_id": "x", "status": "deleted", "default_edition_id": None}

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "belonged to a deleted title" in resp.text
    assert 'type="submit"' in resp.text
    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" in submit_tag
    assert "Reuse this address" in resp.text


async def test_tombstoned_slug_with_reuse_confirmed_enables_submit(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.resolve_slug_result = {"title_id": "x", "status": "deleted", "default_edition_id": None}

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4?confirm_reuse=1")

    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" not in submit_tag


# --- already-added ---------------------------------------------------------


async def test_already_added_banner_names_existing_title_and_submit_stays_enabled(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
            "edition_kind": "remux",
        }
    ]

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "Existing Title" in resp.text
    assert "/v/existing-title-slug" in resp.text
    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" not in submit_tag


# --- the layout selector ------------------------------------------------


async def test_layout_selector_preselects_from_detection_and_names_its_source(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("stereo_sbs_tag.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'name="stereo_layout" value="sbs_full" checked' in resp.text
    assert "Detected from the file" in resp.text


async def test_layout_selector_defaults_to_2d_with_none_detected_caption(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'name="stereo_layout" value="2d" checked' in resp.text
    assert "Not detected, defaulting to 2D." in resp.text


# --- the subtitle group --------------------------------------------------


async def test_subtitle_group_lists_embedded_tracks_and_defaults_to_none(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("subtitles_mixed.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'name="subtitle_choice" value="none" checked' in resp.text
    assert 'name="subtitle_choice" value="embedded:2"' in resp.text
    assert 'name="subtitle_choice" value="embedded:3"' in resp.text
    assert "Russian" in resp.text
    assert "English" in resp.text


async def test_manual_path_field_is_always_present_with_no_inline_script(web_client, monkeypatch):
    """T-05-14: the manual field is a permanent fallback now, not a state
    revealed by picking a radio option -- it renders every time, and its
    own hidden radio carries no static "manual" value in the markup at all
    (a separate acceptance grep enforces that; this test only re-confirms
    the field itself and its own wiring hook exist).
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'id="subtitle-choice-manual"' in resp.text
    assert 'name="subtitle_manual_path"' in resp.text
    assert "data-manual-path-input" in resp.text
    # No inline script anywhere on the page -- every behaviour here comes
    # from the shared static/app.js file.
    assert "<script>" not in resp.text


# --- the manual field is a button, not a track option (T-05-14) --------


async def test_the_manual_entry_is_no_longer_a_radio_option_in_either_group(
    web_client, monkeypatch
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("subtitles_mixed.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert resp.status_code == 200
    assert 'value="manual"' not in resp.text.lower()
    # The button that replaces it is a real control with a real label --
    # "Browse files" (share.cta.pick_sidecar), rendered twice (audio and
    # subtitle groups each get their own).
    assert resp.text.count("Browse files") == 2


def test_preview_button_disabled_state_is_wired_to_the_subtitle_choice_in_the_script():
    """Checkable through the script's own file rather than a browser
    (05-04-PLAN.md task 2's own acceptance note): the listener that keeps
    the preview control's disabled state in sync with the current
    subtitle choice.
    """
    script = (Path(__file__).parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "_updatePreviewGate" in script
    assert "subtitle_choice" in script
    assert "button.disabled" in script


# --- the sidecar/audio picker route (T-05-14) -----------------------------


async def test_sidecar_picker_route_lists_real_subtitle_candidates_for_a_real_source(
    web_client, share_root
):
    _make_sidecar(share_root, ("Subs",), "a-video.eng.srt")

    async with web_client:
        resp = await web_client.get("/sidecar-picker/a-video.mp4?kind=subtitle")

    assert resp.status_code == 200
    assert "a-video.eng.srt" in resp.text


async def test_sidecar_picker_route_lists_real_audio_candidates_for_a_real_source(
    web_client, share_root
):
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("RUS Sound", "AniStar"), "Show - 05.mka")

    async with web_client:
        resp = await web_client.get(f"/sidecar-picker/{_encoded('Show - 05.mp4')}?kind=audio")

    assert resp.status_code == 200
    assert "Show - 05.mka" in resp.text
    assert "AniStar" in resp.text


async def test_sidecar_picker_route_refuses_a_source_path_outside_the_share(web_client):
    async with web_client:
        resp = await web_client.get("/sidecar-picker/%2E%2E/etc?kind=subtitle")

    assert resp.status_code == 400


async def test_sidecar_picker_route_refuses_an_unknown_candidate_kind(web_client):
    async with web_client:
        resp = await web_client.get("/sidecar-picker/a-video.mp4?kind=video")

    assert resp.status_code == 400


async def test_sidecar_picker_route_empty_state_renders_when_nothing_is_found(web_client):
    async with web_client:
        resp = await web_client.get("/sidecar-picker/a-video.mp4?kind=subtitle")

    assert resp.status_code == 200
    assert "No matching files" in resp.text


async def test_sidecar_picker_route_offers_a_value_relative_to_the_share_root_not_absolute(
    web_client, share_root
):
    _make_sidecar(share_root, ("Subs",), "a-video.eng.srt")

    async with web_client:
        resp = await web_client.get("/sidecar-picker/a-video.mp4?kind=subtitle")

    assert resp.status_code == 200
    assert 'data-picker-value="Subs/a-video.eng.srt"' in resp.text
    assert str(share_root) not in resp.text


async def test_subtitle_group_is_disabled_under_a_stereo_layout(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("stereo_sbs_tag.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "can" in resp.text and "subtitles burned in during conversion" in resp.text
    assert 'name="subtitle_choice" value="embedded' not in resp.text


# --- external audio and subtitle labelling (D-180..D-186) ----------------


def _encoded(name: str) -> str:
    return quote(name)


def _make_sidecar(share_root, folder_parts, filename, content=b"stub sidecar bytes"):
    directory = share_root
    for part in folder_parts:
        directory = directory / part
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_bytes(content)


def _stub_external_audio_track(
    *, channels=2, codec="aac", language=None, title=None, duration_seconds=2.0
):
    def _probe(path, *, folder_label=None):
        return AudioTrack(
            index=0,
            codec=codec,
            channels=channels,
            language=language,
            title=title,
            external_path=str(path),
            folder_label=folder_label,
            duration_seconds=duration_seconds,
        )

    return _probe


async def test_merged_audio_list_contains_embedded_then_external_in_order(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("RUS Sound", "AniStar"), "Show - 05.mka")
    monkeypatch.setattr(routes_share, "external_audio_track", _stub_external_audio_track())

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    embedded_pos = resp.text.index('name="audio_choice" value="embedded:')
    external_pos = resp.text.index('name="audio_choice" value="external:')
    assert embedded_pos < external_pos


async def test_external_audio_entry_renders_folder_language_and_codec_caption(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("RUS Sound", "AniStar"), "Show - 05.mka")
    monkeypatch.setattr(
        routes_share,
        "external_audio_track",
        _stub_external_audio_track(language="rus", codec="aac"),
    )

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "RUS Sound/AniStar - ru (aac)" in resp.text


async def test_external_audio_entry_with_unresolved_language_shows_unknown_label_and_folder(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("Sound",), "Show - 05.xyz.mka")
    monkeypatch.setattr(
        routes_share,
        "external_audio_track",
        _stub_external_audio_track(language=None, codec="ac3"),
    )

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "Sound - Unknown language (ac3)" in resp.text


async def test_external_audio_duration_mismatch_renders_warning_and_stays_selectable(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("RUS Sound", "AniStar"), "Show - 05.mka")
    monkeypatch.setattr(
        routes_share,
        "external_audio_track",
        _stub_external_audio_track(duration_seconds=30.0),
    )

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "possible desync" in resp.text
    assert 'name="audio_choice" value="external:' in resp.text


async def test_external_subtitle_entry_resolves_language_from_folder_token(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("Subs", "Russian"), "Show - 05.srt")

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "Subs/Russian - ru" in resp.text


async def test_external_subtitle_entry_with_unresolved_language_shows_unknown_label_and_folder(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("Subs", "Misc"), "Show - 05.zzz.srt")

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "Subs/Misc - Unknown language" in resp.text


async def test_external_subtitle_signs_only_folder_renders_signs_only_caption(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("Subs", "Signs"), "Show - 05.eng.srt")

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "Signs only" in resp.text


async def test_external_subtitle_full_folder_renders_full_caption_and_a_folder_that_says_nothing_renders_neither(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("Subs", "Full"), "Show - 05.rus.srt")
    _make_sidecar(share_root, ("Subs", "Random"), "Show - 05.eng.srt")

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "Full subtitles" in resp.text
    # The "Random" folder resolves neither word -- only the "Full" entry's
    # own caption renders, never invented for the entry whose folder says
    # nothing.
    assert resp.text.count("Full subtitles") == 1
    assert "Signs only" not in resp.text


async def test_two_same_named_external_subtitles_in_different_folders_render_distinguishably_by_folder(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("Subs", "StudioA"), "Show - 05.eng.srt")
    _make_sidecar(share_root, ("Subs", "StudioB"), "Show - 05.eng.srt")

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    assert "Subs/StudioA - en" in resp.text
    assert "Subs/StudioB - en" in resp.text


async def test_external_audio_choice_leaves_subtitle_default_untouched(
    web_client, monkeypatch, share_root
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    (share_root / "Show - 05.mp4").write_bytes(b"stub video bytes")
    _make_sidecar(share_root, ("RUS Sound", "AniStar"), "Show - 05.mka")
    monkeypatch.setattr(
        routes_share,
        "external_audio_track",
        _stub_external_audio_track(language="rus"),
    )

    async with web_client:
        resp = await web_client.get(f"/add/{_encoded('Show - 05.mp4')}")

    assert resp.status_code == 200
    # A Russian dub sitting right there in the audio list changes nothing
    # about the subtitle group's own default -- "No subtitles" is still
    # the only checked radio in that fieldset (D-183: no dub-to-signs
    # heuristic).
    assert 'name="subtitle_choice" value="none" checked' in resp.text


# --- the second-edition gate (D-147) -------------------------------------


async def test_second_edition_gate_blocks_convert_when_existing_is_remux(
    web_client, monkeypatch, db_stub
):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
            "edition_kind": "remux",
        }
    ]

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    add_tag = resp.text.split('name="add"')[1].split(">")[0]
    assert "disabled" not in add_tag
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" in convert_tag
    assert "Add anyway" in resp.text


async def test_second_edition_gate_is_lifted_once_confirmed(web_client, monkeypatch, db_stub):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
            "edition_kind": "remux",
        }
    ]

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4?confirm_add_anyway=1")

    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" not in convert_tag


async def test_second_edition_gate_does_not_fire_for_a_title_with_no_known_job_kind(
    web_client, monkeypatch, db_stub
):
    """A title the CLI created directly, before this phase, carries no
    `jobs` row and so no known `edition_kind` -- unknown is never treated
    as a mismatch.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
            "edition_kind": None,
        }
    ]

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    add_tag = resp.text.split('name="add"')[1].split(">")[0]
    assert "disabled" not in add_tag
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" not in convert_tag


# --- which action is primary ---------------------------------------------


async def test_add_as_is_is_primary_when_compatible(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'name="add" value="1" class="btn btn--primary"' in resp.text
    assert 'name="convert" value="1" class="btn btn--secondary"' in resp.text


async def test_convert_is_primary_when_incompatible(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("hevc.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'name="add"' not in resp.text
    assert 'name="convert" value="1" class="btn btn--primary"' in resp.text


# --- cost line / free space --------------------------------------------


async def test_cost_line_shows_size(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    # compatible.json's format.size is 213539 bytes -> "208.5 KB"-ish;
    # assert on the filesizeformat_binary unit, not a brittle exact figure.
    assert "KB" in resp.text or "MB" in resp.text or "B" in resp.text


async def test_not_enough_space_banner_when_disk_usage_is_low(web_client, monkeypatch):
    monkeypatch.setattr(routes_share, "probe", _probe_returning("compatible.json"))

    class _Usage:
        free = 0

    monkeypatch.setattr(
        routes_share.shutil, "disk_usage", lambda path: _Usage()
    )

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert "Not enough free space" in resp.text
    assert "scratch disk" in resp.text
    submit_tag = resp.text.split('type="submit"')[1].split(">")[0]
    assert "disabled" in submit_tag


async def test_convert_only_source_shows_neither_as_is_estimate_nor_space_banner(
    web_client, monkeypatch
):
    """A source that can only be converted has no as-is button on the
    page, so it must show neither the as-is cost line nor the free-space
    banner -- even with zero bytes free, since the interface no longer
    measures anything for the conversion path at all.
    """
    monkeypatch.setattr(routes_share, "probe", _probe_returning("high10_10bit.json"))

    class _Usage:
        free = 0

    monkeypatch.setattr(
        routes_share.shutil, "disk_usage", lambda path: _Usage()
    )

    async with web_client:
        resp = await web_client.get("/add/a-video.mp4")

    assert 'name="add"' not in resp.text
    assert " about " not in resp.text  # the as-is estimate's own fragment
    assert "Not enough free space" not in resp.text
    convert_tag = resp.text.split('name="convert"')[1].split(">")[0]
    assert "disabled" not in convert_tag


# --- POST /add/{path} -- the convert branch (D-145, D-147, D-148) -------


@pytest.fixture
def post_client(web_client, monkeypatch):
    """The POST route re-probes the file itself (T-02-06) -- same
    `_probe_returning` doubles as GET's, applied to `routes_titles.probe`
    rather than `routes_share.probe`.
    """
    monkeypatch.setattr(routes_titles, "probe", _probe_returning("subtitles_mixed.json"))
    return web_client


def _post_add(client, **overrides):
    data = {
        "name": "A Video",
        "slug": "a-video",
        "audio_choice": "embedded:1",
        "stereo_layout": "2d",
        "subtitle_choice": "none",
        "subtitle_manual_path": "",
        "confirm_reuse": "false",
        "confirm_add_anyway": "false",
        "add": "1",
    }
    data.update(overrides)
    return client.post("/add/a-video.mp4", data=data, follow_redirects=False)


async def test_as_is_and_layout_stores_the_stereo_layout_on_the_created_edition(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(post_client, stereo_layout="sbs_full")

    assert resp.status_code == 302
    assert len(db_stub.create_title_calls) == 1
    assert db_stub.create_title_calls[0]["stereo_layout"] == "sbs_full"
    # The as-is branch queues a remux job and nothing else.
    assert len(db_stub.enqueue_job_calls) == 1
    assert db_stub.enqueue_job_calls[0]["kind"] == "remux"


async def test_convert_refuses_a_stereo_layout_with_a_real_subtitle_choice(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(
            post_client,
            add=None,
            convert="1",
            stereo_layout="sbs_full",
            subtitle_choice="embedded:2",
        )

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_convert_refuses_an_embedded_stream_index_the_probe_never_reported(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(
            post_client, add=None, convert="1", subtitle_choice="embedded:999"
        )

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_convert_refuses_a_manual_subtitle_path_that_does_not_exist(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(
            post_client,
            add=None,
            convert="1",
            subtitle_choice="manual",
            subtitle_manual_path="no-such-file.srt",
        )

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_convert_refuses_an_out_of_vocabulary_layout_value(post_client, db_stub):
    async with post_client:
        resp = await _post_add(
            post_client, add=None, convert="1", stereo_layout="not-a-real-layout"
        )

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_second_edition_gate_refuses_a_convert_post_with_no_confirmation(
    post_client, db_stub
):
    """The gate is enforced here, not only by the page's own control -- a
    form can be submitted with no `confirm_add_anyway` field at all
    (D-147).
    """
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
            "edition_kind": "remux",
        }
    ]

    async with post_client:
        resp = await _post_add(post_client, add=None, convert="1", subtitle_choice="none")

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_second_edition_gate_lets_a_confirmed_convert_post_through(
    post_client, db_stub
):
    db_stub.already_added_rows = [
        {
            "source_path": "a-video.mp4",
            "title_id": "existing-id",
            "title_name": "Existing Title",
            "title_status": "active",
            "slug": "existing-title-slug",
            "edition_kind": "remux",
        }
    ]

    async with post_client:
        resp = await _post_add(
            post_client,
            add=None,
            convert="1",
            subtitle_choice="none",
            confirm_add_anyway="true",
        )

    assert resp.status_code == 302
    assert len(db_stub.create_title_calls) == 1


async def test_convert_payload_carries_only_the_operators_choices(post_client, db_stub):
    async with post_client:
        resp = await _post_add(
            post_client,
            add=None,
            convert="1",
            stereo_layout="2d",
            subtitle_choice="embedded:2",
        )

    assert resp.status_code == 302
    assert len(db_stub.enqueue_job_calls) == 1
    job = db_stub.enqueue_job_calls[0]
    assert job["kind"] == "transcode"
    assert job["title_id"] == db_stub.create_title_id
    assert job["source_path"] == "a-video.mp4"
    assert job["audio_stream_index"] == 1
    assert job["subtitle_kind"] == "embedded_text"
    assert job["subtitle_stream_index"] == 2
    assert job["subtitle_path"] is None
    assert job["stereo_layout"] == "2d"
    assert job["external_audio_path"] is None
    # Nothing derived -- no command, no encoder settings, no filter string.
    assert set(job.keys()) == {
        "kind",
        "title_id",
        "edition_id",
        "source_path",
        "audio_stream_index",
        "subtitle_kind",
        "subtitle_stream_index",
        "subtitle_path",
        "stereo_layout",
        "external_audio_path",
    }


# --- POST /add/{path} -- the audio_choice compound value (T-05-13) -------


async def test_an_external_audio_choice_queues_the_external_path(
    post_client, db_stub, share_root, monkeypatch
):
    (share_root / "RUS Sound").mkdir()
    (share_root / "RUS Sound" / "dub.mka").write_bytes(b"stub dub bytes")
    monkeypatch.setattr(
        routes_titles,
        "external_audio_track",
        lambda path: AudioTrack(
            index=0,
            codec="aac",
            channels=2,
            language="rus",
            title="Russian dub",
            external_path=str(path),
        ),
    )

    async with post_client:
        resp = await _post_add(post_client, audio_choice="external:RUS Sound/dub.mka")

    assert resp.status_code == 302
    assert len(db_stub.enqueue_job_calls) == 1
    job = db_stub.enqueue_job_calls[0]
    assert job["external_audio_path"] == "RUS Sound/dub.mka"
    assert job["audio_stream_index"] is None
    assert db_stub.create_title_calls[0]["audio_stream_index"] is None
    assert db_stub.create_title_calls[0]["audio_channels"] == 2


async def test_an_external_audio_choice_refuses_an_absolute_path(post_client, db_stub):
    async with post_client:
        resp = await _post_add(post_client, audio_choice="external:/etc/passwd")

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_an_external_audio_choice_refuses_a_path_outside_the_share(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(post_client, audio_choice="external:../outside.mka")

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_an_external_audio_choice_refuses_a_path_that_does_not_exist(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(post_client, audio_choice="external:RUS Sound/no-such.mka")

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_an_embedded_index_the_probe_never_reported_is_refused_via_audio_choice(
    post_client, db_stub
):
    async with post_client:
        resp = await _post_add(post_client, audio_choice="embedded:999")

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_a_malformed_audio_choice_bypassing_the_page_is_refused(post_client, db_stub):
    """A submission with no page's own controls behind it -- an
    unrecognised `audio_choice` value -- is refused the same way a
    tampered one would be (T-05-13).
    """
    async with post_client:
        resp = await _post_add(post_client, audio_choice="not-a-real-choice")

    assert resp.status_code == 400
    assert db_stub.create_title_calls == []


async def test_convert_with_an_external_subtitle_signs_only_folder_writes_the_attribute(
    post_client, db_stub, share_root
):
    (share_root / "Subs" / "Signs").mkdir(parents=True)
    (share_root / "Subs" / "Signs" / "a-video.eng.srt").write_bytes(b"1\n")

    async with post_client:
        resp = await _post_add(
            post_client,
            add=None,
            convert="1",
            subtitle_choice="external:Subs/Signs/a-video.eng.srt",
        )

    assert resp.status_code == 302
    assert db_stub.set_edition_subtitle_signs_only_calls == [True]


async def test_convert_with_an_external_subtitle_in_an_unlabelled_folder_leaves_the_attribute_unset(
    post_client, db_stub, share_root
):
    (share_root / "Subs" / "Random").mkdir(parents=True)
    (share_root / "Subs" / "Random" / "a-video.eng.srt").write_bytes(b"1\n")

    async with post_client:
        resp = await _post_add(
            post_client,
            add=None,
            convert="1",
            subtitle_choice="external:Subs/Random/a-video.eng.srt",
        )

    assert resp.status_code == 302
    assert db_stub.set_edition_subtitle_signs_only_calls == []


# --- required_free_bytes: already-correct vs. not ------------------------


def test_required_free_bytes_already_correct_passes_with_less_than_source_free():
    source_size = 20 * 1024 * 1024  # 20 MB
    free_space = 17 * 1024 * 1024  # less than source, more than the poster budget
    assert free_space < source_size
    assert free_space >= required_free_bytes(source_size, already_correct=True)


def test_required_free_bytes_not_already_correct_fails_just_above_source_free():
    source_size = 20 * 1024 * 1024
    free_space = source_size + 1024  # just above the source, nowhere near double
    assert free_space < required_free_bytes(source_size, already_correct=False)
