"""3D layout detection over one probe's output plus a filename.

Pure, stdlib-only, no I/O -- the same discipline `compat.py` states for
itself, since `analyze()` calls this once per verdict from data it already
has. Three sources, consulted in this fixed order, each only when the one
before it found nothing: the file's own stereo metadata, then a filename
token, then unambiguous frame geometry as the last resort.

Half-frame stereo (`sbs_half`/`ou_half`) is never guessed from the tag or
from geometry -- nothing in ffprobe's stereo vocabulary distinguishes a
half (anamorphic) frame from a full one, and geometry alone cannot either
(a half-SBS frame is 1920x1080, an entirely ordinary size for a flat
film). Half is reachable only through a filename token or the operator's
own correction in the form.
"""
import re
from pathlib import Path

LAYOUTS = frozenset({"2d", "sbs_full", "sbs_half", "ou_full", "ou_half"})
LAYOUT_SOURCES = frozenset({"tag", "filename", "geometry", "none"})

# Longer tokens first, and matched in this order: "H-SBS" contains "-SBS",
# and "-" is a non-word character, so a plain \b-bounded search for "SBS"
# would match inside "H-SBS" if it were tried first. Checking the half
# tokens before the full ones they are prefixes of is what keeps
# "Movie.H-SBS.mkv" from being read as full SBS.
FILENAME_TOKENS = {
    "HSBS": "sbs_half",
    "H-SBS": "sbs_half",
    "HOU": "ou_half",
    "SBS": "sbs_full",
    "OU": "ou_full",
    "TAB": "ou_full",
    "3D": "sbs_full",
}

# The two frame sizes that are unambiguous -- a full-frame stereo
# picture packed into one image, nothing else this project's library
# produces is this shape. Every other size, including the perfectly
# ordinary 1920x1080 a half-SBS source also reports, is left as flat.
_GEOMETRY_LAYOUTS = {
    (3840, 1080): "sbs_full",
    (1920, 2160): "ou_full",
}

# ffprobe's Matroska stereo tag, lowercase (captured directly from this
# project's own pinned ffmpeg -- see tests/test_stereo3d.py's module
# docstring). The tag alone cannot distinguish a half frame from a full
# one, so every value here maps to a full layout; the correction path for
# a half-frame source whose tag lies is the operator's own selector,
# not a guess made here.
_TAG_LAYOUTS = {
    "left_right": "sbs_full",
    "right_left": "sbs_full",
    "top_bottom": "ou_full",
    "bottom_top": "ou_full",
}

# The side-data entry's own human-readable orientation string, for a file
# whose tag is absent but whose side data still carries the shape --
# same "always full" reasoning as the tag map above.
_SIDE_DATA_LAYOUTS = {
    "side by side": "sbs_full",
    "top and bottom": "ou_full",
}


def _video_stream(probe_json: dict) -> dict | None:
    return next(
        (s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )


def _from_tag_or_side_data(video: dict | None) -> str | None:
    if video is None:
        return None
    tags = video.get("tags") or {}
    layout = _TAG_LAYOUTS.get(tags.get("stereo_mode"))
    if layout is not None:
        return layout
    for entry in video.get("side_data_list") or []:
        if entry.get("side_data_type") != "Stereo 3D":
            continue
        layout = _SIDE_DATA_LAYOUTS.get(entry.get("type"))
        if layout is not None:
            return layout
    return None


def _from_filename(filename: str) -> str | None:
    for token, layout in FILENAME_TOKENS.items():
        if re.search(rf"\b{re.escape(token)}\b", filename, re.IGNORECASE):
            return layout
    return None


def _from_geometry(video: dict | None) -> str | None:
    if video is None:
        return None
    return _GEOMETRY_LAYOUTS.get((video.get("width"), video.get("height")))


def detect_layout(probe_json: dict, filename: str) -> tuple[str, str]:
    """`(layout, source)`, `layout` always one of `LAYOUTS` and `source`
    always one of `LAYOUT_SOURCES`. `source` names which of the three
    checks actually decided it, so the form can tell the operator why it
    pre-filled what it pre-filled.
    """
    video = _video_stream(probe_json)

    layout = _from_tag_or_side_data(video)
    if layout is not None:
        return layout, "tag"

    layout = _from_filename(Path(filename).name)
    if layout is not None:
        return layout, "filename"

    layout = _from_geometry(video)
    if layout is not None:
        return layout, "geometry"

    return "2d", "none"
