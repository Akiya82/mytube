"""The converter's own deployment surfaces -- the compose file that feeds
its container and the images it runs in -- rather than its runtime
behavior, which the other `test_worker_*` files already cover.

Two properties are asserted here, both read straight from the files
rather than from a running container:

1. The converter's compose service names none of the service's own
   secrets and every variable `WorkerSettings` declares, so a leak on the
   GPU host cannot hand out anything wider than the converter's own
   narrow storage credential (see `docker-compose.worker.yml`'s own
   comment for the reasoning).
2. Both `Dockerfile` and `Dockerfile.worker` switch to a non-root user
   before the entrypoint runs, so an ffmpeg invocation over untrusted
   media never runs as uid 0.

No YAML parser is added for this: the project's own dependency set does
not already carry one (`pyproject.toml`), so the compose file's
`environment:` block is found with a plain line scan instead -- exactly
the fallback the project rule prefers over a new dependency for one
test.
"""
import re
from pathlib import Path

from worker.settings import WorkerSettings

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_WORKER = REPO_ROOT / "docker-compose.worker.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERFILE_WORKER = REPO_ROOT / "Dockerfile.worker"

# The service's own secrets -- none of these may reach the converter's
# container. Checked against the raw file text, not just parsed keys, so
# a leak through a stray env_file: line or a second environment: block
# is caught the same way.
FORBIDDEN_SERVICE_SECRETS = (
    "MYTUBE_S3_KEY",
    "MYTUBE_S3_SECRET",
    "MYTUBE_DATABASE_URL",
    "MYTUBE_SECURE_LINK_SECRET",
    "MYTUBE_SHARE_PASSWORD",
)

# Fields whose defaults are container-internal filesystem paths, already
# fixed by the image (`RUN mkdir -p /work`) and by this compose file's own
# volume mounts (`:/share:ro`, `:/work`) -- not a deployment-facing choice
# an operator overrides, so the compose service does not name them.
_PATH_FIELDS_FIXED_BY_MOUNTS = frozenset({"share_root", "work_dir"})


def _worker_service_environment_block(text: str) -> str:
    """The lines of the `worker:` service's `environment:` mapping, found
    by indentation rather than a YAML parse -- there is exactly one such
    block in this file.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "environment:")
    block_indent = len(lines[start]) - len(lines[start].lstrip(" "))
    block = []
    for line in lines[start + 1 :]:
        if line.strip() == "":
            block.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= block_indent:
            break
        block.append(line)
    return "\n".join(block)


def _environment_keys(block: str) -> set[str]:
    keys = set()
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*):", stripped)
        if match:
            keys.add(match.group(1))
    return keys


def test_worker_compose_names_no_service_secret():
    text = COMPOSE_WORKER.read_text(encoding="utf-8")
    offenders = [name for name in FORBIDDEN_SERVICE_SECRETS if name in text]
    assert not offenders, offenders


def test_worker_compose_no_longer_inherits_an_env_file():
    text = COMPOSE_WORKER.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "env_file" not in body


def test_worker_compose_names_every_setting_the_class_declares():
    text = COMPOSE_WORKER.read_text(encoding="utf-8")
    block = _worker_service_environment_block(text)
    keys = _environment_keys(block)

    expected = {
        f"MYTUBE_{field.upper()}"
        for field in WorkerSettings.model_fields
        if field not in _PATH_FIELDS_FIXED_BY_MOUNTS
    }
    missing = sorted(expected - keys)
    assert not missing, missing


def test_worker_compose_environment_block_actually_has_entries():
    # A block-finder that silently matched nothing would make the test
    # above pass by finding no keys to be missing from -- confirm it
    # actually reaches real entries before trusting that result.
    text = COMPOSE_WORKER.read_text(encoding="utf-8")
    block = _worker_service_environment_block(text)
    assert len(_environment_keys(block)) > 5


def _user_directives(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("USER ")
    ]


def test_dockerfile_runs_as_a_non_root_user():
    directives = _user_directives(DOCKERFILE)
    assert directives == ["USER mytube"]


def test_dockerfile_worker_runs_as_a_non_root_user():
    directives = _user_directives(DOCKERFILE_WORKER)
    assert directives == ["USER mytube"]
