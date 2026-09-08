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

`Verdict.plan` is one of two values -- `"passthrough"` for an eligible
source, `"transcode"` for one that is not -- and `plan_reason_key`/
`plan_reason_params` are exactly the eligibility branch's own `reason_key`/
`reason_params`, never a second sentence computed here (CONV-01). The
bitrate ceiling `over_bitrate_ceiling` compares against is advisory only:
nothing in `_video_eligibility` or in the `plan` computation ever reads
it, so a compatible source above the ceiling still plans a passthrough
and stays addable as-is -- the ceiling recommends conversion, it never
gates it.

`stereo_layout`/`stereo_layout_source` and `subtitle_tracks` come from
`mytube_media.stereo3d`/`mytube_media.subtitles`, called once each from
the same `probe()` output this module already has -- never a second
`ffprobe` invocation. Both of those modules are, like this one, stdlib
only: importing them here does not weaken the bare-python guarantee
`check_faststart.py` depends on.
"""
import json
import struct
import subprocess
from dataclasses import dataclass

from mytube_media.stereo3d import detect_layout
from mytube_media.subtitles import SubtitleTrack, embedded_tracks


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

# D-136: the per-viewer bandwidth figure the constraint section names, as
# a default for the keyword `analyze` takes -- passed in rather than read
# from settings, so this module stays free of the application's config
# (the whole point of the package it lives in). Advisory only: see the
# module docstring's own statement of that rule.
DEFAULT_BITRATE_CEILING_BPS = 9_000_000


@dataclass(frozen=True)
class AudioTrack:
    """One audio stream from ffprobe, in file order. `language`/`title`
    are `None` -- never an invented placeholder -- when the source has no
    such tag (D-43's track list must not lie about what it found).

    `external_path`, `folder_label` and `duration_seconds` are `None` for
    every embedded track `audio_tracks` returns; `external_audio_track`
    below is the one place that fills them, so a caller can hold embedded
    and external candidates in one flat list (D-181) and tell them apart
    by whether `external_path` is set, never by a second type.
    """

    index: int
    codec: str
    channels: int
    language: str | None
    title: str | None
    sample_rate: int | None = None
    channel_layout: str | None = None
    external_path: str | None = None
    folder_label: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class Verdict:
    """The whole answer the add form needs from one `ffprobe` call.

    `reason_key`/`reason_params` are `None`/`{}` when `eligible` is True.
    `already_correct` is the D-44 "skip ffmpeg entirely" signal -- see
    `analyze`'s docstring for exactly what has to line up for it to be
    True. `audio_needs_reencode` reflects the *selected* track only: it is
    the audio half of the "video untouched, audio converted" line (D-39),
    independent of whether the video itself is eligible.

    The seven fields below (`fps` through `audio_channel_layout`) exist
    for the playlist joinability check: `-f concat -c copy` needs two
    members' streams to agree on these, and the catalog could not answer
    the question until now. All seven describe the *source* as this probe
    saw it -- `None` for a missing, malformed or not-yet-measured value,
    never invented. `fps` is parsed from the video stream's average
    frame-rate fraction string into a float; `video_profile`/
    `video_level`/`pix_fmt` are the same video-stream fields
    `_video_eligibility` already inspects, not a second probe. The three
    `audio_*` fields describe the *selected* track only (the only one
    that survives into the output, D-42) -- reconciling the codec to what
    the remux actually stores is `effective_audio_codec`'s job, not this
    dataclass's: this module describes the source, and the caller that
    knows the remux's own outcome does the reconciliation.

    The eight fields below (`plan` through `stereo_layout_source`) are
    CONV-01's "why this path" answer. `plan`/`plan_reason_key`/
    `plan_reason_params` are the eligibility branch's own verdict restated
    as a plan -- one place decides the reason, this dataclass just carries
    it under a second name. `bitrate_bps` is read from the probe's own
    `format.bit_rate`, never computed from size and duration (a computed
    figure would silently differ from what every other tool reports for
    the same file), and is `None` when the probe did not report one --
    `over_bitrate_ceiling` is then `False`, never `True`: an unmeasurable
    bitrate is an unknown, not a warning. `subtitle_tracks` is the
    *embedded* list only; external files come from the filesystem, which
    `analyze` never touches, and the route that draws the form joins the
    two. `stereo_layout`/`stereo_layout_source` are `stereo3d.detect_layout`'s
    own two-tuple, unpacked.
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
    fps: float | None
    video_profile: str | None
    video_level: int | None
    pix_fmt: str | None
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channel_layout: str | None
    plan: str
    plan_reason_key: str | None
    plan_reason_params: dict[str, str]
    bitrate_bps: int | None
    over_bitrate_ceiling: bool
    subtitle_tracks: list[SubtitleTrack]
    stereo_layout: str
    stereo_layout_source: str


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
                sample_rate=_maybe_number(stream.get("sample_rate"), int),
                channel_layout=stream.get("channel_layout"),
            )
        )
    return tracks


# D-182: the gap above which the interface shows a duration-mismatch
# warning next to an external audio track. A starting figure, not a
# measurement -- ordinary container-overhead differences between a video
# and a standalone dub sit well under a second, so a few seconds already
# distinguishes "different container, same cut" from "wrong episode" or
# "trimmed intro." Named and documented so a later plan can retune it
# against real dubs rather than guess a second time.
DURATION_MISMATCH_THRESHOLD_SECONDS = 5.0


def external_audio_track(path, *, folder_label: str | None = None) -> AudioTrack:
    """Probe one external audio file and return it in the same
    `AudioTrack` shape an embedded stream takes (D-181), so a caller can
    hold embedded and external candidates in one flat list.

    Runs `probe()` against `path` itself, then reads its first (and, for
    every real dub in the owner's library, only) audio stream -- an
    external file, unlike a video source, is never expected to carry more
    than one. `folder_label` is carried through unchanged, for the
    interface to show where the file came from ("RUS Sound/AniStar");
    this function does not compute it -- it has no notion of a share root
    or a source file to compute it relative to.
    """
    probe_json = probe(path)
    stream = next(s for s in probe_json["streams"] if s.get("codec_type") == "audio")
    tags = stream.get("tags") or {}
    return AudioTrack(
        index=stream.get("index", 0),
        codec=stream.get("codec_name", ""),
        channels=stream.get("channels", 0),
        language=tags.get("language"),
        title=tags.get("title"),
        sample_rate=_maybe_number(stream.get("sample_rate"), int),
        channel_layout=stream.get("channel_layout"),
        external_path=str(path),
        folder_label=folder_label,
        duration_seconds=_maybe_number(probe_json.get("format", {}).get("duration"), float),
    )


def duration_gap_seconds(a: float | None, b: float | None) -> float | None:
    """The absolute difference between two probed durations -- the pure
    comparison D-182's warning is built from, computed here so nothing
    downstream re-derives it from ffmpeg's own mux-time behaviour (Pitfall
    4: `-shortest` is not how this project measures a mismatch; the two
    `format.duration` values are already available from each file's own
    probe, before any mux command is built). Commutative, and `None` when
    either duration is unmeasurable -- the same "absence is not a warning"
    rule `Verdict.over_bitrate_ceiling` already keeps for a bitrate ffprobe
    could not report: an unmeasurable duration is an unknown, never a
    fabricated gap. This function never refuses anything on its own; it is
    read by the caller that decides whether to show a warning.
    """
    if a is None or b is None:
        return None
    return abs(a - b)


def _default_track_index(tracks: list[AudioTrack]) -> int | None:
    """The first AAC track when one exists, otherwise the first track."""
    for track in tracks:
        if track.codec == "aac":
            return track.index
    return tracks[0].index if tracks else None


def _parse_frame_rate(value) -> float | None:
    """Parse ffprobe's `avg_frame_rate` fraction string ("24/1",
    "24000/1001", "0/0") into a float. `None` for a missing, malformed or
    zero-denominator value -- in the spirit of `_maybe_number` below,
    never invented.
    """
    if not value:
        return None
    parts = value.split("/")
    if len(parts) != 2:
        return None
    try:
        numerator, denominator = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def effective_audio_codec(source_codec: str, reencoded: bool) -> str:
    """The codec the stored object actually carries, not the source's.

    `build_remux_command` emits `-c:a aac` whenever the audio is
    re-encoded, so a member whose source was AC-3 and a member whose
    source was already AAC end up as the same codec in the bucket --
    recording the probed source codec for both would make two identical
    stored objects look like a mismatch and refuse a list that would have
    joined perfectly. `Verdict.audio_codec` keeps carrying the probed
    source value unchanged; this function is where the reconciliation
    happens, once the caller knows the remux's own outcome.
    """
    return "aac" if reencoded else source_codec


def _maybe_number(value, cast):
    """`format.duration` and `format.size` as ffprobe actually reports
    them: either a numeric string, or absent, or the literal string "N/A"
    on a container that does not carry the field -- MPEG-TS, some MKV
    rips, a file still being written. probe() succeeds on all of those, so
    a cast that raises here escapes every ProbeError handler the add form
    has and surfaces as a traceback. Both fields are optional on `Verdict`
    and every consumer already handles None, so an unparsable value simply
    becomes None.
    """
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


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
    probe_json: dict,
    *,
    selected_audio_index: int | None = None,
    moov_first: bool | None = None,
    filename: str = "",
    bitrate_ceiling_bps: int = DEFAULT_BITRATE_CEILING_BPS,
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

    `filename` feeds `stereo3d.detect_layout`'s filename-token check only
    -- it defaults to the empty string, under which that check simply
    finds nothing and falls through to geometry, exactly as if the caller
    genuinely does not have a name for the file yet.
    """
    video = _video_stream(probe_json)
    eligible, reason_key, reason_params = _video_eligibility(video)

    tracks = audio_tracks(probe_json)
    default_index = _default_track_index(tracks)

    if eligible and not tracks:
        # A silent source is compliant video and would otherwise come back
        # eligible with an empty track list -- the form then renders a live
        # Add button above a track picker with no options, and the POST's
        # required audio_track field can only be answered with a raw 422.
        # Only the selected track survives into the output (D-42), so a
        # source with none has no add path; refusing it here routes it
        # through the same localized banner as every other rejection
        # instead of leaving that third, unreachable state.
        eligible = False
        reason_key = "reason.no_audio"
        reason_params = {}

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

    plan = "passthrough" if eligible else "transcode"
    bitrate_bps = _maybe_number(fmt.get("bit_rate"), int)
    over_bitrate_ceiling = bitrate_bps is not None and bitrate_bps > bitrate_ceiling_bps
    stereo_layout, stereo_layout_source = detect_layout(probe_json, filename)

    return Verdict(
        eligible=eligible,
        reason_key=reason_key,
        reason_params=reason_params,
        tracks=tracks,
        default_track_index=default_index,
        already_correct=already_correct,
        audio_needs_reencode=audio_needs_reencode,
        duration_seconds=_maybe_number(fmt.get("duration"), float),
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        size_bytes=_maybe_number(fmt.get("size"), int),
        fps=_parse_frame_rate(video.get("avg_frame_rate") if video else None),
        video_profile=video.get("profile") if video else None,
        video_level=video.get("level") if video else None,
        pix_fmt=video.get("pix_fmt") if video else None,
        audio_codec=selected_track.codec if selected_track else None,
        audio_sample_rate=selected_track.sample_rate if selected_track else None,
        audio_channel_layout=selected_track.channel_layout if selected_track else None,
        plan=plan,
        plan_reason_key=reason_key,
        plan_reason_params=reason_params,
        bitrate_bps=bitrate_bps,
        over_bitrate_ceiling=over_bitrate_ceiling,
        subtitle_tracks=embedded_tracks(probe_json),
        stereo_layout=stereo_layout,
        stereo_layout_source=stereo_layout_source,
    )
