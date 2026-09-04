"""The assertions that only a real ffmpeg process can make (CR-04, CR-05,
CR-06): whether a burn-in with an apostrophe in the subtitle filename
actually opens the file, whether an extracted SubRip track is a container
the muxer can actually write, and whether a hostile attachment name can
put a file outside its own job's fonts directory. Every other test in
this suite asserts the argv string the code builds and never runs the
tool -- which is exactly how the three defects this file exists to catch
survived thirteen plans (04-REVIEW.md).

Every test here is skipped, never errored, when `ffmpeg`/`ffprobe` is not
on `PATH` -- this file is not marked `integration` (that marker means a
Docker daemon in this suite, see `tests/conftest.py`), so it always runs
in the default collection and simply reports a real absence as a skip.
"""
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


# --- CR-06: an attachment cannot name its own destination ----------------


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
