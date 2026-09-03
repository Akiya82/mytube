"""Subtitle track enumeration: embedded (D-122) and external (D-121, D-122),
plus the charset ladder external files need. Stdlib only, matching the rest
of this package's rule.

Font collection and cue-timestamp parsing for the preview are not this
module's job -- they arrive with the code that needs them in a later plan
(see `tests/test_subtitles.py`'s own module docstring).
"""
import os
from dataclasses import dataclass
from pathlib import Path

from mytube_media.share import ShareEscape, safe_join

# D-122's split: text tracks go through libass (`subtitles=` filter),
# bitmap tracks through `overlay` -- a different filter chain and its own
# preview path. Mirrors `compat.py`'s own codec-name walk over
# `probe_json["streams"]`.
TEXT_SUBTITLE_CODECS = frozenset({"ass", "ssa", "subrip", "srt", "webvtt"})
BITMAP_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle"})

# What `external_tracks` will treat as a subtitle file at all -- the
# formats this project's own library actually carries (D-122).
SUBTITLE_EXTENSIONS = frozenset({"srt", "ass", "ssa", "vtt"})

# Bounds on the recursive step of `external_tracks` (T-04-11): a share
# folder with a very large tree must not turn drawing the add form into an
# unbounded filesystem crawl. Both caps stop the walk cleanly rather than
# partially -- whichever is hit first ends the search with whatever was
# already found.
MAX_VISITED_DIRECTORIES = 500
MAX_EXTERNAL_RESULTS = 50

# Named folders D-122 checks before falling back to a full recursive walk.
_NAMED_SUBTITLE_FOLDERS = ("Subs", "Subtitles")

_NO_MATCH = object()


class SubtitleReadError(Exception):
    """`read_text` could not decode this file with any encoding in the
    ladder. A typed error, not a raw `UnicodeDecodeError` -- the one
    exception this module raises at all (every other failure here is a
    returned absence or a skipped entry).
    """


@dataclass(frozen=True)
class SubtitleTrack:
    """One subtitle source, whichever of the three kinds it is.

    `stream_index` is set for the two embedded kinds and `None` for
    `external`; `path` is set for `external` (the sidecar file) and
    `None` for either embedded kind. `language`/`title` are `None` --
    never invented -- when the source carries neither a tag nor a
    filename suffix to read one from (this project's own muxer does not
    persist a per-track title, mirrored from `compat.py`'s `AudioTrack`).
    """

    kind: str  # "embedded_text" | "embedded_bitmap" | "external"
    stream_index: int | None = None
    path: str | None = None
    language: str | None = None
    title: str | None = None


def embedded_tracks(probe_json: dict) -> list[SubtitleTrack]:
    """Every subtitle stream, in file order -- the same walk-and-filter
    shape `compat.audio_tracks` already uses, split further by codec name
    into the text kind and the bitmap kind.
    """
    tracks = []
    for stream in probe_json.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        codec = stream.get("codec_name")
        kind = "embedded_bitmap" if codec in BITMAP_SUBTITLE_CODECS else "embedded_text"
        tags = stream.get("tags") or {}
        tracks.append(
            SubtitleTrack(
                kind=kind,
                stream_index=stream.get("index"),
                language=tags.get("language"),
                title=tags.get("title"),
            )
        )
    return tracks


def _match_sidecar(path: Path, stem: str):
    """`None` when the file has no language suffix, a two-letter/three-
    letter code when it does, or the `_NO_MATCH` sentinel when the name
    does not belong to this source at all -- `None` is a real answer here
    and cannot double as "no match".
    """
    prefix = stem + "."
    if not path.name.startswith(prefix):
        return _NO_MATCH
    remainder = path.name[len(prefix):]
    parts = remainder.split(".")
    if len(parts) == 1:
        language, ext = None, parts[0]
    elif len(parts) == 2:
        language, ext = parts
    else:
        return _NO_MATCH
    if ext.lower() not in SUBTITLE_EXTENSIONS:
        return _NO_MATCH
    return language


def external_tracks(source, share_root) -> list[SubtitleTrack]:
    """Sidecar subtitle files for `source`, searched in D-122's order:
    the source's own folder, then `Subs`/`Subtitles` beside it, then
    recursively under the source's folder. Every candidate is resolved
    and contained against `share_root` the same way `share.safe_join`
    guarantees for every other operator-supplied path in this codebase --
    a path that escapes is skipped, never raised, so one stray symlink
    cannot cost the whole list (T-04-01).
    """
    source = Path(source)
    root = Path(share_root)
    stem = source.stem
    results: list[SubtitleTrack] = []
    seen: set[Path] = set()

    def record(candidate: Path, language):
        if len(results) >= MAX_EXTERNAL_RESULTS:
            return
        try:
            rel = candidate.relative_to(root).as_posix()
        except ValueError:
            return
        try:
            resolved = safe_join(root, rel)
        except ShareEscape:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        results.append(SubtitleTrack(kind="external", path=str(resolved), language=language))

    def scan_top_level(directory: Path):
        if len(results) >= MAX_EXTERNAL_RESULTS or not directory.is_dir():
            return
        try:
            names = sorted(p.name for p in directory.iterdir() if p.is_file())
        except OSError:
            return
        for name in names:
            if len(results) >= MAX_EXTERNAL_RESULTS:
                return
            candidate = directory / name
            language = _match_sidecar(candidate, stem)
            if language is _NO_MATCH:
                continue
            record(candidate, language)

    same_folder = source.parent
    scan_top_level(same_folder)
    for folder_name in _NAMED_SUBTITLE_FOLDERS:
        if len(results) >= MAX_EXTERNAL_RESULTS:
            return results
        scan_top_level(same_folder / folder_name)

    directories_visited = 0
    for dirpath, _dirnames, filenames in os.walk(same_folder):
        if len(results) >= MAX_EXTERNAL_RESULTS:
            break
        directories_visited += 1
        if directories_visited > MAX_VISITED_DIRECTORIES:
            break
        directory = Path(dirpath)
        for name in sorted(filenames):
            if len(results) >= MAX_EXTERNAL_RESULTS:
                break
            candidate = directory / name
            language = _match_sidecar(candidate, stem)
            if language is _NO_MATCH:
                continue
            record(candidate, language)

    return results


# Tried in order, each only when the one before it could not decode the
# bytes at all. "utf-8-sig" alone covers both the BOM-marked and the
# plain-UTF-8 case -- the codec strips a leading BOM when present and
# decodes identically to plain UTF-8 when it is not -- so a separate
# unmarked-UTF-8 attempt would never be reached by anything the first one
# rejects; a second entry for it would be dead code, not a safety net.
# The reported name is always "utf-8" for that rung, never "utf-8-sig",
# since the caller (and the burn-in filter's own charset parameter) has
# no use for the distinction. "windows-1251" is the older single-byte
# Cyrillic encoding this project's own library is full of -- spelled the
# way ffmpeg's `-sub_charenc` accepts it, not as the Python codec alias
# ("cp1251"), because that string is passed straight through into the
# filter term.
_ENCODING_LADDER = (
    ("utf-8-sig", "utf-8"),
    ("cp1251", "windows-1251"),
)


def read_text(path) -> tuple[str, str]:
    """The decoded text plus the name of the encoding that decoded it --
    the caller needs both, since a subtitle burned in without its real
    encoding produces mojibake that nothing downstream reports as a
    failure.
    """
    data = Path(path).read_bytes()
    for codec, reported_name in _ENCODING_LADDER:
        try:
            return data.decode(codec), reported_name
        except UnicodeDecodeError:
            continue
    raise SubtitleReadError(f"{path}: could not decode with any known encoding")
