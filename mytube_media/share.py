"""Path containment and a single cheap directory listing for the mounted
network share.

Every route that turns a browser-chosen path segment into a filesystem call
goes through `safe_join` first: resolve, then require the result
sits under the mount root. That is the whole control -- a blacklist that
hunts for a parent-directory marker is the known-wrong answer here, since it
misses percent-encodings, symlinks, and platform quirks that resolve-then-
contain handles for free, including a symlink inside the root that points
outside it (the check runs after resolution, not before). `list_dir` runs
the same containment check per entry and skips what fails it, so merely
seeing such a link never costs the operator the whole folder.

`list_dir` does not filter by file extension. An allowlist would quietly
hide a container the operator knows is a video, which is worse than showing
a subtitle file that later says, honestly, that it could not be read (this screen opens
instantly with names and sizes only, not a verdict on every file). Names are returned exactly as the filesystem
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


def relative_to_root(root: Path, target: Path) -> str:
    """The canonical share-relative path of a target `safe_join` already
    validated -- the one spelling of a file, whatever spelling the request
    used to reach it.

    Identity comparisons key on this string: `list_dir` builds entry paths
    the same canonical way, and an edition's stored `source_path` is what
    the "already added" chip and the duplicate-detection query match
    against. Storing the raw request segment instead lets
    `Movies/../a.mkv` and `a.mkv` name the same file under two different
    strings, and the duplicate is never noticed again.

    The root itself relativizes to `.`, which is not a path segment --
    returned as the empty string, the same value the routes use for "the
    top of the share".
    """
    rel = target.relative_to(root.resolve()).as_posix()
    return "" if rel == "." else rel


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
        base_rel = Path(path).resolve().relative_to(root_resolved)
        with os.scandir(path) as it:
            for entry in it:
                try:
                    Path(entry.path).resolve().relative_to(root_resolved)
                    st = entry.stat()
                except (ValueError, OSError):
                    # One entry the listing cannot show, never the folder.
                    # ValueError: a symlink whose target sits outside the
                    # mount -- not reachable through safe_join anyway, so a
                    # row for it could only refuse to open. OSError: an
                    # entry that cannot be stat'ed, which over NFS is most
                    # often a dangling symlink the storage server left at
                    # the root (`incomplete -> /incomplete`); stat() follows
                    # the link, and the handler below would otherwise read
                    # that one missing target as the whole share being gone.
                    continue
                # Built from the entry name, never from the resolved
                # target: a symlink pointing elsewhere inside the root has
                # to link to where it appears, or the row navigates to a
                # different file than the one it names and the
                # already-added lookup keys on the wrong path.
                rel_path = (base_rel / entry.name).as_posix()
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
