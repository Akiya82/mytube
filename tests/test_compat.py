"""Unit tests for orchestrator.media.compat, driven by real ffprobe output
frozen into tests/fixtures/probe/*.json rather than hand-typed JSON --
02-RESEARCH.md Pitfall 2 flags the H.264 level x10 convention specifically
as the kind of fact that is easy to get subtly wrong from memory, and a
hand-typed fixture would just re-encode the same assumption the test is
supposed to be checking.

Fixtures were generated locally with this project's own pinned ffmpeg
(`ffmpeg version 2025-10-01-git-...-full_build`) and captured with:

    ffmpeg -y -f lavfi -i "testsrc2=size=640x360:rate=24" \\
      -f lavfi -i "sine=frequency=440:sample_rate=48000" -t 2 \\
      -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p \\
      -c:a aac -b:a 128k -movflags +faststart compatible.mp4
    ffprobe -v quiet -print_format json -show_streams -show_format \\
      compatible.mp4 > tests/fixtures/probe/compatible.json

    # hevc.json: -c:v libx265 (rest identical) -> hevc.mp4
    # level51.json: -level 5.1 (rest identical) -> level51.mp4
    # high10_10bit.json: -profile:v high10 -pix_fmt yuv420p10le -> tenbit.mp4
    # multitrack.json: three lavfi inputs (1 video, 2 sine audio), mapped
    #   with -metadata:s:a:0 language=jpn / -metadata:s:a:1 language=eng
    #   (no title tag: this ffmpeg's mov muxer does not persist a per-
    #   track title atom -- see the audio_tracks title-absence test below,
    #   which this fixture's real absence of a title tag proves directly)
    # mixed_codecs.json: same shape, -c:a:0 ac3 -c:a:1 aac, so the first
    #   track (index 1, language rus) is NOT AAC and the second (index 2,
    #   language eng) is
    # no_aac_track.json: single ac3 audio track, no AAC track at all
    # moov_first.mp4 / moov_last.mp4: a 1s libx264 clip built with and
    #   without -movflags +faststart -- kept as real bytes (not JSON)
    #   because moov_before_mdat reads the file's bytes, not ffprobe JSON

`level` field convention: ffmpeg/ffprobe report the H.264 level multiplied
by ten. compatible.json (built as High 4.1) was captured from a real
`ffprobe` run and its JSON literally contains `"level": 41` -- confirmed
below rather than assumed, matching tools/make_control_clip.sh's own
`check_field level 41` assertion against a clip built the same way.
"""
import json
from pathlib import Path

import pytest

from orchestrator.media.compat import (
    AudioTrack,
    ProbeError,
    analyze,
    audio_is_safe,
    audio_tracks,
    moov_before_mdat,
    probe,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "probe"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# --- the level x10 convention, confirmed against the real fixture ----------


def test_compatible_fixture_json_literally_contains_level_41():
    """Assumption A2 in 02-RESEARCH.md, closed by observation: the fixture's
    own JSON must contain the numeric level, not a remembered convention.
    """
    data = _load("compatible.json")
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert video["level"] == 41


# --- analyze(): the eligibility verdict -------------------------------------


def test_compatible_h264_high_41_yuv420p_is_eligible_with_no_reason():
    verdict = analyze(_load("compatible.json"))
    assert verdict.eligible is True
    assert verdict.reason_key is None
    assert verdict.reason_params == {}


def test_hevc_clip_is_not_eligible_with_codec_reason_and_actual_codec_name():
    verdict = analyze(_load("hevc.json"))
    assert verdict.eligible is False
    assert verdict.reason_key == "reason.codec"
    assert verdict.reason_params == {"codec": "hevc"}


def test_high_10_profile_clip_is_not_eligible():
    """The real fixture (libx264, -profile:v high10 -pix_fmt yuv420p10le)
    produces profile "High 10" and pix_fmt "yuv420p10le". _video_eligibility
    checks profile before pix_fmt, so this fixture's real ffprobe output
    trips the profile check first -- reason.profile, not reason.pixfmt.
    Confirmed by generating the fixture locally and reading its own JSON;
    x264 refuses to encode a non-yuv420p pix_fmt under the plain "high"
    profile string at all (verified: forcing -profile:v high with
    -pix_fmt yuv420p10le fails to encode outright), so a real H.264 stream
    can never reach the pixfmt check with a compliant profile string in
    the first place -- reason.pixfmt is exercised directly below instead,
    against a fixture built by mutating this one real dict.
    """
    verdict = analyze(_load("high10_10bit.json"))
    assert verdict.eligible is False
    assert verdict.reason_key == "reason.profile"
    assert verdict.reason_params == {"profile": "High 10"}


def test_ten_bit_pix_fmt_alone_is_rejected_with_pixfmt_reason():
    """No real encoder in this environment produces an H.264 stream with a
    non-yuv420p pix_fmt under a COMPATIBLE_PROFILES-listed profile string
    (see the comment above and in compat.py's Anti-Patterns note) -- x264
    itself refuses that combination. This isolates the pix_fmt branch by
    mutating a real, frozen ffprobe dict's pix_fmt field directly, the way
    a HEVC Main10 or VP9 profile10 source legitimately could report it
    (profile "Main" + pix_fmt "yuv420p10le" is a real ffprobe shape for
    those codecs -- just not reachable from this project's own H.264
    encoder).
    """
    data = _load("compatible.json")
    for stream in data["streams"]:
        if stream["codec_type"] == "video":
            stream["pix_fmt"] = "yuv420p10le"
    verdict = analyze(data)
    assert verdict.eligible is False
    assert verdict.reason_key == "reason.pixfmt"
    assert verdict.reason_params == {}


def test_level_above_41_is_not_eligible_with_no_params():
    verdict = analyze(_load("level51.json"))
    assert verdict.eligible is False
    assert verdict.reason_key == "reason.level"
    assert verdict.reason_params == {}


def test_format_name_matching_splits_on_commas_for_a_real_mp4():
    """compatible.json's format_name is the real comma-separated list
    ffprobe reports for MP4 ("mov,mp4,m4a,3gp,3g2,mj2") -- already_correct
    can only be True at all if the "mp4" membership check splits on commas
    rather than comparing the whole string for equality (02-RESEARCH.md
    Anti-Patterns).
    """
    data = _load("compatible.json")
    assert data["format"]["format_name"] == "mov,mp4,m4a,3gp,3g2,mj2"
    verdict = analyze(data, moov_first=True)
    assert verdict.already_correct is True


# --- audio_tracks(): the per-track list -------------------------------------


def test_audio_tracks_returns_one_entry_per_stream_in_file_order_with_language():
    tracks = audio_tracks(_load("multitrack.json"))
    assert [t.index for t in tracks] == [1, 2]
    assert tracks[0].language == "jpn"
    assert tracks[1].language == "eng"
    assert all(t.codec == "aac" for t in tracks)
    assert all(t.channels == 1 for t in tracks)


def test_audio_tracks_leaves_title_absent_rather_than_invented():
    """This project's ffmpeg mov muxer does not persist a per-track title
    atom (confirmed while generating multitrack.json -- ffmpeg logs
    receiving -metadata:s:a:N title=... but the muxed file's own ffprobe
    output carries no "title" key in that stream's tags), so this real
    fixture already proves the "absent, not invented" requirement without
    needing a synthetic dict.
    """
    tracks = audio_tracks(_load("multitrack.json"))
    assert all(t.title is None for t in tracks)


def test_audio_tracks_language_and_title_absent_when_tags_dict_is_missing_entirely():
    """A minimal, hand-built stream dict for the one shape no real encoder
    in this environment produces -- a stream with no "tags" key at all
    (some non-MP4 containers omit it). Pure data-shaping behavior of
    audio_tracks() itself, not a compatibility verdict, so this one case is
    exempt from the "real ffprobe fixture only" rule the analyzer tests
    above follow.
    """
    probe_json = {
        "streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac", "channels": 2}]
    }
    tracks = audio_tracks(probe_json)
    assert tracks == [AudioTrack(index=0, codec="aac", channels=2, language=None, title=None)]


def test_default_track_is_first_aac_when_one_exists_among_mixed_codecs():
    data = _load("mixed_codecs.json")
    tracks = audio_tracks(data)
    assert tracks[0].codec == "ac3"
    assert tracks[1].codec == "aac"

    verdict = analyze(data)
    assert verdict.default_track_index == tracks[1].index


def test_default_track_is_first_track_when_no_aac_track_exists():
    data = _load("no_aac_track.json")
    tracks = audio_tracks(data)
    assert tracks[0].codec == "ac3"

    verdict = analyze(data)
    assert verdict.default_track_index == tracks[0].index


def test_selected_track_that_is_not_aac_needs_reencode():
    data = _load("no_aac_track.json")
    verdict = analyze(data)
    assert verdict.audio_needs_reencode is True


def test_selected_track_that_is_aac_does_not_need_reencode():
    data = _load("compatible.json")
    verdict = analyze(data)
    assert verdict.audio_needs_reencode is False


# --- already_correct: the one case ffmpeg is never invoked ------------------


def test_already_correct_requires_moov_first_true():
    data = _load("compatible.json")
    assert analyze(data, moov_first=False).already_correct is False
    assert analyze(data, moov_first=True).already_correct is True


def test_already_correct_is_false_with_more_than_one_audio_stream():
    """D-42: only the selected track ever survives into an ffmpeg-produced
    output. An already-correct file is uploaded with no ffmpeg pass at
    all, so it cannot drop the tracks it did not select -- a source with
    two audio streams can never be already-correct, no matter how
    compliant the selected one is.
    """
    data = _load("multitrack.json")
    verdict = analyze(data, selected_audio_index=1, moov_first=True)
    assert verdict.already_correct is False


# --- moov_before_mdat(): real bytes, not ffprobe JSON -----------------------


def test_moov_before_mdat_true_for_a_faststart_file():
    assert moov_before_mdat(FIXTURES_DIR / "moov_first.mp4") is True


def test_moov_before_mdat_false_for_a_file_built_without_faststart():
    assert moov_before_mdat(FIXTURES_DIR / "moov_last.mp4") is False


# --- probe(): never a raw subprocess exception -----------------------------


def test_probe_on_a_non_media_file_raises_probe_error_not_a_raw_exception(tmp_path):
    garbage = tmp_path / "not-a-video.txt"
    garbage.write_text("this is not media data at all", encoding="utf-8")

    with pytest.raises(ProbeError):
        probe(garbage)


def test_probe_on_a_missing_file_raises_probe_error(tmp_path):
    with pytest.raises(ProbeError):
        probe(tmp_path / "does-not-exist.mp4")


# --- audio_is_safe(): only AAC ----------------------------------------------


def test_audio_is_safe_true_only_for_aac():
    assert audio_is_safe({"codec_name": "aac"}) is True
    assert audio_is_safe({"codec_name": "ac3"}) is False
    assert audio_is_safe({"codec_name": "eac3"}) is False
    assert audio_is_safe({"codec_name": "dts"}) is False
