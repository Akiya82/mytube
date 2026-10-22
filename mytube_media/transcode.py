"""The conversion and preview command builders. Stdlib only, matching the rest of this package's
rule -- see `mytube_media/__init__.py`.

Every argument list is exactly that: a Python list, never a shell string
(mirrors `remux.py`'s own discipline). The one function both commands are
built through is `build_filter_chain`: the preview renders in the api pod
on the processor in seconds, the conversion runs on the worker's card in
minutes, and the only way to guarantee they draw the same picture is to
build both from one call rather than trust two argument lists to agree.
`FilterGraph` carries the flag the graph needs (`-vf` for a one-input
chain, `-filter_complex` for the bitmap track's labelled two-input graph),
the graph string itself, and the label the video is mapped from -- a
caller hand-deriving the flag and the map from the subtitle kind a second
time is exactly where a preview and a conversion eventually disagree.

Two ffmpeg traps become load-bearing here for the first time in this
codebase: `-pix_fmt yuv420p -profile:v high -level` stated
explicitly (an implicit ten-bit stream out of an anime rip shows black),
and `-movflags +faststart` on the rewrite (a `moov` atom left at the end forces a full download before the
first frame). Every earlier ffmpeg branch in this project only copied streams or
pulled a single frame, so neither trap could bite until now.
"""

import math
from dataclasses import dataclass
from pathlib import Path

from mytube_media.remux import audio_encode_args
from mytube_media.subtitles import BUNDLED_FONT_FAMILY, SRT_FORCE_STYLE

# --- rate control --------------------------------------

# A 1080p flat output at the reference bitrate -- every other figure below
# scales off this pair by pixel area.
REFERENCE_AREA_PX = 1920 * 1080
REFERENCE_BITRATE_BPS = 6_000_000

# The per-viewer bandwidth figure. This is also the ceiling
# `bitrate_for_output` caps a full-frame stereo budget at, not a second
# unrelated number -- see the cap's own comment for why they have to be
# the same figure.
BITRATE_CEILING_BPS = 9_000_000

MAXRATE_RATIO = 1.33
BUFSIZE_RATIO = 2.0

# Two seconds of frames is the locked GOP length; the fallback
# below covers a source whose frame rate ffprobe could not report, so the
# GOP is never zero (a zero GOP is not "no keyframes", it is a
# division-by-zero waiting to happen further down the encode chain).
GOP_SECONDS = 2
_GOP_FALLBACK_FPS = 25
GOP_FALLBACK = round(_GOP_FALLBACK_FPS * GOP_SECONDS)

# A layout that genuinely packs two full pictures into one frame -- see
# `bitrate_for_output`'s own comment for why only these two are budgeted
# above the reference and then capped, while a half-frame layout (already
# one eye's worth of pixels) is left to land on the area formula alone.
FULL_FRAME_LAYOUTS = frozenset({"sbs_full", "ou_full"})

# --- scaling ---------------------------------------------------

MAX_OUTPUT_WIDTH = 1920

# H.264 levels by their macroblocks-per-second ceiling. 4.1 holds 1080p up to
# 30 fps, 4.2 up to 60; NVENC refuses to open at a level the stream exceeds
# rather than raising it. Nothing sits above 4.2 yet, so 1080p past 64 fps and
# full-frame stereo still fail in the encoder.
H264_LEVELS = (("4.1", 245_760), ("4.2", 522_240))

# --- decode side --------------------------------------------------

NVDEC_SUPPORTED_CODECS = frozenset({"h264", "hevc", "vp9", "av1"})


class TranscodeRefused(Exception):
    """A conversion this module refuses to build a command for at all --
    never an ffmpeg failure, always a decision made before any process is
    started. `reason_key` is a translation catalog key (never an English
    sentence baked in here), matching the discipline `compat.ProbeError`'s
    callers already follow for the analyzer's own refusals.
    """

    def __init__(self, reason_key: str, **reason_params):
        self.reason_key = reason_key
        self.reason_params = reason_params
        super().__init__(reason_key)


@dataclass(frozen=True)
class TranscodePlan:
    """The output shape and rate-control targets for one conversion.
    `maxrate_bps`/`bufsize_bps` are carried for the caller's own use (a
    UI showing the planned rates before the job runs) but are never read
    by `build_transcode_command` -- the command recomputes both fresh from
    `video_bitrate_bps` via `MAXRATE_RATIO`/`BUFSIZE_RATIO` at build time,
    so the emitted argv can never drift out of sync with a stale plan.
    """

    width: int
    height: int
    sample_aspect_ratio: str
    video_bitrate_bps: int
    maxrate_bps: int
    bufsize_bps: int
    gop: int
    hwaccel: str | None
    audio_stream_index: int
    audio_channels: int
    reencode_audio: bool
    stereo_layout: str
    fps: float | None = None


@dataclass(frozen=True)
class SubtitleChoice:
    """One subtitle choice for a burn-in, or none at all (`kind="none"`).

    `path` is overloaded by kind: for `embedded_text`, a path to the
    single-track file `subtitles.extract_embedded_track` already pulled
    the chosen stream out to -- never the source container -- so the
    burn-in filter reads a file this project wrote, not an operator's
    path into the file being decoded. The sidecar file's path for
    `external` (also a scratch-local copy, from
    `subtitles.copy_external_track`), and unused for `embedded_bitmap`
    (the overlay branch reads the stream directly via its own input
    specifier). `font_dir`/`charenc` are the collected font directory and
    the encoding `subtitles.read_text` reported, both optional.

    `stream_index` is the file's own absolute stream index, which is what
    the bitmap branch's `-map 0:{index}` needs. Both text kinds burn from a
    single-track file this project already extracted or copied, so there
    is no longer a stream ordinal to carry alongside it -- a file with one
    track has nothing to count.
    """

    kind: str  # "none" | "embedded_text" | "embedded_bitmap" | "external"
    stream_index: int | None = None
    path: str | None = None
    language: str | None = None
    title: str | None = None
    font_dir: str | None = None
    charenc: str | None = None


@dataclass(frozen=True)
class FilterGraph:
    """What `build_filter_chain` hands both command builders: the flag the
    graph must be passed under, the graph string itself, and the label the
    video is mapped from. Three values rather than a bare string because
    the two shapes (`-vf`/`0:v:0` for a one-input chain, `-filter_complex`/
    `[out]` for the bitmap track's labelled overlay) are not
    interchangeable at the command level -- a caller handed only the
    string would have to re-derive the other two by branching on the
    subtitle kind a second time, which is exactly where a preview and a
    conversion eventually disagree.
    """

    flag: str
    value: str
    video_map: str


def output_dimensions(width: int, height: int, stereo_layout: str) -> tuple[int, int]:
    """The output frame size. Every stereo layout passes through
    unchanged, always -- halving the width of a side-by-side frame does
    not shrink a picture, it destroys the pair, and the same is true of an
    over/under frame's height. Only a flat (`"2d"`) source is scaled, down
    only, capped at `MAX_OUTPUT_WIDTH` wide with the aspect kept and the
    height forced even (the encoder's own yuv420p subsampling needs an
    even height).
    """
    if stereo_layout != "2d":
        return width, height
    if width <= MAX_OUTPUT_WIDTH:
        return width, height
    new_width = MAX_OUTPUT_WIDTH
    new_height = round(height * new_width / width)
    if new_height % 2:
        new_height += 1
    return new_width, new_height


def level_for(width: int, height: int, fps: float | None) -> str:
    macroblocks_per_second = math.ceil(width / 16) * math.ceil(height / 16) * (fps or 0)
    for level, ceiling in H264_LEVELS:
        if macroblocks_per_second <= ceiling:
            return level
    return H264_LEVELS[-1][0]


def bitrate_for_output(
    width: int, height: int, stereo_layout: str, *, ceiling: int = BITRATE_CEILING_BPS
) -> int:
    """The target video bitrate, scaled by the output's pixel area against
    the 1080p reference.

    A full-frame stereo layout is budgeted by area too -- it genuinely
    carries two pictures side by side, and scaling the
    budget by area for exactly that reason -- but the result is then
    capped at `ceiling`, never doubled. 9 Mbit/s is the same per-viewer
    bandwidth figure the analyzer warns other files about; a stereo
    conversion that shipped at the bare doubling (12 Mbit/s) would be this
    product exceeding the very ceiling it recommends conversion to stay
    under. Half-frame stereo carries one eye's worth of pixels and is left
    on the area formula alone, landing at the reference figure.

    A flat frame is capped at the reference figure too, defensively --
    `output_dimensions` already keeps a flat frame's area at or below the
    reference, so this second cap should never bind in practice, but nothing
    here should silently exceed the reference by relying on that alone.
    """
    ratio = (width * height) / REFERENCE_AREA_PX
    bitrate = REFERENCE_BITRATE_BPS * ratio
    if stereo_layout in FULL_FRAME_LAYOUTS:
        bitrate = min(bitrate, ceiling)
    else:
        bitrate = min(bitrate, REFERENCE_BITRATE_BPS)
    return round(bitrate)


def gop_for(fps: float | None) -> int:
    """Two seconds of frames, rounded. `fps` of `None` or `0` (ffprobe
    could not report a frame rate) falls back to a fixed assumed rate
    rather than raising or returning zero.
    """
    if not fps:
        return GOP_FALLBACK
    return round(fps * GOP_SECONDS)


def required_free_bytes_for_transcode(
    target_bitrate_bps: float, duration_seconds: float
) -> float:
    """Twice the encoded output's size, in bytes. No source size enters
    this at all: the source is read over the network share and never
    copied locally, so only the output -- doubled, because the rewrite
    pass that adds `+faststart` needs the encoded file's size again at the
    moment it runs -- needs budgeting. This check runs after the expensive
    part (the encode itself) has already finished, so getting it wrong
    wastes the whole conversion, not just the rewrite.
    """
    encoded_bytes = target_bitrate_bps * duration_seconds / 8
    return encoded_bytes * 2


# --- the filter chain -----------------------


# Two grammars read this value in sequence, and both have to be satisfied
# in the order they run. First the filter's own argument grammar (`:`
# separates key=value pairs, `'` quotes a value), which is what the value
# looks like once it reaches the `subtitles` filter itself. Then the
# *filtergraph* grammar wrapped around it, which reads the whole `-vf`
# string before the filter ever sees its own arguments: `[`/`]` delimit a
# link label, `,` chains one filter to the next, `;` ends a chain -- and
# `'` again, because the graph-level tokenizer also has to recognise a
# quoted run in order to know that a comma or semicolon inside it is not a
# structural separator. That double duty is why measuring against a real
# ffmpeg turned up three backslashes as the only form that opens a
# file named with an apostrophe: `:` and `[`/`]`/`,`/`;` are read by one
# grammar each and need one round of escaping, but `'` is read by both and
# needs two. A real library filename carrying brackets --
# `Show - 01 [BDRip 1080p HEVC].mkv`, the shape a release group actually
# ships -- is rejected unescaped by both pinned builds ("Trailing garbage
# after a filter"), which is a failure to even start, not a rendering
# flaw. Spaces need no escaping at any level.
_FILTER_OPTION_ESCAPED_CHARACTERS = ("\\", ":", "'")
_FILTERGRAPH_ESCAPED_CHARACTERS = ("\\", "'", "[", "]", ",", ";")


def _escape_filter_value(value: str) -> str:
    """Escape a value that will ride inside an ffmpeg filter term, at both
    levels that parse it: the filter's own argument grammar first, then
    the filtergraph grammar wrapped around it -- see the comment above
    `_FILTER_OPTION_ESCAPED_CHARACTERS` for why `'` runs through both. No
    surrounding quoting is added -- this project's pinned ffmpeg accepts
    backslash-escaped values with no quotes, and the injection test
    exercises this path with several of these characters at
    once.
    """
    for character in _FILTER_OPTION_ESCAPED_CHARACTERS:
        value = value.replace(character, "\\" + character)
    for character in _FILTERGRAPH_ESCAPED_CHARACTERS:
        value = value.replace(character, "\\" + character)
    return value


def _subtitle_term(subtitle: SubtitleChoice) -> str:
    """The `subtitles=...` filter term for a text-kind burn-in (an
    embedded ASS/SSA/SRT track or an external file). All values that ride
    inside this term go through `_escape_filter_value` and are never
    concatenated at a call site.

    Carries no stream ordinal: the burn-in reads a single-track file this
    project extracted or copied into the job's scratch directory
    (`subtitles.extract_embedded_track`/`subtitles.copy_external_track`),
    never the source container with a stream selector, so there is no
    longer a subtitle stream to count.
    """
    parts = [f"filename={_escape_filter_value(subtitle.path)}"]
    if subtitle.font_dir:
        # ffmpeg has no `-fontsdir` command-line flag -- a real build
        # answers "Unrecognized option" for that spelling and exits before
        # encoding anything. `ffmpeg -h filter=subtitles` lists `fontsdir`
        # among the *filter's own* options, which is the only place this
        # value can go.
        parts.append(f"fontsdir={_escape_filter_value(subtitle.font_dir)}")
    if subtitle.charenc and subtitle.charenc.lower() not in ("utf-8", "utf8"):
        # Omitted for the Unicode default -- emitted for anything else
        # (an older Cyrillic library), since a burn-in that assumes
        # Unicode renders every cp1251 file in the operator's library as
        # mojibake: the conversion still succeeds, uploads and plays, and
        # is wrong.
        parts.append(f"charenc={_escape_filter_value(subtitle.charenc)}")

    # House style for plain text: SubRip/WebVTT carry no styling
    # information of their own, so without this every SRT/VTT burn rendered
    # in the renderer's bare defaults. An ASS/SSA track already carries its
    # own design end-to-end and never gets this style forced onto it.
    # Taking the family from a sidecar font's own name table instead of the
    # bundled fallback needs that font's name table read, which is not one
    # line and is left undone.
    suffix = Path(subtitle.path).suffix.lstrip(".").lower() if subtitle.path else ""
    if suffix in ("srt", "vtt"):
        style = SRT_FORCE_STYLE.format(family=BUNDLED_FONT_FAMILY)
        parts.append(f"force_style={_escape_filter_value(style)}")

    return "subtitles=" + ":".join(parts)


def build_filter_chain(plan: TranscodePlan, subtitle: SubtitleChoice) -> FilterGraph:
    """The one filter chain both `build_transcode_command` and
    `build_preview_command` draw from.

    Refuses first: a stereo source (any layout other than `"2d"`) with any
    real subtitle choice is refused outright -- 3D burn-in is a
    manual BD3D2MK3D step, and a naive burn here would draw one caption
    across both eyes. The same layout with no subtitle choice proceeds
    normally; SAR/DAR are preserved either way.

    Otherwise builds, in this order: the scale, then the pixel-aspect
    preservation term when the source's aspect is not square, then the
    subtitle term. Scaling has to precede the burn-in -- subtitles burned
    in first would be resampled with the picture on the way down and go
    soft.
    """
    if plan.stereo_layout != "2d" and subtitle.kind != "none":
        raise TranscodeRefused(
            "reason.stereo_subtitle_refused", stereo_layout=plan.stereo_layout
        )

    width, height = output_dimensions(plan.width, plan.height, plan.stereo_layout)
    parts = [f"scale={width}:{height}"]
    if plan.sample_aspect_ratio not in (None, "1:1", "1/1"):
        parts.append(f"setsar={plan.sample_aspect_ratio}")

    if subtitle.kind == "embedded_bitmap":
        # Shape two: a labelled two-input graph. A real ffmpeg refuses this
        # exact shape on the simple (`-vf`) flag before reading a frame --
        # "Simple filtergraph was expected to have exactly 1 input and 1
        # output" -- so this branch has its own flag and its own named
        # output label, and both command builders below take the flag and
        # the map from this return value rather than assuming `-vf`.
        scaled_chain = ",".join(parts)
        value = f"[0:v]{scaled_chain}[scaled];[scaled][0:{subtitle.stream_index}]overlay[out]"
        return FilterGraph(flag="-filter_complex", value=value, video_map="[out]")

    if subtitle.kind in ("embedded_text", "external"):
        parts.append(_subtitle_term(subtitle))
    elif subtitle.kind != "none":
        # Every kind this builder understands has been branched on by
        # here. Anything left is a value that was carried this far without
        # ever being resolved -- "embedded" is the one that has actually
        # happened, the form's own word for a choice that still has to be
        # read against the file as text or bitmap. Falling through would
        # produce a perfectly good conversion with no subtitle in it,
        # reported as a success: the failure mode this raise exists for.
        raise ValueError(f"unknown subtitle kind {subtitle.kind!r}")

    return FilterGraph(flag="-vf", value=",".join(parts), video_map="0:v:0")


# --- the conversion command --------


def build_transcode_command(
    source,
    destination,
    *,
    plan: TranscodePlan,
    subtitle: SubtitleChoice,
    external_audio: str | None = None,
) -> list[str]:
    """The command the worker runs on the card. Decodes on NVDEC only when
    `plan.hwaccel` is set by the caller (the decision of whether the
    source codec is one `NVDEC_SUPPORTED_CODECS` names is the caller's,
    made once against the probed codec) -- and never with the flag that
    would keep decoded frames on the card, since `scale`/`subtitles`/
    `overlay` all run on the processor and one filter chain has to serve
    both this command and the preview.

    `external_audio` mirrors `remux.build_remux_command`'s own
    parameter of the same name: a second input added right after the
    source, mapped by its own first audio stream (`1:a:0`) instead of
    `plan.audio_stream_index`, always encoded with
    `audio_encode_args(plan.audio_channels)` -- the caller sets
    `audio_channels` to the *external* file's own probed channel count in
    that case, not the source's. `-hwaccel`, stated once before the
    source's own `-i`, is a per-input option and never applies to this
    second input, which is exactly right -- an audio-only file needs no
    hardware decode. Never emits the flag that would end the output at
    whichever stream finishes first, the same reasoning
    `build_remux_command`'s own docstring states in full. With no
    `external_audio`, every emitted argument is unchanged from before this
    parameter existed.
    """
    graph = build_filter_chain(plan, subtitle)

    cmd = ["ffmpeg", "-y"]
    if plan.hwaccel:
        cmd += ["-hwaccel", plan.hwaccel]
    cmd += ["-i", str(source)]
    if external_audio:
        cmd += ["-i", str(external_audio)]
    cmd += [graph.flag, graph.value]
    audio_map = "1:a:0" if external_audio else f"0:{plan.audio_stream_index}"
    cmd += ["-map", graph.video_map, "-map", audio_map]
    cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr"]

    maxrate = round(plan.video_bitrate_bps * MAXRATE_RATIO)
    bufsize = round(plan.video_bitrate_bps * BUFSIZE_RATIO)
    cmd += [
        "-b:v",
        str(plan.video_bitrate_bps),
        "-maxrate",
        str(maxrate),
        "-bufsize",
        str(bufsize),
    ]

    # Without these stated explicitly, the encoder happily carries a ten-bit
    # stream out of an anime rip and the player shows black.
    level = level_for(plan.width, plan.height, plan.fps)
    cmd += ["-pix_fmt", "yuv420p", "-profile:v", "high", "-level", level]
    cmd += ["-g", str(plan.gop), "-keyint_min", str(plan.gop)]

    if external_audio or plan.reencode_audio:
        # The same arguments the as-is branch uses. An external track is
        # always encoded, regardless of `plan.reencode_audio`: it is a
        # source that is never already inside the destination container.
        cmd += audio_encode_args(plan.audio_channels)
    else:
        cmd += ["-c:a", "copy"]

    # Without this, the moov atom sits at the end of the file and a player
    # downloads the whole thing before the first frame.
    cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(destination)]
    return cmd


# --- the preview command ----------------------------


def build_preview_command(
    source,
    destination,
    *,
    plan: TranscodePlan,
    subtitle: SubtitleChoice,
    at_seconds: float,
) -> list[str]:
    """The command the api pod runs on the processor, in seconds. Seeks
    with `-ss` before `-i` so the render only decodes from the nearest
    keyframe forward, `-copyts` so the burn-in filter reads the cue at
    `at_seconds` rather than one from the start of the file. Takes the
    same flag, value and video map `build_transcode_command` took from one
    call to `build_filter_chain` -- so the identity that makes the preview
    honest holds by construction in all four subtitle branches, not only
    the text one. Never asks for the card: no `-hwaccel`, no video
    encoder, one frame out.
    """
    graph = build_filter_chain(plan, subtitle)

    cmd = ["ffmpeg", "-y"]
    cmd += ["-ss", str(at_seconds), "-copyts"]
    cmd += ["-i", str(source)]
    cmd += [graph.flag, graph.value]
    cmd += ["-map", graph.video_map]
    cmd += ["-frames:v", "1"]
    cmd += [str(destination)]
    return cmd
