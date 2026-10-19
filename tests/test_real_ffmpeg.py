"""The assertions that only a real ffmpeg process can make:
whether a burn-in with an apostrophe in the subtitle filename actually
opens the file, whether an extracted SubRip track is a container
the muxer can actually write, and whether a hostile attachment name can
put a file outside its own job's fonts directory. Every other test in
this suite asserts the argv string the code builds and never runs the
tool -- which is exactly how the three defects this file exists to catch
survived every earlier round of work.

Every test here is skipped, never errored, when `ffmpeg`/`ffprobe` is not
on `PATH` -- this file is not marked `integration` (that marker means a
Docker daemon in this suite, see `tests/conftest.py`), so it always runs
in the default collection and simply reports a real absence as a skip.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)

_TIMEOUT_SECONDS = 30


def _run(argv, cwd=None):
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
    )


# --- The burn-in actually opens a filename with an apostrophe ------------


def test_a_real_burn_in_opens_a_subtitle_filename_with_an_apostrophe(tmp_path):
    """Runs with `cwd` at the temporary directory and names the subtitle
    by a bare relative path -- an absolute `tmp_path` on this platform
    puts a drive letter and its own backslashes through the escaper,
    which is not the character this test exists to prove.
    """
    from mytube_media.transcode import SubtitleChoice, TranscodePlan, build_filter_chain

    srt_name = "it's a test.srt"
    (tmp_path / srt_name).write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8"
    )

    plan = TranscodePlan(
        width=320, height=240, sample_aspect_ratio="1:1",
        video_bitrate_bps=500_000, maxrate_bps=650_000, bufsize_bps=1_000_000,
        gop=50, hwaccel=None, audio_stream_index=0, audio_channels=2,
        reencode_audio=False, stereo_layout="2d",
    )
    subtitle = SubtitleChoice(kind="external", path=srt_name)
    graph = build_filter_chain(plan, subtitle)

    argv = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1",
        graph.flag, graph.value,
        "-frames:v", "1", "-update", "1",
        "out.jpg",
    ]
    result = _run(argv, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    output = tmp_path / "out.jpg"
    assert output.is_file() and output.stat().st_size > 0


# --- A SubRip track extracts to a container the muxer accepts ------------


def test_a_real_subrip_track_extracts_to_a_non_empty_file(tmp_path):
    from mytube_media.subtitles import extract_embedded_track, extraction_extension

    srt_path = tmp_path / "sub.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

    container = tmp_path / "container.mkv"
    mux_argv = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
        "-i", str(srt_path),
        "-c:s:0", "srt",
        "-map", "0:v", "-map", "1:s",
        str(container),
    ]
    mux_result = _run(mux_argv)
    assert mux_result.returncode == 0, mux_result.stderr

    probe_argv = [
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
        str(container),
    ]
    probe_result = _run(probe_argv)
    assert probe_result.returncode == 0, probe_result.stderr

    probe_json = json.loads(probe_result.stdout)
    subtitle_stream = next(
        s for s in probe_json["streams"] if s.get("codec_type") == "subtitle"
    )
    extension = extraction_extension(probe_json, subtitle_stream["index"])
    assert extension == "srt"

    destination = extract_embedded_track(
        container, subtitle_stream["index"], tmp_path, extension=extension
    )

    assert destination.is_file()
    assert destination.stat().st_size > 0


# --- An attachment cannot name its own destination -----------------------


def test_collect_fonts_never_writes_outside_its_scratch_directory_for_a_hostile_attachment_name(
    tmp_path,
):
    from mytube_media.subtitles import collect_fonts

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    font_path = build_dir / "innocuous.ttf"
    font_path.write_bytes(b"not a real font, but bytes are enough for a dump test")

    container_dir = tmp_path / "share"
    container_dir.mkdir()
    container = container_dir / "movie.mkv"
    mux_argv = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
        "-attach", str(font_path),
        "-metadata:s:t:0", "filename=../escaped.ttf",
        "-metadata:s:t:0", "mimetype=application/x-truetype-font",
        str(container),
    ]
    mux_result = _run(mux_argv)
    assert mux_result.returncode == 0, mux_result.stderr

    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = collect_fonts(source=str(container), subtitle_path=None, scratch_dir=str(scratch))

    assert result is not None
    fonts_dir = Path(result)
    assert fonts_dir == scratch / "fonts"
    assert any(fonts_dir.iterdir())
    # the whole point: nothing landed in the share directory (the
    # attachment's parent, per its own claimed name) or anywhere else
    # outside the scratch directory this call was given.
    assert sorted(p.name for p in container_dir.iterdir()) == ["movie.mkv"]
    assert not (tmp_path / "escaped.ttf").exists()
