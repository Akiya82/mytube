"""Subtitle track enumeration: embedded (D-122) and external (D-121, D-122),
plus the charset ladder external files need. Also the font collection, the
plain-text house style, cue timing for the preview and the missing-font
notice (D-124, D-126) -- everything `transcode.build_filter_chain`'s
caller needs beyond knowing which tracks exist. Stdlib and the media
tool's own binaries only, matching the rest of this package's rule.

Every function that reads a subtitle file goes through `read_text` and
carries its encoding forward -- a caller that only kept the text half of
that pair would produce a preview and a conversion that both render the
operator's cp1251 subtitles as mojibake.
"""
import os
import re
import shutil
import subprocess
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


class SubtitleExtractError(Exception):
    """`extract_embedded_track` could not pull the stream out to a file --
    the media tool exited non-zero or timed out. Carries only a bounded
    tail of stderr, the same discipline `remux.RemuxError` already keeps.
    """


# collect_fonts (D-124): what a fansub's own font files look like, and how
# much of them this module is willing to copy for one job. A directory
# full of fonts must not be able to fill the scratch space before a
# conversion even starts (T-04-19) -- both caps stop the collection
# cleanly with whatever was already copied.
FONT_EXTENSIONS = frozenset({"ttf", "otf"})
MAX_FONT_FILES = 50
MAX_FONT_BYTES = 20 * 1024 * 1024  # 20 MiB -- generous for a fansub's set
_ATTACHMENT_DUMP_TIMEOUT_SECONDS = 30
_EXTRACT_TIMEOUT_SECONDS = 30

# D-124: the family both images install (`fonts-dejavu-core`, added to the
# interface image in an earlier plan and to the converter image in a
# later one) and one of the few Cyrillic-capable families available as a
# plain package -- the fallback when a plain-text subtitle has no
# collected font of its own to name.
BUNDLED_FONT_FAMILY = "DejaVu Sans"

# The one house style for plain text (D-124): a readable size for a
# full-height frame, an outline, no shadow. `{family}` is filled by the
# caller with a collected font's family name when `collect_fonts` found
# one, or `BUNDLED_FONT_FAMILY` otherwise -- this constant never decides
# that on its own, since it has no access to what a job collected. Applied
# only to plain-text subtitles (SRT has no styling information at all); an
# ASS/SSA track already carries its own design end-to-end and this style
# is never forced onto it.
SRT_FORCE_STYLE = "FontName={family},FontSize=28,Outline=2,Shadow=0"

# The real libass "font substitution" log line, captured against this
# project's own pinned ffmpeg by requesting a font family that does not
# exist and reading the warning it printed:
#   fontselect: (ThisFontDoesNotExistXYZ, 400, 0) -> ArialMT, 0, ArialMT
# The message itself comes from libass's core font-selection code, not
# from a platform-specific font provider, so the shape is stable across
# the DirectWrite provider this dev machine's ffmpeg uses and the
# fontconfig provider the Linux converter image uses.
_FONTSELECT_RE = re.compile(r"fontselect:\s*\(([^,]+),")

# One arrow-separated timestamp line (SubRip/WebVTT) or one ASS/SSA
# Dialogue line's own start time. Two fixed, simple formats -- a handful
# of lines of parsing, no new dependency, matching how this project
# already handles other small fixed formats (D-126).
_SRT_CUE_RE = re.compile(
    r"^(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{3}"
)
_ASS_DIALOGUE_RE = re.compile(r"^Dialogue:\s*[^,]*,(\d{1,2}:\d{2}:\d{2}\.\d{2}),")


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


def subtitle_ordinal(probe_json: dict, stream_index: int) -> int | None:
    """How many subtitle streams come before `stream_index` in the file,
    or `None` when that index is not a subtitle stream at all.

    This is the number the `subtitles=` filter's `si` option is matched
    against -- it counts subtitle streams alone, so the first subtitle
    track is 0 whatever its file index is. The absolute index stays the
    right answer everywhere else (the bitmap track's own `-map 0:{index}`,
    `extract_embedded_track`), which is why both travel together on
    `transcode.SubtitleChoice` instead of one standing in for the other.
    """
    ordinal = 0
    for stream in probe_json.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        if stream.get("index") == stream_index:
            return ordinal
        ordinal += 1
    return None


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


# --- fonts (D-124) ----------------------------------------------------


def _dump_container_font_attachments(source: Path, destination: Path) -> None:
    """Dump every attachment stream in `source`'s own container into
    `destination`, using ffmpeg's built-in `-dump_attachment` rather than
    a second external tool for one feature. Runs unconditionally against
    the source regardless of whether the chosen subtitle track is the
    embedded one or an external file -- a container's font attachments
    exist to serve whichever subtitle track ends up burned in, and
    checking is cheap even when nothing is there to find. A source with no
    attachments, or one ffmpeg cannot read attachments from, is silently a
    no-op: the fansub's loose font files (already collected by the
    caller) are still used, and this module never blocks a conversion
    over a font.
    """
    if not source.is_file():
        return
    destination.mkdir(parents=True, exist_ok=True)
    argv = ["ffmpeg", "-y", "-dump_attachment:t", "", "-i", str(source), "-f", "null", "-"]
    try:
        subprocess.run(
            argv,
            cwd=destination,
            capture_output=True,
            timeout=_ATTACHMENT_DUMP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _enforce_font_caps(destination: Path) -> None:
    """Trim `destination` down to `MAX_FONT_FILES`/`MAX_FONT_BYTES` after
    everything has landed in it (loose copies and dumped attachments
    alike) -- one bound applied once to the whole directory, rather than
    two separate counters that could each stay under their own limit while
    the directory as a whole does not.
    """
    try:
        files = sorted(p for p in destination.iterdir() if p.is_file())
    except OSError:
        return
    total_bytes = 0
    for index, path in enumerate(files):
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        if index >= MAX_FONT_FILES or total_bytes > MAX_FONT_BYTES:
            path.unlink(missing_ok=True)


def collect_fonts(source, subtitle_path, scratch_dir) -> str | None:
    """Build one font directory for one job: `.ttf`/`.otf` files sitting
    beside the subtitle file and beside the source (a fansub ships them as
    loose files -- that is the first place to look), plus the source
    container's own font attachments. Returns the directory, or `None`
    when nothing was found anywhere, so the caller can omit the burn-in's
    `fontsdir` parameter entirely rather than pointing it at an empty
    directory -- what consumes this directory is the subtitle *filter
    term* (`transcode.build_filter_chain`), never a command-line flag.
    """
    destination = Path(scratch_dir) / "fonts"

    def copy_from(directory: Path) -> None:
        if not directory.is_dir():
            return
        try:
            candidates = sorted(p for p in directory.iterdir() if p.is_file())
        except OSError:
            return
        for candidate in candidates:
            if candidate.suffix.lstrip(".").lower() not in FONT_EXTENSIONS:
                continue
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / candidate.name
            if target.exists():
                continue
            try:
                shutil.copy2(candidate, target)
            except OSError:
                continue

    if subtitle_path:
        copy_from(Path(subtitle_path).parent)
    if source:
        source_path = Path(source)
        copy_from(source_path.parent)
        _dump_container_font_attachments(source_path, destination)

    if not destination.is_dir():
        return None

    _enforce_font_caps(destination)

    try:
        found_any = any(destination.iterdir())
    except OSError:
        return None
    return str(destination) if found_any else None


def extract_embedded_track(source, stream_index: int, scratch_dir, *, extension: str = "ass"):
    """Pull one embedded text subtitle stream out to its own file in
    `scratch_dir`, so the burn-in and `cue_times` both work against a real
    file rather than reaching back into the container a second time. A
    short, bounded call with its own timeout, wrapped in this module's own
    `SubtitleExtractError` -- never a raw `CalledProcessError`/
    `TimeoutExpired` escaping this module, the same discipline
    `remux.run_ffmpeg` already keeps.
    """
    destination = Path(scratch_dir) / f"track_{stream_index}.{extension}"
    argv = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-map", f"0:{stream_index}",
        "-c:s", "copy",
        str(destination),
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=_EXTRACT_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise SubtitleExtractError(
            f"ffmpeg timed out extracting stream {stream_index}: {exc}"
        ) from exc

    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
        tail = "\n".join(stderr_text.splitlines()[-10:])
        raise SubtitleExtractError(
            f"ffmpeg exited {result.returncode} extracting stream {stream_index}: {tail}"
        )
    return destination


# --- cue timing (D-126) -------------------------------------------------


def _timestamp_to_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def cue_times(text: str) -> list[float]:
    """Every cue start time found in `text`, in the order encountered.
    Two fixed, simple formats: SubRip/WebVTT's arrow-separated timestamp
    line and ASS/SSA's `Dialogue:` line. Anything that does not match
    either is ignored, never raised -- a malformed cue must not take the
    preview down.
    """
    times: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        match = _SRT_CUE_RE.match(line)
        if match:
            times.append(_timestamp_to_seconds(match.group(1)))
            continue
        match = _ASS_DIALOGUE_RE.match(line)
        if match:
            times.append(_timestamp_to_seconds(match.group(1)))
    return times


def first_cue_after(cues: list[float], offset_seconds: float) -> float | None:
    """The first cue at or after `offset_seconds`, falling back to the
    first cue at all when nothing qualifies, and `None` when there are no
    cues. The offset exists because a fixed early timestamp usually lands
    in silence and proves nothing -- the preview needs a frame where text
    is actually on screen.
    """
    for cue in cues:
        if cue >= offset_seconds:
            return cue
    return cues[0] if cues else None


def bitmap_cue_after(source, stream_index: int, offset_seconds: float) -> float | None:
    """The same question as `first_cue_after`, answered for a bitmap
    track by asking the probe tool for the first packet on that stream at
    or after the offset, rather than parsing anything -- a bitmap track
    has no text to parse.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", str(stream_index),
                "-show_entries", "packet=pts_time",
                "-of", "csv=p=0",
                str(source),
            ],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT_SECONDS,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    times: list[float] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line == "N/A":
            continue
        try:
            times.append(float(line))
        except ValueError:
            continue
    return first_cue_after(times, offset_seconds)


# --- the missing-font notice (D-124) -------------------------------------


def missing_font_from_log(log_text: str) -> str | None:
    """The family name from libass's own "font substitution" complaint in
    a captured error stream, or `None` when the log carries no such line.
    Returns the family name only -- never a slice of the raw log -- since
    what this returns is interpolated into a sentence an operator reads,
    and this feeds a notice beside the preview, never a refusal: a
    substituted font still renders, and blocking a conversion over it
    would be worse than the substitution.
    """
    match = _FONTSELECT_RE.search(log_text)
    if not match:
        return None
    return match.group(1).strip()
