"""A clean checkout must resolve and default to a local share.

Every service declares `env_file: .env`, which Compose resolves against the
project directory, so a render inside the working copy reads the real `.env`
whatever `--env-file` says. These tests therefore render inside a fresh clone
whose `.env` is an unedited copy of `.env.example`.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.integration


def _container_tool() -> str | None:
    """The docker binary, only when its compose plugin answers; else None."""
    tool = shutil.which("docker")
    if tool is None:
        return None
    try:
        subprocess.run(
            [tool, "compose", "version"], capture_output=True, timeout=5, check=True
        )
    except Exception:
        return None
    return tool


@pytest.fixture(scope="module")
def clone(tmp_path_factory) -> Path:
    tool = _container_tool()
    if tool is None:
        pytest.skip("no container tool with a working compose plugin found on PATH")
    if shutil.which("git") is None:
        pytest.skip("git not found on PATH")
    target = tmp_path_factory.mktemp("clean-checkout") / "mytube"
    subprocess.run(
        ["git", "clone", "--quiet", str(REPO_ROOT), str(target)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    shutil.copy(target / ".env.example", target / ".env")
    return target


def _render(clone: Path, *overrides: str) -> subprocess.CompletedProcess:
    command = [_container_tool(), "compose", "-f", "docker-compose.yml"]
    for name in overrides:
        command += ["-f", name]
    command += ["config", "--format", "json"]
    return subprocess.run(
        command, cwd=clone, capture_output=True, text=True, timeout=60
    )


def _api_share_mounts(rendered: dict) -> list[dict]:
    volumes = rendered["services"]["api"].get("volumes", [])
    return [v for v in volumes if v["target"] == "/share"]


def test_default_stack_renders_in_a_clean_checkout_without_cifs(clone):
    result = _render(clone)
    assert result.returncode == 0, result.stderr
    assert "cifs" not in result.stdout.lower()


def test_default_api_share_is_a_single_host_bind_mount(clone):
    rendered = json.loads(_render(clone).stdout)
    mounts = _api_share_mounts(rendered)
    assert len(mounts) == 1
    assert mounts[0]["type"] == "bind"
    assert mounts[0]["read_only"] is True
    assert "share" in mounts[0]["source"]


def test_cifs_override_replaces_the_share_mount_instead_of_adding_one(clone):
    result = _render(clone, "docker-compose.cifs.yml")
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    mounts = _api_share_mounts(rendered)
    assert len(mounts) == 1
    assert mounts[0]["type"] == "volume"
    assert "cifs" in json.dumps(rendered["volumes"]["share"])


def test_only_the_build_override_builds_the_interface_image(clone):
    default = json.loads(_render(clone).stdout)
    assert "build" not in default["services"]["api"]

    result = _render(clone, "docker-compose.build.yml")
    assert result.returncode == 0, result.stderr
    built = json.loads(result.stdout)
    assert "context" in built["services"]["api"]["build"]
