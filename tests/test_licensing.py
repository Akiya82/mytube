"""Tripwires over the repository's licence files.

Whoever copies this project relies on `LICENSE` being the text it claims to
be, and on the package metadata saying the same thing. Assertions about the
licence text bundled inside the built images belong in this module too.
"""

import re
import tomllib
from pathlib import Path

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
