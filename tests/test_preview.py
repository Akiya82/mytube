"""Route tests for the on-demand preview (`POST /preview/{path:path}`,
`GET /preview/frame/{token}.jpg`) -- 04-UI-SPEC.md States -> Preview.

The route's own heavy lifting (`extract_embedded_track`, `read_text`,
`collect_fonts`, the render subprocess itself) is monkeypatched at the
`orchestrator.web.routes_preview` module boundary, the same shape
`tests/test_add_form.py` already uses for `routes_share` -- no real
ffmpeg subprocess in most of this file. `cue_times`/`first_cue_after`
are exercised for real (pure functions, no I/O) against hand-built
subtitle text, since the timestamp logic is exactly what this file is
here to prove.
"""
import subprocess

import httpx
import pytest

from mytube_media.compat import ProbeError
from mytube_media.subtitles import SubtitleExtractError
from mytube_media.transcode import (
    SubtitleChoice,
    TranscodePlan,
    build_preview_command,
    build_transcode_command,
)
from orchestrator.config import get_settings
from orchestrator.main import app
from orchestrator.web import routes_preview

from conftest import ADMIN_FQDN, SESSION_COOKIE, WORKER_TOKEN


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
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setenv("MYTUBE_WORK_DIR", str(work_dir))
    get_settings.cache_clear()
    yield work_dir
    get_settings.cache_clear()


class _DummyPool:
    def acquire(self):
        raise AssertionError("the preview route never touches the database")

    async def close(self):
        pass


@pytest.fixture
def web_client(_web_env, signed_in):
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


_PROBE_JSON = {
    "format": {"duration": "120.0"},
    "streams": [
        {"index": 0, "codec_type": "video", "width": 1920, "height": 1080, "sample_aspect_ratio": "1:1"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    ],
}


def _fake_completed(returncode=0, stderr=b""):
    return subprocess.CompletedProcess(args=["ffmpeg"], returncode=returncode, stdout=b"", stderr=stderr)


@pytest.fixture
def render_stubs(monkeypatch, _web_env):
    """Every blocking call the render path makes beyond `probe`, stubbed
    to a fast, deterministic default a test overrides one piece of at a
    time. `subprocess.run` writes the destination file it was asked to
    produce, exactly like a real ffmpeg run creating its output.
    """

    class _Stub:
        probe_json = _PROBE_JSON
        extracted_text = "1\n00:00:05,000 --> 00:00:07,000\nHello\n"
        returncode = 0
        stderr = b""

    stub = _Stub()

    def _probe(path):
        return stub.probe_json

    def _extract_embedded_track(source, stream_index, scratch_dir, extension="ass"):
        dest = scratch_dir / f"track_{stream_index}.srt"
        dest.write_text(stub.extracted_text, encoding="utf-8")
        return dest

    def _read_text(path):
        from pathlib import Path

        return Path(path).read_text(encoding="utf-8"), "utf-8"

    def _collect_fonts(source, subtitle_path, scratch_dir):
        return None

    def _run(cmd, capture_output, timeout, check):
        # The last argument build_preview_command appends is the
        # destination path.
        dest = cmd[-1]
        from pathlib import Path

        if stub.returncode == 0:
            Path(dest).write_bytes(b"\xff\xd8\xff\xd9")  # a minimal JPEG-shaped stub
        return _fake_completed(returncode=stub.returncode, stderr=stub.stderr)

    monkeypatch.setattr(routes_preview, "probe", _probe)
    monkeypatch.setattr(routes_preview, "extract_embedded_track", _extract_embedded_track)
    monkeypatch.setattr(routes_preview, "read_text", _read_text)
    monkeypatch.setattr(routes_preview, "collect_fonts", _collect_fonts)
    monkeypatch.setattr(routes_preview.subprocess, "run", _run)
    return stub


# --- containment ------------------------------------------------------


async def test_source_path_escaping_the_share_root_is_refused(web_client):
    async with web_client:
        resp = await web_client.post(
            "/preview/..%2F..%2Fetc%2Fpasswd",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "Could not render a preview" in resp.text


async def test_manual_subtitle_path_escaping_the_share_root_is_refused(
    web_client, render_stubs
):
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={
                "subtitle_choice": "manual",
                "subtitle_manual_path": "../../../../etc/passwd",
                "stereo_layout": "2d",
            },
        )
    assert resp.status_code == 200
    assert "Could not render a preview" in resp.text


# --- the stereo refusal (D-140) ----------------------------------------


async def test_a_stereo_layout_refuses_the_burn_in_and_answers_the_note(
    web_client, render_stubs
):
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "sbs_full"},
        )
    assert resp.status_code == 200
    assert "subtitles burned in during conversion" in resp.text
    assert "<img" not in resp.text


async def test_no_subtitle_choice_renders_nothing_to_preview(web_client, render_stubs):
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "none", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "<img" not in resp.text


# --- a successful render -------------------------------------------------


async def test_rendered_fragment_carries_an_image_address_and_a_timestamp(
    web_client, render_stubs
):
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "<img" in resp.text
    assert "/preview/frame/" in resp.text
    assert 'data-frame-seconds="5.0"' in resp.text


async def test_the_timestamp_used_is_at_or_after_the_requested_time_when_a_later_cue_exists(
    web_client, render_stubs
):
    render_stubs.extracted_text = (
        "1\n00:00:02,000 --> 00:00:03,000\nToo early\n\n"
        "2\n00:00:09,000 --> 00:00:10,000\nJust right\n"
    )
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d", "after": "5"},
        )
    assert 'data-frame-seconds="9.0"' in resp.text


async def test_the_timestamp_falls_back_to_the_first_cue_when_none_qualify(
    web_client, render_stubs
):
    render_stubs.extracted_text = "1\n00:00:01,000 --> 00:00:02,000\nOnly cue\n"
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d", "after": "50"},
        )
    assert 'data-frame-seconds="1.0"' in resp.text


# --- the font-missing notice (D-124) -------------------------------------


async def test_font_notice_appears_when_the_tool_output_names_a_missing_family(
    web_client, render_stubs
):
    render_stubs.stderr = (
        b"[Parsed_subtitles_0 @ 0x0] fontselect: "
        b"(ThisFontDoesNotExistXYZ, 400, 0) -> ArialMT, 0, ArialMT\n"
    )
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert "ThisFontDoesNotExistXYZ" in resp.text
    assert "<img" in resp.text  # non-blocking: the frame still rendered


async def test_no_font_notice_when_the_tool_output_names_nothing_missing(
    web_client, render_stubs
):
    render_stubs.stderr = b"frame=1 fps=0.0 q=-1.0 Lsize=N/A time=00:00:00.04\n"
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert "font_missing" not in resp.text
    assert "not found, a substitute was used" not in resp.text


# --- a failed render -------------------------------------------------------


async def test_a_failed_render_answers_the_error_state_convert_stays_possible(
    web_client, render_stubs
):
    render_stubs.returncode = 1
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "Could not render a preview" in resp.text
    assert "<img" not in resp.text


async def test_a_probe_failure_answers_the_error_state(web_client, render_stubs, monkeypatch):
    def _raise(path):
        raise ProbeError("simulated")

    monkeypatch.setattr(routes_preview, "probe", _raise)
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "Could not render a preview" in resp.text


async def test_an_embedded_stream_index_the_probe_never_reported_is_refused(
    web_client, render_stubs
):
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:99", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "Could not render a preview" in resp.text


async def test_an_extraction_failure_answers_the_error_state(
    web_client, render_stubs, monkeypatch
):
    def _raise(source, stream_index, scratch_dir, extension="ass"):
        raise SubtitleExtractError("simulated extraction failure")

    monkeypatch.setattr(routes_preview, "extract_embedded_track", _raise)
    async with web_client:
        resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
    assert resp.status_code == 200
    assert "Could not render a preview" in resp.text


# --- the frame route ------------------------------------------------------


async def test_frame_route_refuses_a_token_that_is_not_the_generated_shape(web_client):
    async with web_client:
        resp = await web_client.get("/preview/frame/../../etc/passwd.jpg")
    assert resp.status_code == 404


async def test_frame_route_refuses_a_well_shaped_token_with_no_file(web_client):
    async with web_client:
        resp = await web_client.get("/preview/frame/aValidLookingToken123456.jpg")
    assert resp.status_code == 404


async def test_frame_route_serves_a_frame_that_was_actually_rendered(
    web_client, render_stubs
):
    async with web_client:
        render_resp = await web_client.post(
            "/preview/a-video.mp4",
            data={"subtitle_choice": "embedded:2", "stereo_layout": "2d"},
        )
        image_url = render_resp.text.split('src="')[1].split('"')[0]
        frame_resp = await web_client.get(image_url)

    assert frame_resp.status_code == 200
    assert frame_resp.headers["content-type"] == "image/jpeg"


# --- preview/conversion identity (CONV-04) ---------------------------------


def test_the_preview_and_the_conversion_command_share_the_same_filter_value():
    """The structural half of CONV-04's promise: for the same plan and the
    same subtitle choice, the preview and the conversion commands carry
    the identical filter graph -- proven here directly against
    `mytube_media.transcode`, the module both `build_preview_command` and
    `build_transcode_command` draw their one shared `build_filter_chain`
    call from (already unit-tested in tests/test_transcode.py; this is
    the route-adjacent restatement the plan's own acceptance criteria
    ask for).
    """
    plan = TranscodePlan(
        width=1920,
        height=1080,
        sample_aspect_ratio="1:1",
        video_bitrate_bps=6_000_000,
        maxrate_bps=7_980_000,
        bufsize_bps=12_000_000,
        gop=50,
        hwaccel=None,
        audio_stream_index=1,
        audio_channels=2,
        reencode_audio=False,
        stereo_layout="2d",
    )
    subtitle = SubtitleChoice(
        kind="embedded_text", stream_index=2, subtitle_ordinal=0, path="/share/a-video.mp4"
    )

    preview_cmd = build_preview_command(
        "/share/a-video.mp4", "/work/frame.jpg", plan=plan, subtitle=subtitle, at_seconds=5.0
    )
    transcode_cmd = build_transcode_command(
        "/share/a-video.mp4", "/work/out.mp4", plan=plan, subtitle=subtitle
    )

    def _filter_value(cmd):
        flag_index = cmd.index("-vf") if "-vf" in cmd else cmd.index("-filter_complex")
        return cmd[flag_index + 1]

    assert _filter_value(preview_cmd) == _filter_value(transcode_cmd)
