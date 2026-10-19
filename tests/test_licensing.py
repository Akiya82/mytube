"""Tripwires over the repository's licence files.

Whoever copies this project relies on `LICENSE` being the text it claims to
be, and on the package metadata saying the same thing. Assertions about the
licence text bundled inside the built images belong in this module too.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE = REPO_ROOT / "LICENSE"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _license_text() -> str:
    assert LICENSE.is_file(), "the repository root carries no LICENSE file"
    return LICENSE.read_text(encoding="utf-8")


def test_the_licence_is_the_mit_text():
    assert "Permission is hereby granted, free of charge" in _license_text(), (
        "LICENSE must carry the MIT permission grant, otherwise redistribution terms are unclear"
    )


def test_the_licence_names_a_year_and_a_holder():
    assert re.search(r"^Copyright \(c\) \d{4} \S+", _license_text(), re.M), (
        "LICENSE needs a copyright line with a year and a holder"
    )


def test_the_package_metadata_declares_the_same_licence():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["license"]["text"] == "MIT", (
        "pyproject.toml and LICENSE must not drift apart"
    )


DOCKERFILES = {
    "api": REPO_ROOT / "Dockerfile",
    "worker": REPO_ROOT / "Dockerfile.worker",
}
LICENCE_IN_IMAGE = "/usr/share/licenses/ffmpeg/LICENSE.txt"


def _line_of(lines: list[str], needle: str) -> int:
    for number, line in enumerate(lines, start=1):
        if needle in line:
            return number
    raise AssertionError(f"no line contains {needle!r}")


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_ffmpeg_licence_is_copied_before_the_extraction_is_deleted(name: str):
    lines = DOCKERFILES[name].read_text(encoding="utf-8").splitlines()
    copied = _line_of(lines, f"/tmp/ffmpeg-*/LICENSE.txt {LICENCE_IN_IMAGE}")
    removed = _line_of(lines, "rm -rf /tmp/ffmpeg")
    assert copied < removed, "the licence copy after the cleanup copies nothing"


@pytest.mark.integration
@pytest.mark.image_build
@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_built_image_carries_the_ffmpeg_licence(name: str):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("no container tool on this machine")
    tag = f"mytube-licence-test-{name}"
    build = subprocess.run(
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "-t",
            tag,
            "-f",
            DOCKERFILES[name].name,
            ".",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    try:
        assert build.returncode == 0, build.stderr[-2000:]
        run = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--entrypoint",
                "test",
                tag,
                "-f",
                LICENCE_IN_IMAGE,
            ],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, f"{LICENCE_IN_IMAGE} is missing from the image"
    finally:
        subprocess.run([docker, "image", "rm", "-f", tag], capture_output=True)
