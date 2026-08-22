"""Path containment and a single cheap directory listing for the mounted
network share (D-54 through D-56).

Every route that turns a browser-chosen path segment into a filesystem call
goes through `safe_join` first (T-02-01): resolve, then require the result
sits under the mount root. That is the whole control -- a blacklist that
hunts for a parent-directory marker is the known-wrong answer here, since it
misses percent-encodings, symlinks, and platform quirks that resolve-then-
contain handles for free, including a symlink inside the root that points
outside it (the check runs after resolution, not before).

`list_dir` does not filter by file extension. An allowlist would quietly
hide a container the operator knows is a video, which is worse than showing
a subtitle file that later says, honestly, that it could not be read (D-56
already decided that this screen opens instantly with names and sizes only,
not a verdict on every file). Names are returned exactly as the filesystem
has them -- this module sanitizes nothing; the template that renders them
is what escapes.
"""
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ShareEscape(Exception):
    """The requested path resolves outside the mount root."""


class ShareUnavailable(Exception):
    """The mount is not readable -- the state the interface renders as
    "cannot reach the share".
    """


@dataclass(frozen=True)
class ShareEntry:
    name: str
    is_dir: bool
    size: int
    mtime: float
    rel_path: str


def safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` (a forward-slashed, browser-supplied path segment)
    against `root` and require the result to stay inside it.

    `rel` is rejected before any joining happens if it is itself absolute
    or drive-qualified -- joining an absolute segment onto a base path
    silently discards the base entirely (`Path("/a") / "/b"` is `/b`), so
    the rejection has to happen on the raw string, not on the join result.
    """
    if rel:
        candidate = PurePosixPath(rel)
        if candidate.is_absolute():
            raise ShareEscape(f"{rel!r} is an absolute path, not a share-relative one")
        first_part = candidate.parts[0] if candidate.parts else ""
        if len(first_part) == 2 and first_part[1] == ":":
            raise ShareEscape(
                f"{rel!r} looks drive-qualified, not a share-relative path"
            )

    root_resolved = root.resolve()
    joined = (root_resolved / rel) if rel else root_resolved
    resolved = joined.resolve()

    if not resolved.is_relative_to(root_resolved):
        raise ShareEscape(f"{rel!r} resolves outside the mount root")

    return resolved


def list_dir(path: Path, root: Path) -> list[ShareEntry]:
    """One directory scan over `path`, directories before files, each group
    sorted case-insensitively by name.

    `DirEntry.stat()` reuses the stat data the directory read already
    produced on most filesystems, including CIFS mounts under Linux -- a
    designed advantage over listing names and then calling `stat()` a
    second time per entry, which would double the round trips over a
    network mount.
    """
    root_resolved = root.resolve()
    dirs: list[ShareEntry] = []
    files: list[ShareEntry] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                st = entry.stat()
                entry_path = Path(entry.path).resolve()
                rel_path = entry_path.relative_to(root_resolved).as_posix()
                item = ShareEntry(
                    name=entry.name,
                    is_dir=entry.is_dir(),
                    size=st.st_size,
                    mtime=st.st_mtime,
                    rel_path=rel_path,
                )
                (dirs if item.is_dir else files).append(item)
    except OSError as exc:
        raise ShareUnavailable(f"cannot list {path}: {exc}") from exc

    dirs.sort(key=lambda e: e.name.casefold())
    files.sort(key=lambda e: e.name.casefold())
    return dirs + files
