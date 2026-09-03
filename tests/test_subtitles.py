"""Failing enumeration contract for `mytube_media.subtitles` (D-121, D-122,
D-136): the three subtitle sources, their search order and the charset
ladder. Font collection and cue parsing arrive with the code that needs
them, in a later plan -- this module does not pre-assert them.

Every subject is imported inside the test body so this module collects
cleanly while `mytube_media.subtitles` does not exist yet.
"""
import copy
import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _subtitle_stream(probe_json: dict, index: int) -> dict:
    return next(s for s in probe_json["streams"] if s.get("index") == index)


# --- embedded enumeration, against the real captured fixture ---------------


def test_an_embedded_text_track_is_listed_with_its_language_and_title():
    from mytube_media.subtitles import embedded_tracks

    probe_json = _load("subtitles_mixed.json")
    tracks = embedded_tracks(probe_json)

    ass_stream = _subtitle_stream(probe_json, 3)
    match = next(t for t in tracks if t.stream_index == 3)
    assert match.kind == "embedded_text"
    assert match.language == ass_stream["tags"]["language"]
    assert match.title == ass_stream["tags"]["title"]


def test_an_embedded_bitmap_track_is_listed_as_a_different_kind():
    """Feed a probe JSON with a `hdmv_pgs_subtitle` stream, built by
    copying the mixed fixture's structure and substituting the codec
    name -- legitimate here because the branch under test is the
    codec-name split, not ffprobe's output shape.
    """
    from mytube_media.subtitles import embedded_tracks

    probe_json = copy.deepcopy(_load("subtitles_mixed.json"))
    bitmap_stream = _subtitle_stream(probe_json, 2)
    bitmap_stream["codec_name"] = "hdmv_pgs_subtitle"

    tracks = embedded_tracks(probe_json)
    match = next(t for t in tracks if t.stream_index == 2)
    assert match.kind == "embedded_bitmap"


def test_a_track_with_no_title_is_not_given_one():
    """This project's own muxer does not persist a per-track title (see
    `tests/test_compat.py`'s equivalent audio-track proof) -- inventing
    one here would put a lie in the picker.
    """
    from mytube_media.subtitles import embedded_tracks

    probe_json = copy.deepcopy(_load("subtitles_mixed.json"))
    stream = _subtitle_stream(probe_json, 2)
    stream["tags"].pop("title", None)

    tracks = embedded_tracks(probe_json)
    match = next(t for t in tracks if t.stream_index == 2)
    assert match.title is None


def test_only_subtitle_streams_are_listed():
    from mytube_media.subtitles import embedded_tracks

    probe_json = _load("subtitles_mixed.json")
    tracks = embedded_tracks(probe_json)

    assert {t.stream_index for t in tracks} == {2, 3}
    assert all(t.kind in ("embedded_text", "embedded_bitmap") for t in tracks)


def test_every_listed_kind_is_in_the_frozen_vocabulary():
    from mytube_media.subtitles import embedded_tracks

    probe_json = copy.deepcopy(_load("subtitles_mixed.json"))
    _subtitle_stream(probe_json, 2)["codec_name"] = "dvd_subtitle"

    tracks = embedded_tracks(probe_json)
    assert all(t.kind in ("none", "embedded_text", "embedded_bitmap", "external") for t in tracks)


def test_the_ordinal_counts_subtitle_streams_not_file_streams():
    """The number the burn-in filter is matched against. In the fixture the
    two subtitle tracks are streams 2 and 3, behind a video and an audio
    stream, so they are the first and second subtitles: 0 and 1.
    """
    from mytube_media.subtitles import subtitle_ordinal

    probe_json = _load("subtitles_mixed.json")

    assert subtitle_ordinal(probe_json, 2) == 0
    assert subtitle_ordinal(probe_json, 3) == 1


def test_an_index_that_is_not_a_subtitle_stream_has_no_ordinal():
    """A video or audio index, or one the file does not carry at all --
    an answer of `None`, never a count that would silently select some
    other track.
    """
    from mytube_media.subtitles import subtitle_ordinal

    probe_json = _load("subtitles_mixed.json")

    assert subtitle_ordinal(probe_json, 0) is None
    assert subtitle_ordinal(probe_json, 1) is None
    assert subtitle_ordinal(probe_json, 99) is None


# --- external discovery, against a real tmp_path tree -----------------------


def test_a_sidecar_beside_the_source_is_found_by_its_stem(tmp_path):
    from mytube_media.subtitles import external_tracks

    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")
    sidecar = tmp_path / "Show.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

    tracks = external_tracks(source, tmp_path)

    assert len(tracks) == 1
    assert tracks[0].kind == "external"
    assert Path(tracks[0].path) == sidecar


def test_a_language_suffix_is_read_from_the_sidecar_name(tmp_path):
    """None of the four sidecars is dropped when they all sit next to
    the same source at once.
    """
    from mytube_media.subtitles import external_tracks

    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")
    suffix_to_ext = {"ru": "srt", "rus": "ass", "eng": "srt", "jpn": "ass"}
    for suffix, ext in suffix_to_ext.items():
        (tmp_path / f"Show.{suffix}.{ext}").write_text("x", encoding="utf-8")

    tracks = external_tracks(source, tmp_path)

    assert len(tracks) == len(suffix_to_ext)
    found_languages = {t.language for t in tracks}
    assert found_languages == set(suffix_to_ext)


def test_the_search_visits_the_named_folders_before_recursing(tmp_path):
    from mytube_media.subtitles import external_tracks

    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")

    same_folder = tmp_path / "Show.eng.srt"
    same_folder.write_text("x", encoding="utf-8")
    subs_folder = tmp_path / "Subs" / "Show.rus.srt"
    subs_folder.parent.mkdir()
    subs_folder.write_text("x", encoding="utf-8")
    subtitles_folder = tmp_path / "Subtitles" / "Show.jpn.srt"
    subtitles_folder.parent.mkdir()
    subtitles_folder.write_text("x", encoding="utf-8")
    deep_nested = tmp_path / "Deep" / "Nested" / "Show.fra.srt"
    deep_nested.parent.mkdir(parents=True)
    deep_nested.write_text("x", encoding="utf-8")

    tracks = external_tracks(source, tmp_path)

    assert [Path(t.path) for t in tracks] == [
        same_folder,
        subs_folder,
        subtitles_folder,
        deep_nested,
    ]


def test_a_subtitle_for_a_different_video_is_not_offered(tmp_path):
    from mytube_media.subtitles import external_tracks

    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")
    (tmp_path / "Other.srt").write_text("x", encoding="utf-8")

    tracks = external_tracks(source, tmp_path)

    assert tracks == []


def test_the_search_never_leaves_the_share_root(tmp_path):
    from mytube_media.subtitles import external_tracks

    root = tmp_path / "share"
    root.mkdir()
    source = root / "Show.mkv"
    source.write_bytes(b"")

    outside = tmp_path / "outside" / "Show.eng.srt"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    link = root / "Show.eng.srt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("platform cannot create a symlink without elevation")

    tracks = external_tracks(source, root)

    for track in tracks:
        assert Path(track.path).resolve().is_relative_to(root.resolve())


def test_a_source_folder_with_no_subtitles_returns_an_empty_list(tmp_path):
    from mytube_media.subtitles import external_tracks

    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")

    tracks = external_tracks(source, tmp_path)

    assert tracks == []


# --- charset ladder -----------------------------------------------------


def test_a_subtitle_file_is_read_as_utf8_when_it_is_utf8(tmp_path):
    from mytube_media.subtitles import read_text

    path = tmp_path / "sub.srt"
    path.write_text("Привет", encoding="utf-8")

    text, encoding = read_text(path)

    assert text == "Привет"
    assert encoding.lower() in ("utf-8", "utf8")


def test_an_older_cyrillic_subtitle_file_is_still_readable(tmp_path):
    """The operator's own library is full of these, and a decode error at
    this point would remove a real track from the picker with no
    explanation.
    """
    from mytube_media.subtitles import read_text

    path = tmp_path / "old.srt"
    path.write_bytes("Привет".encode("cp1251"))

    text, encoding = read_text(path)

    assert text == "Привет"
    assert encoding.lower() not in ("utf-8", "utf8")


def test_a_byte_order_marked_file_is_read_correctly(tmp_path):
    from mytube_media.subtitles import read_text

    path = tmp_path / "bom.srt"
    path.write_bytes("Hello".encode("utf-8-sig"))

    text, _encoding = read_text(path)

    assert text == "Hello"


@pytest.mark.parametrize(
    "content,write_encoding,expect_unicode",
    [
        ("Hello", "utf-8", True),
        ("Hello", "utf-8-sig", True),
        ("Привет", "cp1251", False),
    ],
    ids=["plain_utf8", "bom_utf8", "cp1251"],
)
def test_the_encoding_that_decoded_the_file_is_returned_with_the_text(
    tmp_path, content, write_encoding, expect_unicode
):
    """The cp1251 case's name is something the burn-in filter's own
    character-encoding parameter accepts, not an internal alias, because
    that string is passed straight through into the filter term.
    """
    from mytube_media.subtitles import read_text

    path = tmp_path / "sub.srt"
    path.write_bytes(content.encode(write_encoding))

    _text, encoding = read_text(path)

    if expect_unicode:
        assert encoding.lower() in ("utf-8", "utf8")
    else:
        assert encoding.lower() == "windows-1251"
