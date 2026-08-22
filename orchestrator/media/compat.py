"""ffprobe-driven compatibility verdict, audio track listing, and the
moov-before-mdat atom check (D-39..D-44, D-59).

This module's imports stay standard-library only (`struct`, `json`,
`subprocess`, `pathlib`, `dataclasses`) -- `tools/check_faststart.py`
imports from here and runs under the *host*'s bare python, which has no
virtualenv and cannot see `orchestrator`'s third-party dependencies. A
convenience import at the top of this module (pydantic, boto3, anything
from `orchestrator.config`) would silently break that script the moment it
tried to import this one. See `tools/check_faststart.py` for the two-line
sys.path bootstrap that lets it reach this module at all.

`read_top_level_atoms`/`moov_before_mdat` were moved here from
`tools/check_faststart.py` rather than duplicated: the image that runs the
orchestrator copies `orchestrator/`, `alembic.ini` and `migrations/`, and
never `tools/` (see the Dockerfile) -- the check that decides whether a
file can skip ffmpeg entirely (D-44) was previously unreachable from the
running container. `check_faststart.py` now imports from here and stays a
thin CLI wrapper with its own Russian operator-facing messages, since it
remains a dev/CI diagnostic, not part of the localized web interface.

Every not-eligible verdict below carries a translation catalog key (never
an English sentence) plus a parameter dict -- the interface is localized
(D-66) and renders these through `t(key)` with the parameters substituted.
See `.planning/phases/02-as-is-clips/02-UI-SPEC.md` "Incompatibility
reasons" for the exact four keys and which of them take parameters.
"""
import json
import struct
import subprocess
from dataclasses import dataclass


class ProbeError(Exception):
    """ffprobe could not be run on this file, or its output could not be
    parsed. The message here is for a log, not for a user -- the interface
    renders the localized `reason.unreadable` catalog key when this raises,
    never this exception's text.
    """


# --- moov-before-mdat (moved from tools/check_faststart.py) ----------------


def read_top_level_atoms(path) -> list[str]:
    """Read the top-level box/atom type sequence of an MP4/QuickTime file
    without decoding any media -- just the box headers, `struct.unpack`
    over raw bytes. Enough to tell whether `moov` precedes `mdat`
    (D-44/the `-movflags +faststart` pitfall); not a general MP4 parser and
    never grows into one (see 02-RESEARCH.md's own Anti-Patterns section).
    """
    atoms = []
    with open(path, "rb") as f:
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            size, atom_type = struct.unpack(">I4s", header)
            atom_type = atom_type.decode("ascii", errors="replace")
            header_len = 8
            if size == 1:
                # 64-bit largesize immediately after the header
                largesize_bytes = f.read(8)
                if len(largesize_bytes) < 8:
                    break
                size = struct.unpack(">Q", largesize_bytes)[0]
                header_len = 16
            atoms.append(atom_type)
            if size == 0:
                # this atom runs to end of file -- last one in the table
                break
            if size < header_len:
                break  # corrupt atom, nothing further to read
            f.seek(size - header_len, 1)
    return atoms


def moov_before_mdat(path) -> bool:
    """True only when both `moov` and `mdat` are present as top-level
    atoms and `moov` comes first -- the D-44 "already correct, skip
    ffmpeg" precondition. False (never raises) for anything else,
    including a file missing one of the two atoms entirely.
    """
    atoms = read_top_level_atoms(path)
    if "moov" not in atoms or "mdat" not in atoms:
        return False
    return atoms.index("moov") < atoms.index("mdat")


# --- ffprobe verdict ---------------------------------------------------

PROBE_TIMEOUT_SECONDS = 30

# A whitelist, not a comparison -- ffprobe's `profile` field is a human
# string ("Baseline", "Main", "High", "High 10", "High 4:2:2", "High 4:4:4
# Predictive", ...) and these do not sort as "levels of High". "High 10" is
# a *different*, incompatible 10-bit profile, not "above High" (see
# 02-RESEARCH.md Anti-Patterns and Pitfall 2's sibling warning).
COMPATIBLE_PROFILES = frozenset({"Baseline", "Constrained Baseline", "Main", "High"})

# ffprobe reports the H.264 level multiplied by ten -- 41 means 4.1, not a
# typo. Confirmed against the real fixture below: tests/fixtures/probe/
# compatible.json, captured from a clip built exactly like
# tools/make_control_clip.sh's own High-4.1 control clip, literally
# contains "level": 41.
MAX_H264_LEVEL = 41


@dataclass(frozen=True)
class AudioTrack:
    """One audio stream from ffprobe, in file order. `language`/`title`
    are `None` -- never an invented placeholder -- when the source has no
    such tag (D-43's track list must not lie about what it found).
    """

    index: int
    codec: str
    channels: int
    language: str | None
    title: str | None


@dataclass(frozen=True)
class Verdict:
    """The whole answer the add form needs from one `ffprobe` call.

    `reason_key`/`reason_params` are `None`/`{}` when `eligible` is True.
    `already_correct` is the D-44 "skip ffmpeg entirely" signal -- see
    `analyze`'s docstring for exactly what has to line up for it to be
    True. `audio_needs_reencode` reflects the *selected* track only: it is
    the audio half of the "video untouched, audio converted" line (D-39),
    independent of whether the video itself is eligible.
    """

    eligible: bool
    reason_key: str | None
    reason_params: dict[str, str]
    tracks: list[AudioTrack]
    default_track_index: int | None
    already_correct: bool
    audio_needs_reencode: bool
    duration_seconds: float | None
    width: int | None
    height: int | None
    size_bytes: int | None


def probe(path) -> dict:
    """Run `ffprobe -show_streams -show_format` as JSON and parse it.

    Raises `ProbeError` -- never a raw `subprocess.CalledProcessError` or
    `json.JSONDecodeError` that would leak a command line into a user-
    facing surface -- on a non-zero exit, a timeout, unparsable output, or
    a file with no streams at all (an empty stream list means ffprobe
    opened the file but found nothing media-shaped in it, the same
    "unreadable" outcome as a hard failure, from the form's point of view).
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"ffprobe failed to run for {path}: {exc}") from exc

    if result.returncode != 0:
        stderr_tail = result.stderr[-500:] if result.stderr else b""
        raise ProbeError(
            f"ffprobe exited {result.returncode} for {path}: {stderr_tail!r}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe produced unparsable JSON for {path}: {exc}") from exc

    if not data.get("streams"):
        raise ProbeError(f"ffprobe found no streams for {path}")

    return data


def audio_is_safe(stream: dict) -> bool:
    """True only for AAC (D-40). Nothing else is treated as safe, and no
    experiment is run here to find out whether the player would cope with
    anything else -- a silent mid-film audio failure costs more than the
    re-encode it would save.
    """
    return stream.get("codec_name") == "aac"


def audio_tracks(probe_json: dict) -> list[AudioTrack]:
    """Every audio stream, in file order, with language/title left absent
    (never invented) when the source carries no such tag.
    """
    tracks = []
    for stream in probe_json["streams"]:
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        tracks.append(
            AudioTrack(
                index=stream["index"],
                codec=stream.get("codec_name", ""),
                channels=stream.get("channels", 0),
                language=tags.get("language"),
                title=tags.get("title"),
            )
        )
    return tracks


def _default_track_index(tracks: list[AudioTrack]) -> int | None:
    """The first AAC track when one exists, otherwise the first track."""
    for track in tracks:
        if track.codec == "aac":
            return track.index
    return tracks[0].index if tracks else None


def _video_stream(probe_json: dict) -> dict | None:
    return next(
        (s for s in probe_json["streams"] if s.get("codec_type") == "video"), None
    )


def _video_eligibility(video: dict | None) -> tuple[bool, str | None, dict[str, str]]:
    """The four not-eligible reason keys, checked in the order that also
    determines which one fires for a stream that fails more than one check
    at once -- codec, then profile, then level, then pixel format.

    `reason.level` and `reason.pixfmt` carry no parameters (see
    02-UI-SPEC.md's Copywriting Contract: those two strings are fixed
    sentences with nothing to substitute); `reason.codec`/`reason.profile`
    each carry the offending value.
    """
    if video is None:
        return False, "reason.codec", {"codec": "none"}

    codec = video.get("codec_name")
    if codec != "h264":
        return False, "reason.codec", {"codec": codec or "unknown"}

    profile = video.get("profile")
    if profile not in COMPATIBLE_PROFILES:
        return False, "reason.profile", {"profile": profile or "unknown"}

    level = video.get("level") or 0
    if level > MAX_H264_LEVEL:
        return False, "reason.level", {}

    if video.get("pix_fmt") != "yuv420p":
        return False, "reason.pixfmt", {}

    return True, None, {}


def analyze(
    probe_json: dict, *, selected_audio_index: int | None = None, moov_first: bool | None = None
) -> Verdict:
    """Turn one `probe()` result into the `Verdict` the add form renders.

    `selected_audio_index` defaults to `_default_track_index`'s pick (first
    AAC, else first track) when the caller has not overridden it yet --
    the same default the track picker radio group pre-selects (D-43).

    `already_correct` (D-44's "skip ffmpeg entirely" signal) is true only
    when *all* of: the container is MP4, the video is eligible, the
    selected audio track is AAC, it is the *only* audio stream in the
    source, and `moov_first` is true. The "only audio stream" condition is
    not incidental: D-42 says only the selected track ever survives into
    the output, and a file that is uploaded as-is (no ffmpeg pass) cannot
    drop the tracks it did not select -- so a source with more than one
    audio stream can never be already-correct, no matter how compliant the
    selected one is.
    """
    video = _video_stream(probe_json)
    eligible, reason_key, reason_params = _video_eligibility(video)

    tracks = audio_tracks(probe_json)
    default_index = _default_track_index(tracks)
    selected_index = (
        selected_audio_index if selected_audio_index is not None else default_index
    )
    selected_track = next((t for t in tracks if t.index == selected_index), None)
    audio_needs_reencode = selected_track is None or not audio_is_safe(
        {"codec_name": selected_track.codec}
    )

    format_names = (probe_json.get("format", {}).get("format_name") or "").split(",")
    is_mp4 = "mp4" in format_names
    already_correct = bool(
        eligible
        and is_mp4
        and selected_track is not None
        and audio_is_safe({"codec_name": selected_track.codec})
        and len(tracks) == 1
        and moov_first
    )

    fmt = probe_json.get("format", {})
    duration = fmt.get("duration")
    size = fmt.get("size")

    return Verdict(
        eligible=eligible,
        reason_key=reason_key,
        reason_params=reason_params,
        tracks=tracks,
        default_track_index=default_index,
        already_correct=already_correct,
        audio_needs_reencode=audio_needs_reencode,
        duration_seconds=float(duration) if duration is not None else None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        size_bytes=int(size) if size is not None else None,
    )
