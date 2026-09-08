"""External audio as a first-class audio source (D-180, D-181, D-182):
the dub search over `RUS Sound/`-shaped folders, matched by episode
number rather than by stem (Pattern 5's generalisation of the existing
sidecar walker); probing a dub file into the same `AudioTrack` shape an
embedded stream takes; the duration-mismatch comparison the interface
warns from; and the mux command contract both builders now carry --
never `-shortest`, since that flag truncates the video to the shorter
dub, exactly backwards for a video stream this project never touches
(Pitfall 4).

The walker and matching tests below use bare touch files, in the style of
`test_subtitles.py`'s own `external_tracks` tests -- nothing here needs a
real container until the mux section, where a real `ffmpeg`/`ffprobe` is
used and every test that needs one is individually marked to skip rather
than fail when neither is on `PATH`.
"""
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_NEEDS_REAL_FFMPEG = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)

_TIMEOUT_SECONDS = 30


def _run(argv):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
    )


# --- the shared walker, searched for a dub (D-180, Pattern 5) -----------


def test_a_dub_beside_the_season_in_the_real_folder_shape_is_found(tmp_path):
    from mytube_media.subtitles import external_audio_tracks

    source = tmp_path / "Show - 05.mkv"
    source.write_bytes(b"")
    dub_dir = tmp_path / "RUS Sound" / "AniStar"
    dub_dir.mkdir(parents=True)
    dub = dub_dir / "AniStar - 05.mka"
    dub.write_bytes(b"")

    candidates = external_audio_tracks(source, tmp_path, episode_number=5)

    assert len(candidates) == 1
    assert candidates[0].path == dub.resolve()


def test_a_candidate_is_paired_by_the_episode_number_in_its_own_name(tmp_path):
    from mytube_media.subtitles import external_audio_tracks

    source = tmp_path / "Show - 05.mkv"
    source.write_bytes(b"")
    dub_dir = tmp_path / "RUS Sound"
    dub_dir.mkdir()
    (dub_dir / "AniStar_03.mka").write_bytes(b"")
    (dub_dir / "AniStar_05.mka").write_bytes(b"")
    (dub_dir / "AniStar_07.mka").write_bytes(b"")

    candidates = external_audio_tracks(source, tmp_path, episode_number=5)

    assert [c.path.name for c in candidates] == ["AniStar_05.mka"]


def test_a_candidate_whose_number_matches_nothing_is_not_returned(tmp_path):
    from mytube_media.subtitles import external_audio_tracks

    source = tmp_path / "Show - 05.mkv"
    source.write_bytes(b"")
    dub_dir = tmp_path / "RUS Sound"
    dub_dir.mkdir()
    (dub_dir / "AniStar_99.mka").write_bytes(b"")

    candidates = external_audio_tracks(source, tmp_path, episode_number=5)

    assert candidates == []


def test_a_resolution_or_a_year_in_the_filename_does_not_pair(tmp_path):
    """A four-digit run is never a standalone 1-3 digit match -- the same
    conservatism the episode-recognition regexes already use.
    """
    from mytube_media.subtitles import _episode_number_from_filename, external_audio_tracks

    assert _episode_number_from_filename("AniStar - 1080p.mka") is None
    assert _episode_number_from_filename("AniStar - 2024.mka") is None

    source = tmp_path / "Show - 05.mkv"
    source.write_bytes(b"")
    dub_dir = tmp_path / "RUS Sound"
    dub_dir.mkdir()
    (dub_dir / "AniStar - 1080p.mka").write_bytes(b"")
    (dub_dir / "AniStar - 2024.mka").write_bytes(b"")

    candidates = external_audio_tracks(source, tmp_path, episode_number=5)

    assert candidates == []


def test_a_candidate_outside_the_share_root_is_refused_by_containment(tmp_path):
    from mytube_media.subtitles import external_audio_tracks

    root = tmp_path / "share"
    root.mkdir()
    source = root / "Show - 05.mkv"
    source.write_bytes(b"")

    outside = tmp_path / "outside" / "AniStar_05.mka"
    outside.parent.mkdir()
    outside.write_bytes(b"")

    link = root / "RUS Sound" / "AniStar_05.mka"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("platform cannot create a symlink without elevation")

    candidates = external_audio_tracks(source, root, episode_number=5)

    for candidate in candidates:
        assert candidate.path.resolve().is_relative_to(root.resolve())


def test_a_candidate_carries_its_relative_path_and_its_folder_segments(tmp_path):
    from mytube_media.subtitles import external_audio_tracks

    source = tmp_path / "Show - 05.mkv"
    source.write_bytes(b"")
    dub_dir = tmp_path / "RUS Sound" / "AniStar"
    dub_dir.mkdir(parents=True)
    (dub_dir / "AniStar_05.mka").write_bytes(b"")

    candidates = external_audio_tracks(source, tmp_path, episode_number=5)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.rel_path == "RUS Sound/AniStar/AniStar_05.mka"
    assert candidate.folder_segments == ("RUS Sound", "AniStar")


def test_an_english_folder_name_is_recognised_alongside_the_russian_one(tmp_path):
    """The product ships in two languages (D-66) -- the search must not
    only recognise the owner's own Russian folder name.
    """
    from mytube_media.subtitles import external_audio_tracks

    source = tmp_path / "Show - 05.mkv"
    source.write_bytes(b"")
    dub_dir = tmp_path / "Sound"
    dub_dir.mkdir()
    (dub_dir / "05.mka").write_bytes(b"")

    candidates = external_audio_tracks(source, tmp_path, episode_number=5)

    assert len(candidates) == 1


# --- one walker, not two (T-05-08, T-05-11) -------------------------------


def test_there_is_still_exactly_one_walk_in_the_module():
    import inspect

    from mytube_media import subtitles

    source = inspect.getsource(subtitles)
    assert source.count("def walk_sidecars") == 1
    assert source.count("os.walk") == 1


# --- probing an external file into the shared AudioTrack shape (D-181) --


def _fake_ffprobe_result(streams, duration="7.007000"):
    payload = {"streams": streams, "format": {"duration": duration}}
    return MagicMock(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")


def test_an_external_file_probes_into_the_same_shape_an_embedded_track_takes(monkeypatch):
    from mytube_media import compat

    def fake_run(argv, **kwargs):
        return _fake_ffprobe_result(
            [
                {
                    "index": 0,
                    "codec_type": "audio",
                    "codec_name": "flac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "channel_layout": "stereo",
                    "tags": {"language": "rus"},
                }
            ]
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    track = compat.external_audio_track("/share/RUS Sound/AniStar/05.mka", folder_label="RUS Sound/AniStar")

    assert isinstance(track, compat.AudioTrack)
    assert track.codec == "flac"
    assert track.channels == 2
    assert track.language == "rus"
    assert track.sample_rate == 48000
    assert track.channel_layout == "stereo"
    assert track.external_path == "/share/RUS Sound/AniStar/05.mka"
    assert track.folder_label == "RUS Sound/AniStar"
    assert track.duration_seconds == pytest.approx(7.007)


def test_an_external_file_with_no_language_tag_leaves_it_absent(monkeypatch):
    from mytube_media import compat

    def fake_run(argv, **kwargs):
        return _fake_ffprobe_result(
            [{"index": 0, "codec_type": "audio", "codec_name": "ac3", "channels": 6}]
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    track = compat.external_audio_track("/share/dub.mka")

    assert track.language is None
    assert track.title is None
    assert track.folder_label is None


# --- the duration-mismatch comparison (D-182) -----------------------------


def test_duration_gap_is_a_plain_number_computed_both_ways_round():
    from mytube_media.compat import duration_gap_seconds

    assert duration_gap_seconds(10.0, 7.0) == pytest.approx(3.0)
    assert duration_gap_seconds(7.0, 10.0) == pytest.approx(3.0)
    assert isinstance(duration_gap_seconds(10.0, 7.0), float)


def test_duration_gap_is_none_when_either_duration_is_unmeasurable():
    from mytube_media.compat import duration_gap_seconds

    assert duration_gap_seconds(None, 7.0) is None
    assert duration_gap_seconds(10.0, None) is None
    assert duration_gap_seconds(None, None) is None


def test_duration_gap_never_refuses_a_large_mismatch():
    """The function itself is a pure comparison -- the refusal that never
    happens is the point (D-182): a mismatch is a warning shown elsewhere,
    never raised here.
    """
    from mytube_media.compat import duration_gap_seconds

    assert duration_gap_seconds(1400.0, 30.0) == pytest.approx(1370.0)


# --- the mux command contract: never -shortest (Pitfall 4, D-180) --------


def _remux_cmd(**overrides):
    from mytube_media.remux import build_remux_command

    kwargs = dict(
        source="/share/movie.mkv",
        destination="/work/out.mp4",
        audio_stream_index=1,
        reencode_audio=False,
        channels=2,
    )
    kwargs.update(overrides)
    source = kwargs.pop("source")
    destination = kwargs.pop("destination")
    return build_remux_command(source, destination, **kwargs)


def _transcode_cmd(**overrides):
    from mytube_media.transcode import SubtitleChoice, TranscodePlan, build_transcode_command

    plan_kwargs = dict(
        width=1920,
        height=1080,
        sample_aspect_ratio="1:1",
        video_bitrate_bps=6_000_000,
        maxrate_bps=round(6_000_000 * 1.33),
        bufsize_bps=round(6_000_000 * 2.0),
        gop=48,
        hwaccel=None,
        audio_stream_index=1,
        audio_channels=2,
        reencode_audio=False,
        stereo_layout="2d",
    )
    plan_kwargs.update(overrides.pop("plan_overrides", {}))
    external_audio = overrides.pop("external_audio", None)
    plan = TranscodePlan(**plan_kwargs)
    subtitle = SubtitleChoice(kind="none")
    return build_transcode_command(
        "/share/movie.mkv", "/work/out.mp4", plan=plan, subtitle=subtitle,
        external_audio=external_audio,
    )


def test_neither_builder_ever_emits_shortest():
    no_external_remux = _remux_cmd()
    with_external_remux = _remux_cmd(external_audio="/share/RUS Sound/dub.mka")
    no_external_transcode = _transcode_cmd()
    with_external_transcode = _transcode_cmd(external_audio="/share/RUS Sound/dub.mka")

    for cmd in (
        no_external_remux,
        with_external_remux,
        no_external_transcode,
        with_external_transcode,
    ):
        assert "-shortest" not in cmd


def test_the_remux_builder_maps_the_external_input_s_own_first_audio_stream():
    cmd = _remux_cmd(external_audio="/share/RUS Sound/dub.mka", channels=6)

    assert cmd.count("-i") == 2
    idx = cmd.index("-i")
    assert cmd[idx + 1] == "/share/movie.mkv"
    idx = cmd.index("-i", idx + 1)
    assert cmd[idx + 1] == "/share/RUS Sound/dub.mka"
    assert "1:a:0" in cmd
    assert "0:1" not in cmd  # the source's own absolute audio index is not mapped
    idx = cmd.index("-c:a")
    assert cmd[idx + 1] == "aac"


def test_the_remux_builder_s_second_input_comes_after_the_first():
    cmd = _remux_cmd(external_audio="/share/dub.mka")

    assert cmd.index("/share/movie.mkv") < cmd.index("/share/dub.mka")


def test_the_remux_builder_still_never_touches_the_video():
    cmd = _remux_cmd(external_audio="/share/dub.mka")

    idx = cmd.index("-c:v")
    assert cmd[idx + 1] == "copy"
    assert "+faststart" in cmd


def test_the_remux_builder_s_absent_path_branch_is_byte_identical_to_before():
    with_none = _remux_cmd(external_audio=None)
    explicit_absent = _remux_cmd()
    assert with_none == explicit_absent
    assert with_none.count("-i") == 1


def test_the_transcode_builder_maps_the_external_input_s_own_first_audio_stream():
    cmd = _transcode_cmd(external_audio="/share/RUS Sound/dub.mka")

    assert cmd.count("-i") == 2
    assert "1:a:0" in cmd
    idx = cmd.index("-c:a")
    assert cmd[idx + 1] == "aac"


def test_the_transcode_builder_s_absent_path_branch_is_unchanged():
    without = _transcode_cmd()
    assert "-shortest" not in without
    assert without.count("-i") == 1


def test_the_transcode_builder_still_carries_faststart_with_an_external_track():
    cmd = _transcode_cmd(external_audio="/share/dub.mka")

    idx = cmd.index("-movflags")
    assert cmd[idx + 1] == "+faststart"


# --- a real run: the video is full length, the dub simply ends (Pitfall 4) --


@_NEEDS_REAL_FFMPEG
def test_a_real_mux_carries_the_full_video_with_the_shorter_dub_ending_early(tmp_path):
    from mytube_media.remux import build_remux_command

    video = tmp_path / "video.mp4"
    dub = tmp_path / "dub.mka"

    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=25:d=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ]
    )
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=48000:duration=7",
            "-c:a", "flac", str(dub),
        ]
    )
    assert video.is_file() and dub.is_file()

    destination = tmp_path / "out.mp4"
    cmd = build_remux_command(
        video, destination,
        audio_stream_index=0, reencode_audio=False, channels=2,
        external_audio=str(dub),
    )
    result = _run(cmd)
    assert result.returncode == 0, result.stderr
    assert destination.is_file()

    probe = _run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(destination),
        ]
    )
    assert probe.returncode == 0, probe.stderr
    probe_json = json.loads(probe.stdout)

    format_duration = float(probe_json["format"]["duration"])
    audio_stream = next(
        s for s in probe_json["streams"] if s.get("codec_type") == "audio"
    )
    audio_duration = float(audio_stream["duration"])

    # The video's own 10 seconds survive whole; the 7-second dub simply
    # ends early -- measured, not asserted from the flag's absence alone.
    assert format_duration == pytest.approx(10.0, abs=0.5)
    assert audio_duration == pytest.approx(7.0, abs=0.5)
    assert audio_duration < format_duration - 1.0
