"""ffmpeg argument construction and the runner (D-39..D-44, D-72).

The boundary this module keeps is "build the argument list" versus "run
it" -- `build_remux_command`/`build_poster_command` are pure functions
over strings, unit-testable with no ffmpeg present, and `run_ffmpeg` is
the one place that actually calls `subprocess.run`. Never interpolate a
path into a string command: every argument is its own list element, so a
source path containing a space, a quote or a semicolon still reaches
ffmpeg as exactly one argument (T-02-06).

Video is always `-c:v copy` in every branch this module builds -- the
compatibility gate in `orchestrator.media.compat` is what guarantees the
source is already H.264/High/<=4.1/yuv420p before this module ever runs,
so there is no video-encode branch here to guard. `-movflags +faststart`
is present in both remux branches without exception: without it the
`moov` atom sits at the end of the file and a player downloads the whole
thing before the first frame (`.claude/CLAUDE.md`, trap 1).

A note that belongs here because the next reader will otherwise "fix" it:
this project's other ffmpeg trap -- stating `-pix_fmt yuv420p` and
`-profile:v high -level 4.1` explicitly -- applies to the branch that
*encodes video*. This module never encodes video (it only ever copies the
video stream), so those arguments are deliberately absent here; what
guarantees an 8-bit yuv420p High-at-most-4.1 stream reaches this module at
all is the compatibility gate refusing anything else before either
command is built.
"""
import subprocess

# D-41: roughly 96 kbit/s per audio channel, floored so a mono/stereo
# re-encode never drops below a sane minimum, capped so a source with an
# implausible channel count never asks ffmpeg for an absurd bitrate. The
# floor and the per-channel rate are both above the flat 192k placeholder
# 02-RESEARCH.md flagged as under-serving a 5.1 track -- see aac_bitrate_for.
AAC_KBPS_PER_CHANNEL = 96
AAC_MIN_KBPS = 128
AAC_MAX_KBPS = 640

POSTER_WIDTH = 640
# D-72: ten percent of the duration, capped so a long film's poster is not
# pulled from deep in the runtime, with a fallback when duration is unknown
# (an unreadable/zero duration must never crash the extraction attempt).
POSTER_FRACTION = 0.1
POSTER_MAX_SECONDS = 60.0
POSTER_FALLBACK_SECONDS = 5.0


class RemuxError(Exception):
    """ffmpeg exited non-zero or timed out. Carries the last few lines of
    stderr -- enough for a log line, never surfaced to a user (the
    interface renders a translation catalog key, same discipline as
    `orchestrator.media.compat.ProbeError`).
    """


def aac_bitrate_for(channels: int) -> int:
    """~96 kbit/s per channel, floored at 128k, capped at 640k -- D-41
    preserves the channel count on purpose (no `-ac` downmix anywhere in
    this module), so the bitrate has to scale with it or a 5.1/7.1 track
    gets starved at the same flat rate a mono commentary track would get.
    """
    per_channel = max(channels, 1) * AAC_KBPS_PER_CHANNEL
    return max(AAC_MIN_KBPS, min(AAC_MAX_KBPS, per_channel))


def build_remux_command(
    source,
    destination,
    *,
    audio_stream_index: int,
    reencode_audio: bool,
    channels: int,
) -> list[str]:
    """Video is always `-c:v copy`. Audio is `-c:a copy` when the source
    audio is already AAC (`reencode_audio=False`) and `-c:a aac` otherwise,
    at `aac_bitrate_for(channels)`. No `-ac` argument is ever emitted in
    either branch -- D-41's channel count is preserved by omission, not by
    an explicit passthrough flag.

    `audio_stream_index` is mapped by its **absolute** ffprobe stream
    index (`0:{index}`), never a relative audio-track selector
    (`0:a:{n}`) -- the analyzer reports absolute indices, and the
    relative form would pick the wrong track on any file whose streams
    are not in the obvious video-then-audio order (D-42: only the
    selected track is ever muxed in).
    """
    audio_codec_args = (
        ["-c:a", "aac", "-b:a", f"{aac_bitrate_for(channels)}k"]
        if reencode_audio
        else ["-c:a", "copy"]
    )
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        f"0:{audio_stream_index}",
        "-c:v",
        "copy",
        *audio_codec_args,
        "-movflags",
        "+faststart",
        str(destination),
    ]


def poster_timestamp(duration_seconds: float | None) -> float:
    """Ten percent of the duration, capped at sixty seconds -- far enough
    in to miss a black opening frame, not so far as to spoil anything. A
    five-second fallback covers an unknown or non-positive duration
    (`ffprobe` did not report one, or reported garbage) rather than
    crashing the extraction attempt.
    """
    if not duration_seconds or duration_seconds <= 0:
        return POSTER_FALLBACK_SECONDS
    return min(duration_seconds * POSTER_FRACTION, POSTER_MAX_SECONDS)


def build_poster_command(source, destination, *, at_seconds: float) -> list[str]:
    """Seek with `-ss` *before* `-i` -- ffmpeg can then seek directly to
    the nearest keyframe instead of decoding every frame from the start,
    which matters on an eight-gigabyte source. Takes exactly one frame,
    scales to `POSTER_WIDTH` wide preserving aspect ratio, writes a JPEG
    (the destination's own extension picks the encoder).
    """
    return [
        "ffmpeg",
        "-y",
        "-ss",
        str(at_seconds),
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={POSTER_WIDTH}:-1",
        str(destination),
    ]


def run_ffmpeg(argv: list[str], *, timeout: float) -> None:
    """Run `argv` with no shell -- the whole point of building an argument
    list instead of a string is that this call never interpolates a path
    into anything a shell could re-parse. Raises `RemuxError` carrying the
    last few lines of stderr on a non-zero exit or a timeout; never lets a
    raw `CalledProcessError`/`TimeoutExpired` escape this module.
    """
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RemuxError(
            f"ffmpeg timed out after {timeout}s: {' '.join(argv)}"
        ) from exc

    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
        tail = "\n".join(stderr_text.splitlines()[-10:])
        raise RemuxError(f"ffmpeg exited {result.returncode}: {tail}")
