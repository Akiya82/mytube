"""Pure assembly functions for a playlist: the stream parameters members
are compared on, the joinability verdict, the free-space figure one build
needs, the argument list that turns one member into a deliverable
segment, and the manifest renderer with the validator that stands in
front of it.

Same split as `mytube_media.remux`: everything here is a pure
function over plain values, unit-testable with no ffmpeg installed, no
database and no network. There is exactly one ffmpeg runner in this
codebase -- `remux.run_ffmpeg` -- and the caller imports it directly;
this module never defines a second one, so there is one error type and
one stderr-trimming rule.

**Which mechanism this builds, and why.** A playlist is served as a
generated manifest listing its members, not as one physically joined
file. That was measured rather than assumed: a video player inside
VRChat was watched across three boundaries -- differing resolution,
differing frame rate, differing audio channel layout -- and carried every
one of them with the next clip starting, no black frame and no audio
drift (`src/docs/experiments/04-avpro-discontinuity.md`). Physical
joining would have required every member to share stream parameters and
would therefore have refused exactly the heterogeneous as-is library a
playlist is most wanted for. A single continuous re-muxed stream was
measured too and rejected on arithmetic: it declares one frame rate for
the whole timeline, so a member recorded at a different rate plays slow
and the picture falls behind the sound. Correcting that needs a
re-encode, which this stage does not do.

**One segment per member, not many.** A member becomes exactly one
segment object, so every boundary in the rendered manifest is a real clip
boundary -- precisely the thing the experiment measured. It also keeps
`playlist_artifacts.position` meaning what its own comment says (one row
per segment, in order) with no schema change: splitting a member into
several segments would need a second column saying which member a
segment belongs to, since the boundary markers cannot be placed without
it. The ceiling this accepts: one segment can be forty minutes long, and
the player's behaviour on a list of that size was explicitly not measured
(see the experiment's "What was not measured"). If that turns out to
matter, the upgrade is a member-index column on the artifact table plus a
real split here; nothing else in this module changes.

**Why a segment carries MPEG-TS bytes under a key ending in `.mp4`.**
The extension is the delivery address pattern, not a claim about the
container: the edge accepts one shape and one shape only,
`{identifier}.mp4`, and the same shape is what the existing signer signs
and what the reclamation query already covers. Widening that pattern to
admit a second extension would mean a second delivery surface for no
gain. The bytes are MPEG-TS because that is the container the boundary
was actually measured with, and a manifest's segments are identified by
the manifest, never by their filename. The upload states the real type
(`video/mp2t`) so the response a player receives is honest about what it
holds; only the key spelling is fixed by the edge.
"""
import math
from dataclasses import dataclass

from mytube_media.compat import _parse_frame_rate, audio_tracks

# Log tags, deliberately outside the `playlist.refused.*` namespace the
# interface renders from. The verdict below never reaches an operator: a
# parameter difference is what the boundary marker exists to carry, so it
# is recorded in the service log and never written to the row. Spelling
# these as catalogue keys, and translating them, promised a sentence
# nothing could ever display. Two tags, not one, because a clip that
# simply predates the columns recording its parameters is a different
# finding from two clips that were measured and differ.
PARITY_LOG_MISMATCH = "parity.mismatch"
PARITY_LOG_UNMEASURED = "parity.unmeasured"

# The parameters two members are compared on, each with the wording that
# names it to an operator. Width and height are deliberately absent: a
# resolution change was one of the three the player carried, and the
# catalog does not record the other members' dimensions in the same shape
# anyway.
JOIN_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("fps", "frame rate"),
    ("video_profile", "video profile"),
    ("video_level", "video level"),
    ("pix_fmt", "pixel format"),
    ("audio_codec", "audio codec"),
    ("audio_sample_rate", "audio sample rate"),
    ("audio_channel_layout", "audio channel layout"),
)

# Every member is pulled into scratch and they all coexist there, the
# segment output of the same total size is written beside them, and one
# more multiple is the margin for the rewrite that produces it. See
# required_build_bytes.
BUILD_SCRATCH_MULTIPLE = 3


@dataclass(frozen=True)
class StreamParameters:
    """What a stored object actually carries, read out of a probe result.

    Every field is `None` when the probe does not report it, never
    defaulted to something plausible: an invented value would let a member
    nobody measured pass as a member known to match.
    """

    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    video_profile: str | None
    video_level: int | None
    pix_fmt: str | None
    audio_codec: str | None
    audio_sample_rate: int | None
    audio_channel_layout: str | None


def stream_parameters(probe_json: dict) -> StreamParameters:
    """Read the ten joinability parameters out of one ffprobe result.

    The audio values come from the track the add pipeline would have
    selected -- the first AAC track when there is one, otherwise the first
    track -- through `compat.audio_tracks`, not from whichever audio
    stream happens to come first in the file. The frame rate goes through
    `compat`'s own fraction parser rather than a second copy of it here.
    """
    video = next(
        (s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"),
        None,
    ) or {}

    tracks = audio_tracks(probe_json)
    track = next((t for t in tracks if t.codec == "aac"), tracks[0] if tracks else None)

    level = video.get("level")
    return StreamParameters(
        width=video.get("width"),
        height=video.get("height"),
        fps=_parse_frame_rate(video.get("avg_frame_rate")),
        video_codec=video.get("codec_name"),
        video_profile=video.get("profile"),
        video_level=int(level) if level is not None else None,
        pix_fmt=video.get("pix_fmt"),
        audio_codec=track.codec if track else None,
        audio_sample_rate=track.sample_rate if track else None,
        audio_channel_layout=track.channel_layout if track else None,
    )


@dataclass(frozen=True)
class Mismatch:
    """One member that does not sit with the rest, and which parameter
    puts it there.
    """

    title_name: str
    parameter: str


@dataclass(frozen=True)
class ParityVerdict:
    """The same shape `compat`'s eligibility functions already return: a
    boolean, an identifying tag and a parameter mapping. Never a sentence.

    Unlike `compat`'s, this verdict goes to the service log rather than to
    the screen, so `reason_key` carries one of the parity log tags above
    and not a translation catalogue key -- there is no catalogue entry
    behind it and nothing renders it.
    """

    ok: bool
    mismatches: tuple[Mismatch, ...]
    reason_key: str | None
    reason_params: dict


def _unmeasured_parameter(member) -> str | None:
    for field, label in JOIN_PARAMETERS:
        if member.get(field) is None:
            return label
    return None


def _reason_params(mismatches: tuple[Mismatch, ...]) -> dict:
    """What the log line reports: the member names, and the parameters
    that separate them. Two plain strings rather than a nested structure,
    so one log line reads as a sentence without a formatter walking it.
    """
    return {
        "members": ", ".join(m.title_name for m in mismatches),
        "parameters": ", ".join(dict.fromkeys(m.parameter for m in mismatches)),
    }


def parity_verdict(members) -> ParityVerdict:
    """Whether these members share the parameters that matter for joining.

    Informational under the mechanism this project builds -- an unmatched
    parameter is what the boundary marker exists to handle, so a
    difference is recorded and never refuses a build. It stays here
    because the finding belongs in the service log when a build joins
    clips that differ, and because a physical join, if this project ever
    gains a re-encode, needs exactly this answer before any bytes move.

    A member marked as dropped out is skipped entirely: its title is gone
    from the library and it will never be part of the result, so counting
    it as a mismatch would refuse a list over a clip nobody will ever see.

    A member with any parameter unrecorded is its own answer, not a
    mismatch -- see the two parity log tags above.
    """
    considered = [m for m in members if not m.get("dropped")]

    unmeasured = [m for m in considered if _unmeasured_parameter(m) is not None]
    if unmeasured:
        mismatches = tuple(
            Mismatch(
                title_name=m.get("title_name") or "",
                parameter=_unmeasured_parameter(m) or "",
            )
            for m in unmeasured
        )
        return ParityVerdict(
            ok=False,
            mismatches=mismatches,
            reason_key=PARITY_LOG_UNMEASURED,
            reason_params=_reason_params(mismatches),
        )

    if len(considered) < 2:
        return ParityVerdict(ok=True, mismatches=(), reason_key=None, reason_params={})

    # Group by the joinability parameters, preserving first-seen order, and
    # take the largest group as the reference. `max` returns the first
    # maximum it meets, so a tie -- three members that all differ from each
    # other -- resolves to the one that appears first in the list, which is
    # the one an operator would call "the rest".
    groups: dict[tuple, list] = {}
    for member in considered:
        key = tuple(member.get(field) for field, _ in JOIN_PARAMETERS)
        groups.setdefault(key, []).append(member)

    reference_key = max(groups, key=lambda k: len(groups[k]))
    reference = dict(zip((field for field, _ in JOIN_PARAMETERS), reference_key))

    mismatches = []
    for key, group in groups.items():
        if key == reference_key:
            continue
        for member in group:
            differing = next(
                label
                for field, label in JOIN_PARAMETERS
                if member.get(field) != reference[field]
            )
            mismatches.append(
                Mismatch(title_name=member.get("title_name") or "", parameter=differing)
            )

    if not mismatches:
        return ParityVerdict(ok=True, mismatches=(), reason_key=None, reason_params={})

    frozen = tuple(mismatches)
    return ParityVerdict(
        ok=False,
        mismatches=frozen,
        reason_key=PARITY_LOG_MISMATCH,
        reason_params=_reason_params(frozen),
    )


def required_build_bytes(members) -> int:
    """The scratch space one playlist build needs, over the members'
    recorded sizes. Accepts the member rows themselves (anything with a
    `size_bytes`) or plain integers.

    Three times the members' total, and no poster allowance: a playlist
    build extracts no frame, so there is nothing for one to pay for.
    Empty list, zero.

    **`orchestrator.media.add.required_free_bytes` is deliberately not
    called here**, because reusing it is the obvious-looking move and it
    is wrong. That function returns twice the source plus a poster budget,
    and its own docstring says in as many words that the doubling is a
    margin for one rewrite *precisely because the source never enters
    scratch* -- it is read from the read-only share. A playlist build's
    inputs do enter scratch, all of them at once, and its output is
    written beside them. Reusing that arithmetic passes the precheck on a
    disk that cannot hold the build, and ffmpeg dies part-way through a
    multi-gigabyte write. This stack has exhausted its storage twice; the
    precheck exists for exactly that, and the add path's figure is
    calibrated for different work.
    """
    total = 0
    for member in members:
        total += member if isinstance(member, int) else (member.get("size_bytes") or 0)
    return BUILD_SCRATCH_MULTIPLE * total


def build_segment_command(source, destination) -> list[str]:
    """The argument list that turns one member into one deliverable
    segment by copying its packets -- no re-encode, no filter, no
    parameter change of any kind.

    A plain list of strings, never a shell string: a title containing a
    space, a quote or a semicolon reaches ffmpeg as exactly one argument.

    `-movflags +faststart` is deliberately absent and its absence is not
    an oversight: the output is MPEG-TS, which has no `moov` atom to move
    to the front -- it is readable from any point by construction, which
    is the property faststart buys for MP4. The module docstring explains
    why the container is MPEG-TS and why the object key still ends
    `.mp4`. The same paragraph is the reason no explicit `-pix_fmt` or
    `-profile:v` appears here either: nothing is encoded, so there is no
    encoder branch for those flags to constrain. What guarantees an 8-bit
    High-at-most-4.1 stream is the compatibility gate that refused
    anything else before the member was ever stored.
    """
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-c",
        "copy",
        "-f",
        "mpegts",
        str(destination),
    ]


@dataclass(frozen=True)
class Segment:
    """One stored object a manifest points at. `object_key` is the full
    storage key, extension included, exactly as `playlist_artifacts` holds
    it; `url` is the signed delivery address the one existing signer
    produced for it.
    """

    object_key: str
    duration_seconds: float
    url: str = ""


@dataclass(frozen=True)
class Section:
    """One member's worth of segments. A section boundary is a clip
    boundary, and a boundary marker is emitted between every pair of
    sections.
    """

    title_name: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class ManifestVerdict:
    """Same shape as the parity verdict. `reason_params` carries a
    `detail` the worker and the address route log -- these failures are
    never shown to anyone, because a viewer gets the not-ready clip.
    """

    ok: bool
    reason_key: str | None
    reason_params: dict


REASON_KEY_MANIFEST_EMPTY = "manifest.empty"
REASON_KEY_MANIFEST_MISSING_SEGMENT = "manifest.missing_segment"
REASON_KEY_MANIFEST_BAD_DURATION = "manifest.bad_duration"
REASON_KEY_MANIFEST_TARGET_DURATION = "manifest.target_duration"
REASON_KEY_MANIFEST_MISSING_BOUNDARY = "manifest.missing_boundary"
REASON_KEY_MANIFEST_TRUNCATED = "manifest.truncated"


def _target_duration(sections) -> int:
    longest = max(
        (seg.duration_seconds for section in sections for seg in section.segments),
        default=0.0,
    )
    return max(1, math.ceil(longest))


def render_manifest(sections) -> str:
    """Assemble the document a player reads. Plain string joining.

    No library is reached for, for two reasons worth stating rather than
    rediscovering. This document is five kinds of line, so a
    general-purpose one would be a dependency carrying a whole
    specification for the fraction of it used here. And this project's
    delivery path has no frontend dependency at all -- adding the first
    one to emit five line shapes is not a trade anybody would make twice.

    The boundary marker between two sections is emitted
    **unconditionally**, without computing whether the members' parameters
    actually differ. A spurious one costs at most an unnecessary decoder
    reset; a missing one is the exact failure the whole experiment existed
    to catch.

    The target duration is taken from the longest real segment rather than
    assumed. A target duration below a real segment is a malformed
    document, and a malformed document does not fail for one viewer -- it
    hangs every player in the instance.
    """
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{_target_duration(sections)}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for index, section in enumerate(sections):
        if index:
            lines.append("#EXT-X-DISCONTINUITY")
        for segment in section.segments:
            lines.append(f"#EXTINF:{segment.duration_seconds:.3f},")
            lines.append(segment.url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def validate_manifest(sections, known_segment_keys, *, document=None) -> ManifestVerdict:
    """What stands between a malformed document and every player in a
    VRChat instance. Not defence in depth, not optional: a player handed a
    document naming a segment that is not there stalls, and it stalls for
    the whole room rather than for one viewer. This is the largest blast
    radius anything in this project has, and there is no
    send-it-anyway branch behind this function.

    Checks, in order: there is at least one segment; every segment names a
    key that is actually in `known_segment_keys`; every duration is a
    positive number; the declared target duration is at least the longest
    segment; a boundary marker sits between every pair of members; and the
    document terminates.

    The last two are checked against the rendered text rather than against
    the sections, because the text is what goes out. `document` defaults
    to rendering the sections here, so a caller that validates before
    rendering gets the same answer; a caller that already rendered passes
    the exact bytes it is about to send.
    """
    segments = [seg for section in sections for seg in section.segments]
    if not segments:
        return ManifestVerdict(
            ok=False,
            reason_key=REASON_KEY_MANIFEST_EMPTY,
            reason_params={
                "detail": "no segments: a player handed an empty list stalls, "
                "and in a VRChat world it stalls for everyone in the instance"
            },
        )

    missing = [seg.object_key for seg in segments if seg.object_key not in known_segment_keys]
    if missing:
        return ManifestVerdict(
            ok=False,
            reason_key=REASON_KEY_MANIFEST_MISSING_SEGMENT,
            reason_params={
                "keys": ", ".join(missing),
                "detail": "a segment the document names does not exist: every "
                "player in the instance stalls on it, not just the one that "
                "asked",
            },
        )

    bad = [
        seg.object_key
        for seg in segments
        if not isinstance(seg.duration_seconds, (int, float))
        or seg.duration_seconds <= 0
    ]
    if bad:
        return ManifestVerdict(
            ok=False,
            reason_key=REASON_KEY_MANIFEST_BAD_DURATION,
            reason_params={
                "keys": ", ".join(bad),
                "detail": "a segment declares a duration of zero or less, which "
                "leaves the player with no timeline to schedule and hangs the "
                "whole instance",
            },
        )

    text = render_manifest(sections) if document is None else document

    declared = None
    for line in text.splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                declared = int(line.split(":", 1)[1])
            except ValueError:
                declared = None
            break
    longest = max(seg.duration_seconds for seg in segments)
    if declared is None or declared < longest:
        return ManifestVerdict(
            ok=False,
            reason_key=REASON_KEY_MANIFEST_TARGET_DURATION,
            reason_params={
                "declared": declared,
                "longest": longest,
                "detail": "the declared target duration is below a real segment, "
                "which is a malformed document -- served, it hangs every player "
                "in the instance",
            },
        )

    expected_boundaries = max(len(sections) - 1, 0)
    found_boundaries = text.count("#EXT-X-DISCONTINUITY")
    if found_boundaries < expected_boundaries:
        return ManifestVerdict(
            ok=False,
            reason_key=REASON_KEY_MANIFEST_MISSING_BOUNDARY,
            reason_params={
                "expected": expected_boundaries,
                "found": found_boundaries,
                "detail": "a boundary between two clips is unmarked, so the "
                "player never resets its decoder at the change -- the failure "
                "the boundary experiment existed to catch, and it takes the "
                "whole instance with it",
            },
        )

    if "#EXT-X-ENDLIST" not in text:
        return ManifestVerdict(
            ok=False,
            reason_key=REASON_KEY_MANIFEST_TRUNCATED,
            reason_params={
                "detail": "the document does not terminate, so the player waits "
                "for more of a list that will never grow, and every viewer in "
                "the instance waits with it"
            },
        )

    return ManifestVerdict(ok=True, reason_key=None, reason_params={})
