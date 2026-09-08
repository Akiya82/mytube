"""Failing enumeration contract for `mytube_media.subtitles` (D-121, D-122,
D-136): the three subtitle sources, their search order and the charset
ladder. Font collection and cue parsing arrive with the code that needs
them, in a later plan -- this module does not pre-assert them.

Every subject is imported inside the test body so this module collects
cleanly while `mytube_media.subtitles` does not exist yet.
"""
import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

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


def test_the_walk_stops_at_the_result_cap(tmp_path, monkeypatch):
    """The shared walker (`walk_sidecars`) enforces this, not either
    caller -- proven here through `external_tracks`, the same way
    `test_external_audio.py` proves it through `external_audio_tracks`.
    """
    from mytube_media import subtitles
    from mytube_media.subtitles import external_tracks

    monkeypatch.setattr(subtitles, "MAX_EXTERNAL_RESULTS", 3)
    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")
    for i in range(6):
        (tmp_path / f"Show.l{i}.srt").write_text("x", encoding="utf-8")

    tracks = external_tracks(source, tmp_path)

    assert len(tracks) == 3


def test_the_walk_stops_at_the_visited_directory_cap(tmp_path, monkeypatch):
    from mytube_media import subtitles
    from mytube_media.subtitles import external_tracks

    monkeypatch.setattr(subtitles, "MAX_VISITED_DIRECTORIES", 1)
    source = tmp_path / "Show.mkv"
    source.write_bytes(b"")
    # Two sibling directories under the source's own folder -- the
    # recursive stage visits the source's own folder itself as its first
    # directory, so a cap of 1 must stop before either subdirectory is
    # ever opened.
    for name in ("A", "B"):
        nested = tmp_path / name
        nested.mkdir()
        (nested / "Show.eng.srt").write_text("x", encoding="utf-8")

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


# --- read_text's refusal boundary (WR-03, T-04-41) ---------------------


def test_read_text_refuses_a_path_whose_extension_is_not_a_subtitle_format(
    tmp_path, monkeypatch
):
    """The obvious share mistake: the same stem, the video's own
    extension. Refused before a single byte is read -- a 20 GB file must
    never reach `read_bytes()` because of a typo.
    """
    from mytube_media.subtitles import SubtitleReadError, read_text

    path = tmp_path / "movie.mkv"
    path.write_bytes(b"not actually read")

    def _fail_if_read(*_a, **_k):
        raise AssertionError("read_text must not read a non-subtitle file")

    monkeypatch.setattr(Path, "read_bytes", _fail_if_read)

    with pytest.raises(SubtitleReadError):
        read_text(path)


def test_read_text_refuses_a_file_over_the_size_cap(tmp_path, monkeypatch):
    from mytube_media import subtitles
    from mytube_media.subtitles import SubtitleReadError, read_text

    monkeypatch.setattr(subtitles, "MAX_SUBTITLE_BYTES", 10)
    path = tmp_path / "big.srt"
    path.write_bytes(b"x" * 11)

    def _fail_if_read(*_a, **_k):
        raise AssertionError("read_text must not read an oversized file")

    monkeypatch.setattr(Path, "read_bytes", _fail_if_read)

    with pytest.raises(SubtitleReadError):
        read_text(path)


def test_read_text_still_reads_a_subtitle_file_under_the_cap(tmp_path):
    from mytube_media.subtitles import read_text

    path = tmp_path / "sub.srt"
    path.write_text("Hello", encoding="utf-8")

    text, _encoding = read_text(path)

    assert text == "Hello"


# --- copy_external_track (CR-04's own note, T-04-41) --------------------


def test_copy_external_track_copies_bytes_under_a_generated_stem(tmp_path):
    from mytube_media.subtitles import copy_external_track

    source_dir = tmp_path / "share"
    source_dir.mkdir()
    source = source_dir / "Ocean's Eleven.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = copy_external_track(source, scratch)

    assert result.parent == scratch
    assert result.suffix == ".srt"
    assert result.name != source.name
    assert result.read_bytes() == source.read_bytes()


def test_copy_external_track_refuses_a_non_subtitle_extension(tmp_path):
    from mytube_media.subtitles import SubtitleReadError, copy_external_track

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(SubtitleReadError):
        copy_external_track(source, scratch)


def test_copy_external_track_refuses_an_oversized_file(tmp_path, monkeypatch):
    from mytube_media import subtitles
    from mytube_media.subtitles import SubtitleReadError, copy_external_track

    monkeypatch.setattr(subtitles, "MAX_SUBTITLE_BYTES", 10)
    source = tmp_path / "big.srt"
    source.write_bytes(b"x" * 11)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(SubtitleReadError):
        copy_external_track(source, scratch)


# --- extraction_extension (CR-05) ---------------------------------------


def test_extraction_extension_maps_known_codecs_and_falls_back_for_unknown():
    from mytube_media.subtitles import extraction_extension

    probe_json = {
        "streams": [
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
            {"index": 3, "codec_type": "subtitle", "codec_name": "webvtt"},
            {"index": 4, "codec_type": "subtitle", "codec_name": "ass"},
            {"index": 5, "codec_type": "subtitle", "codec_name": "ssa"},
            {"index": 6, "codec_type": "subtitle", "codec_name": "dvb_subtitle"},
        ]
    }

    assert extraction_extension(probe_json, 2) == "srt"
    assert extraction_extension(probe_json, 3) == "vtt"
    assert extraction_extension(probe_json, 4) == "ass"
    assert extraction_extension(probe_json, 5) == "ass"
    assert extraction_extension(probe_json, 6) == "ass"
    assert extraction_extension(probe_json, 99) == "ass"


# --- cue_times: WebVTT without an hours field (IN-04) -------------------


def test_cue_times_reads_a_webvtt_cue_with_no_hours_field():
    from mytube_media.subtitles import cue_times

    text = "WEBVTT\n\n00:05.000 --> 00:07.000\nhi\n"

    times = cue_times(text)

    assert times == [5.0]


def test_cue_times_still_reads_an_srt_cue_with_hours():
    from mytube_media.subtitles import cue_times

    text = "1\n01:00:05,000 --> 01:00:07,000\nhi\n"

    times = cue_times(text)

    assert times == [3605.0]


# --- bitmap_cue_after's bounded read window (IN-03) ----------------------


def test_bitmap_cue_after_bounds_the_ffprobe_read_to_a_window(monkeypatch):
    from mytube_media import subtitles

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return MagicMock(returncode=0, stdout="12.5\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = subtitles.bitmap_cue_after("/share/movie.mkv", 3, 10.0)

    assert result == 12.5
    idx = captured["argv"].index("-read_intervals")
    assert captured["argv"][idx + 1] == f"10.0%+{subtitles._CUE_SEARCH_WINDOW_SECONDS}"


def test_bitmap_cue_after_returns_none_on_timeout(monkeypatch):
    from mytube_media import subtitles

    def _raise(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)

    assert subtitles.bitmap_cue_after("/share/movie.mkv", 3, 10.0) is None


# --- the font cap bounds the copy, not the leftovers (WR-04, T-04-42) ----


def test_the_font_cap_stops_copying_at_the_file_limit(tmp_path, monkeypatch):
    from mytube_media import subtitles

    subs_dir = tmp_path / "subs"
    subs_dir.mkdir()
    for i in range(60):
        (subs_dir / f"font{i:02d}.ttf").write_bytes(b"x")
    subtitle_path = subs_dir / "movie.srt"
    subtitle_path.write_text("x", encoding="utf-8")

    copy_calls = []
    real_copy2 = subtitles.shutil.copy2

    def _counting_copy2(src, dst):
        copy_calls.append((src, dst))
        return real_copy2(src, dst)

    monkeypatch.setattr(subtitles.shutil, "copy2", _counting_copy2)

    scratch = tmp_path / "scratch"
    result = subtitles.collect_fonts(
        source=None, subtitle_path=str(subtitle_path), scratch_dir=str(scratch)
    )

    assert result is not None
    # The distinguishing assertion: the copy loop itself must never be
    # asked to write more than the cap allows -- a cap enforced only by
    # `_enforce_font_caps` afterwards would still let all 60 calls happen
    # first and only trim the directory once they are all already on disk.
    assert len(copy_calls) <= subtitles.MAX_FONT_FILES


def test_the_font_cap_stops_copying_at_the_byte_limit(tmp_path, monkeypatch):
    from mytube_media import subtitles

    subs_dir = tmp_path / "subs"
    subs_dir.mkdir()
    chunk = 6 * 1024 * 1024  # 6 MiB each, cap is 20 MiB -- the fourth crosses it
    for i in range(5):
        (subs_dir / f"font{i:02d}.ttf").write_bytes(b"x" * chunk)
    subtitle_path = subs_dir / "movie.srt"
    subtitle_path.write_text("x", encoding="utf-8")

    copy_calls = []
    real_copy2 = subtitles.shutil.copy2

    def _counting_copy2(src, dst):
        copy_calls.append((src, dst))
        return real_copy2(src, dst)

    monkeypatch.setattr(subtitles.shutil, "copy2", _counting_copy2)

    scratch = tmp_path / "scratch"
    subtitles.collect_fonts(source=None, subtitle_path=str(subtitle_path), scratch_dir=str(scratch))

    # 3 * 6 MiB = 18 MiB fits; the 4th would push the running total to 24
    # MiB, over the 20 MiB cap, so the loop must stop before it.
    assert len(copy_calls) == 3


# --- attachment dumping never reads the container's own name (T-04-40) --


def test_dump_container_font_attachments_is_a_noop_with_no_attachment_streams(
    tmp_path, monkeypatch
):
    from mytube_media import subtitles

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    destination = tmp_path / "fonts"

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return MagicMock(returncode=0, stdout=json.dumps({"streams": []}).encode())

    monkeypatch.setattr(subprocess, "run", _fake_run)

    subtitles._dump_container_font_attachments(source, destination)

    # only the probe ran -- no ffmpeg dump call was ever built for an
    # empty stream list.
    assert len(calls) == 1
    assert not destination.exists()


def test_dump_container_font_attachments_never_emits_the_bare_selector():
    import inspect

    from mytube_media import subtitles

    source = inspect.getsource(subtitles._dump_container_font_attachments)
    assert "dump_attachment:t" not in source


def test_dump_container_font_attachments_builds_the_dump_argv_before_the_input(
    tmp_path, monkeypatch
):
    """`-dump_attachment` is an input option: ffmpeg refuses to start if it
    is placed after `-i` (the option is read as applying to the output
    file instead).
    """
    from mytube_media import subtitles

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    destination = tmp_path / "fonts"

    probe_streams = {
        "streams": [{"index": 1, "codec_name": "ttf", "tags": {}}]
    }
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "ffprobe":
            return MagicMock(returncode=0, stdout=json.dumps(probe_streams).encode())
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    subtitles._dump_container_font_attachments(source, destination)

    dump_argv = calls[1]
    assert dump_argv.index("-dump_attachment:1") < dump_argv.index("-i")
    assert destination.is_dir()


def test_dump_container_font_attachments_names_each_dump_by_a_generated_name(
    tmp_path, monkeypatch
):
    from mytube_media import subtitles

    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    destination = tmp_path / "fonts"

    probe_streams = {
        "streams": [
            {"index": 1, "codec_name": "ttf", "tags": {}},
            {"index": 2, "codec_name": "ttf", "tags": {}},
        ]
    }
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "ffprobe":
            return MagicMock(returncode=0, stdout=json.dumps(probe_streams).encode())
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    subtitles._dump_container_font_attachments(source, destination)

    dump_argv = calls[1]
    targets = [dump_argv[i + 1] for i, arg in enumerate(dump_argv) if arg.startswith("-dump_attachment:")]
    assert len(targets) == 2
    assert targets[0] != targets[1]
    assert all(str(destination) in target for target in targets)
    # neither name came from the container -- there is no filename tag in
    # the probe fixture at all for this test to have leaked.
    assert all("attachment_" in target for target in targets)


def test_is_font_attachment_uses_codec_name_when_present():
    from mytube_media.subtitles import _is_font_attachment

    assert _is_font_attachment({"codec_name": "ttf", "tags": {}}) is True
    assert _is_font_attachment({"codec_name": "otf", "tags": {}}) is True
    assert _is_font_attachment({"codec_name": "mjpeg", "tags": {}}) is False


def test_is_font_attachment_falls_back_to_mimetype_without_a_codec_name():
    from mytube_media.subtitles import _is_font_attachment

    assert _is_font_attachment(
        {"codec_name": None, "tags": {"mimetype": "application/x-truetype-font"}}
    ) is True
    assert _is_font_attachment(
        {"codec_name": "", "tags": {"mimetype": "image/jpeg"}}
    ) is False


def test_is_font_attachment_dumps_when_neither_tag_is_usable():
    """A fansub's own loose TTF, muxed with no codec detection and no
    mimetype at all, keeps today's behaviour rather than being refused.
    """
    from mytube_media.subtitles import _is_font_attachment

    assert _is_font_attachment({"codec_name": None, "tags": {}}) is True
    assert _is_font_attachment({}) is True
