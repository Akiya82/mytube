"""ffmpeg argument construction (orchestrator.media.remux) -- no ffmpeg
invoked anywhere in this file, only the produced argv lists inspected.
"""
import subprocess
from unittest.mock import MagicMock

import pytest

from orchestrator.media.remux import (
    RemuxError,
    aac_bitrate_for,
    build_poster_command,
    build_remux_command,
    poster_timestamp,
    run_ffmpeg,
)

VIDEO_ENCODER_PATTERN = ("libx264", "h264_nvenc", "hevc")


def _copy_audio_cmd():
    return build_remux_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        audio_stream_index=1,
        reencode_audio=False,
        channels=2,
    )


def _reencode_audio_cmd():
    return build_remux_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        audio_stream_index=2,
        reencode_audio=True,
        channels=6,
    )


@pytest.mark.parametrize("cmd_factory", [_copy_audio_cmd, _reencode_audio_cmd])
def test_faststart_present_in_both_branches(cmd_factory):
    assert "+faststart" in cmd_factory()


@pytest.mark.parametrize("cmd_factory", [_copy_audio_cmd, _reencode_audio_cmd])
def test_video_always_copy_never_an_encoder(cmd_factory):
    cmd = cmd_factory()
    idx = cmd.index("-c:v")
    assert cmd[idx + 1] == "copy"
    for arg in cmd:
        for encoder in VIDEO_ENCODER_PATTERN:
            assert encoder not in arg


@pytest.mark.parametrize("cmd_factory", [_copy_audio_cmd, _reencode_audio_cmd])
def test_no_channel_count_argument_in_either_branch(cmd_factory):
    assert "-ac" not in cmd_factory()


def test_copy_branch_uses_c_a_copy():
    cmd = _copy_audio_cmd()
    idx = cmd.index("-c:a")
    assert cmd[idx + 1] == "copy"


def test_reencode_branch_uses_c_a_aac_with_scaled_bitrate():
    cmd = _reencode_audio_cmd()
    idx = cmd.index("-c:a")
    assert cmd[idx + 1] == "aac"
    assert f"{aac_bitrate_for(6)}k" in cmd


def test_maps_video_stream_zero_and_the_absolute_audio_index():
    cmd = build_remux_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        audio_stream_index=3,
        reencode_audio=False,
        channels=2,
    )
    assert "0:v:0" in cmd
    assert "0:3" in cmd
    # exactly one -map for video and one for audio
    assert cmd.count("-map") == 2


def test_source_path_with_special_characters_is_one_argument():
    dangerous = '/share/it\'s "weird"; rm -rf.mkv'
    cmd = build_remux_command(
        dangerous, "/work/out.mp4", audio_stream_index=0, reencode_audio=False, channels=2
    )
    assert dangerous in cmd
    assert cmd.count(dangerous) == 1


def test_aac_bitrate_scales_with_channel_count():
    stereo = aac_bitrate_for(2)
    surround = aac_bitrate_for(6)
    seven_one = aac_bitrate_for(8)
    assert len({stereo, surround, seven_one}) == 3
    # the research's flagged placeholder was a flat 192k -- the 5.1 value
    # must sit above it, not merely differ from it.
    assert surround > 192


def test_aac_bitrate_has_a_floor():
    assert aac_bitrate_for(0) >= 128
    assert aac_bitrate_for(1) >= 128


def test_build_poster_command_seeks_before_input():
    cmd = build_poster_command("/share/movie.mkv", "/work/poster.jpg", at_seconds=12.5)
    assert cmd.index("-ss") < cmd.index("-i")
    assert "12.5" in cmd
    assert "-frames:v" in cmd
    assert cmd[cmd.index("-frames:v") + 1] == "1"


def test_poster_timestamp_ten_percent_capped_at_sixty():
    assert poster_timestamp(100) == pytest.approx(10.0)
    assert poster_timestamp(10_000) == 60.0


def test_poster_timestamp_fallback_when_duration_unknown():
    assert poster_timestamp(None) == 5.0
    assert poster_timestamp(0) == 5.0


def test_no_shell_true_in_module():
    import inspect

    from orchestrator.media import remux as remux_module

    source = inspect.getsource(remux_module)
    assert "shell=True" not in source


def test_run_ffmpeg_raises_remux_error_on_nonzero_exit(monkeypatch):
    fake_result = MagicMock(returncode=1, stderr=b"boom\nline two\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    with pytest.raises(RemuxError):
        run_ffmpeg(["ffmpeg", "-i", "x"], timeout=5)


def test_run_ffmpeg_raises_remux_error_on_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)

    with pytest.raises(RemuxError):
        run_ffmpeg(["ffmpeg", "-i", "x"], timeout=5)


def test_run_ffmpeg_succeeds_silently_on_zero_exit(monkeypatch):
    fake_result = MagicMock(returncode=0, stderr=b"")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    run_ffmpeg(["ffmpeg", "-i", "x"], timeout=5)  # must not raise
