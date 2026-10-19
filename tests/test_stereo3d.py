"""Failing detection contract for `mytube_media.stereo3d`:
tag beats filename beats geometry, and half is never guessed from a
1920x1080 frame.

Pure detection over captured probe JSON plus a filename -- no I/O beyond
loading a fixture. Every subject is imported inside the test body so this
module collects cleanly while `mytube_media.stereo3d` does not exist yet.

Fixtures were captured locally with this project's own pinned ffmpeg
(`ffmpeg version 2025-10-01-git-...-full_build`) by
`tools/make_probe_fixtures.sh`. Capturing them settled
an open question: the real Matroska tag key is lowercase `stereo_mode` (not
the uppercase `STEREO_MODE` the research document had only cross-
referenced from documentation), and the side-data entry's type name lives
under `side_data_type` ("Stereo 3D"), with the actual orientation as a
human string in its own `type` field ("side by side" / "top and bottom") --
also not what the research document's untested code sketch guessed.
"""

import copy
import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _video_stream(probe_json: dict) -> dict:
    return next(s for s in probe_json["streams"] if s["codec_type"] == "video")


def _stripped_of_stereo_tag(probe_json: dict) -> dict:
    """A deep copy of a real captured fixture with the stereo tag and side
    data removed -- isolates the geometry-only branch the same way
    `tests/test_compat.py` mutates a real, frozen ffprobe dict directly
    rather than hand-writing a synthetic one from scratch.
    """
    data = copy.deepcopy(probe_json)
    video = _video_stream(data)
    video.get("tags", {}).pop("stereo_mode", None)
    video.pop("side_data_list", None)
    return data


# --- the file's own tag wins ------------------------------------------------


def test_the_file_s_own_stereo_tag_wins():
    from mytube_media.stereo3d import detect_layout

    sbs = _load("stereo_sbs_tag.json")
    assert _video_stream(sbs)["tags"]["stereo_mode"] == "left_right"
    assert detect_layout(sbs, "Movie.mkv") == ("sbs_full", "tag")

    ou = _load("stereo_ou_tag.json")
    assert _video_stream(ou)["tags"]["stereo_mode"] == "top_bottom"
    assert detect_layout(ou, "Movie.mkv") == ("ou_full", "tag")


def test_a_tag_beats_a_contradicting_filename():
    from mytube_media.stereo3d import detect_layout

    sbs = _load("stereo_sbs_tag.json")
    assert detect_layout(sbs, "Movie.OU.mkv") == ("sbs_full", "tag")


# --- filename tokens, when no tag is present --------------------------------

_TOKEN_LAYOUTS = [
    ("HSBS", "sbs_half"),
    ("H-SBS", "sbs_half"),
    ("HOU", "ou_half"),
    ("SBS", "sbs_full"),
    ("OU", "ou_full"),
    ("TAB", "ou_full"),
    ("3D", "sbs_full"),
]


@pytest.mark.parametrize("token,expected_layout", _TOKEN_LAYOUTS)
@pytest.mark.parametrize("case", ["upper", "lower"])
def test_a_filename_token_is_read_when_no_tag_is_present(case, token, expected_layout):
    from mytube_media.stereo3d import detect_layout

    # no stereo tag anywhere in this fixture -- captured, not hand-written.
    no_tag_probe = _load("subtitles_mixed.json")
    assert "stereo_mode" not in _video_stream(no_tag_probe).get("tags", {})

    spelling = token.upper() if case == "upper" else token.lower()
    for filename in (f"Movie.{spelling}.mkv", f"Movie [{spelling}] Final.mkv"):
        assert detect_layout(no_tag_probe, filename) == (expected_layout, "filename")


def test_a_token_embedded_inside_an_unrelated_word_does_not_match():
    from mytube_media.stereo3d import detect_layout

    no_tag_probe = _load("subtitles_mixed.json")
    # "OU" sits inside "Shoutout" with no separator on either side -- not a
    # token occurrence, just letters that happen to spell one.
    assert detect_layout(no_tag_probe, "Shoutout.Finale.mkv") == ("2d", "none")


def test_a_longer_token_wins_over_a_shorter_one_it_contains():
    """This is why the token mapping is ordered."""
    from mytube_media.stereo3d import detect_layout

    no_tag_probe = _load("subtitles_mixed.json")
    assert detect_layout(no_tag_probe, "Movie.HSBS.mkv") == ("sbs_half", "filename")
    assert detect_layout(no_tag_probe, "Movie.HOU.mkv") == ("ou_half", "filename")


# --- geometry, the last resort ----------------------------------------------


def test_unambiguous_geometry_is_the_last_resort():
    from mytube_media.stereo3d import detect_layout

    sbs_geometry_only = _stripped_of_stereo_tag(_load("stereo_sbs_tag.json"))
    assert detect_layout(sbs_geometry_only, "Movie.mkv") == ("sbs_full", "geometry")

    ou_geometry_only = _stripped_of_stereo_tag(_load("stereo_ou_tag.json"))
    assert detect_layout(ou_geometry_only, "Movie.mkv") == ("ou_full", "geometry")


def test_a_normal_frame_is_never_guessed_as_half_stereo():
    """Guessing half side by side from an ordinary frame size would mark
    every flat film in the library as stereo and refuse subtitles on all
    of them.
    """
    from mytube_media.stereo3d import detect_layout

    normal_frame = _load("subtitles_mixed.json")  # 1920x1080, no tag
    assert _video_stream(normal_frame)["width"] == 1920
    assert _video_stream(normal_frame)["height"] == 1080
    assert detect_layout(normal_frame, "Movie.mkv") == ("2d", "none")


# --- the frozen vocabulary ---------------------------------------------------


def test_every_returned_layout_is_in_the_frozen_vocabulary():
    from mytube_media.stereo3d import LAYOUT_SOURCES, LAYOUTS, detect_layout

    cases = [
        (_load("stereo_sbs_tag.json"), "Movie.mkv"),
        (_load("stereo_ou_tag.json"), "Movie.mkv"),
        (_stripped_of_stereo_tag(_load("stereo_sbs_tag.json")), "Movie.mkv"),
        (_stripped_of_stereo_tag(_load("stereo_ou_tag.json")), "Movie.mkv"),
        (_load("subtitles_mixed.json"), "Movie.mkv"),
        (_load("subtitles_mixed.json"), "Movie.HSBS.mkv"),
        (_load("subtitles_mixed.json"), "Movie.OU.mkv"),
    ]
    for probe_json, filename in cases:
        layout, source = detect_layout(probe_json, filename)
        assert layout in LAYOUTS
        assert source in LAYOUT_SOURCES


def test_the_frozen_vocabulary_has_exactly_the_five_layouts_and_four_sources():
    from mytube_media.stereo3d import LAYOUT_SOURCES, LAYOUTS

    assert LAYOUTS == frozenset({"2d", "sbs_full", "sbs_half", "ou_full", "ou_half"})
    assert LAYOUT_SOURCES == frozenset({"tag", "filename", "geometry", "none"})


def test_the_filename_token_mapping_is_ordered_longer_tokens_first():
    from mytube_media.stereo3d import FILENAME_TOKENS

    tokens = list(FILENAME_TOKENS.keys())
    assert tokens.index("HSBS") < tokens.index("SBS")
    assert tokens.index("H-SBS") < tokens.index("SBS")
    assert tokens.index("HOU") < tokens.index("OU")
