#!/usr/bin/env python3
"""Machine check for faststart: moov must come before mdat in an MP4.

Without +faststart, moov sits at the end of the file, and a player
downloads the whole file before it can show the first frame.

Thin wrapper: the actual logic (`orchestrator.media.compat.moov_before_mdat`)
lives there instead, because it is also needed inside the running container
(the "already good, skip ffmpeg" decision is made at runtime), not only
here, during development. Two call sites, one piece of logic -- duplicating
it would be a trap waiting to spring.

This script still has to run under the host's own python, which has no
project virtual environment and no `orchestrator` package installed --
`tools/make_control_clip.sh` and `tools/make_error_clips.sh` call it exactly
that way. So the repository root is added to sys.path by hand before the
import, and `orchestrator.media.compat` deliberately pulls in nothing
third-party at module level (see its own docstring).

Usage: check_faststart.py <mp4_path>
Exit code: 0 if moov comes before mdat, 1 otherwise (or on a read error).
"""
import sys
from pathlib import Path

# The host's python cannot see the orchestrator package (it is installed
# editable only inside the venv/image) -- add the repository root to
# sys.path before the import instead of relying on PYTHONPATH or a real
# package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.media.compat import moov_before_mdat  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("usage: check_faststart.py <mp4_path>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    try:
        ok = moov_before_mdat(path)
    except OSError as exc:
        print(f"ERROR: could not read '{path}': {exc}", file=sys.stderr)
        return 1

    if not ok:
        print(
            "ERROR: moov does not come before mdat (or one of them is "
            "missing from the top-level atoms) -- the file is not "
            "faststart. A player will download the whole file before the "
            "first frame. Re-encode with -movflags +faststart.",
            file=sys.stderr,
        )
        return 1

    print("OK: moov comes before mdat (faststart confirmed)")
    return 0


def demo():
    """Self-check against the real `compat.moov_before_mdat`, on two
    minimal atom tables this function writes itself -- one faststart, one
    not.

    It used to compare two hardcoded integers (`assert 1 < 2`) and import
    nothing from the module it claimed to be checking. An assertion that
    cannot fail is worse than no assertion, because it reads as coverage.
    """
    import struct
    import tempfile

    def atom_file(order):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            for atom_type in order:
                # An 8-byte header declaring an 8-byte box: header only,
                # no payload. read_top_level_atoms needs nothing more.
                f.write(struct.pack(">I4s", 8, atom_type))
            return Path(f.name)

    faststart = atom_file([b"ftyp", b"moov", b"mdat"])
    not_faststart = atom_file([b"ftyp", b"mdat", b"moov"])
    no_moov = atom_file([b"ftyp", b"mdat"])
    try:
        assert moov_before_mdat(faststart)
        assert not moov_before_mdat(not_faststart)
        assert not moov_before_mdat(no_moov)
    finally:
        for path in (faststart, not_faststart, no_moov):
            path.unlink()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        demo()
        print("self-test OK")
        sys.exit(0)
    sys.exit(main())
