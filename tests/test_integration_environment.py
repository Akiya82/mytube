"""Tripwire for the strict integration mode.

A runner without Docker or a full ffmpeg would otherwise skip hundreds of
tests and still report green. These tests run pytest in a subprocess against
a throwaway test file and a copy of the suite's conftest, with PATH emptied
to fake an inadequate machine, and check both directions of the switch.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).with_name("conftest.py")

TINY_TEST = """
import pytest

@pytest.mark.integration
def test_needs_docker():
    pass
"""


def _run_pytest(tmp_path, strict):
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_tiny.py").write_text(TINY_TEST, encoding="utf-8")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {**os.environ, "PATH": str(empty_bin)}
    env.pop("MYTUBE_REQUIRE_INTEGRATION", None)
    if strict:
        env["MYTUBE_REQUIRE_INTEGRATION"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_strict_mode_fails_the_session_and_names_what_is_missing(tmp_path):
    result = _run_pytest(tmp_path, strict=True)
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"strict mode passed on an empty PATH:\n{output}"
    assert "Docker daemon" in output, f"Docker not named:\n{output}"
    assert "ffmpeg on PATH" in output, f"ffmpeg not named:\n{output}"
    assert "ffprobe on PATH" in output, f"ffprobe not named:\n{output}"


def test_without_strict_mode_the_same_environment_skips_cleanly(tmp_path):
    result = _run_pytest(tmp_path, strict=False)
    output = result.stdout + result.stderr

    assert result.returncode == 0, f"non-strict run failed:\n{output}"
    assert "1 skipped" in output, f"integration test was not skipped:\n{output}"
