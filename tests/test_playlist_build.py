"""Pure functions only -- no ffmpeg, no database, in the style of
tests/test_remux.py. `orchestrator.media.playlist_build` does not exist
yet, so every test imports it inside its own body rather than at module
level: the module can be absent without breaking collection, and each test
reports its own failure instead of one collection error hiding the rest.
"""
import json
import pathlib

import pytest

FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "probe" / "compatible.json"
)


def _load_probe_json():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _member(title_name="Clip", dropped=False, **over):
    """One member's stream parameters, as `parity_verdict` reads them --
    the seven fields `compat.Verdict`/`set_edition_technical` also carry,
    fixed as the same shape across both call sites (see
    tests/test_compat.py, tests/test_queries.py).
    """
    base = {
        "title_name": title_name,
        "dropped": dropped,
        "fps": 24.0,
        "video_profile": "High",
        "video_level": 41,
        "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_sample_rate": 48000,
        "audio_channel_layout": "mono",
    }
    base.update(over)
    return base


def test_build_concat_list_quotes_each_absolute_path_on_its_own_line():
    from orchestrator.media.playlist_build import build_concat_list

    paths = ["/work/a.mp4", "/work/b.mp4", "/work/c.mp4"]
    result = build_concat_list(paths)

    lines = result.splitlines()
    assert len(lines) == 3
    for path, line in zip(paths, lines):
        assert line == f"file '{path}'"
    assert result.endswith("\n")


def test_build_concat_command_carries_the_mandatory_faststart_flag():
    from orchestrator.media.playlist_build import build_concat_command

    cmd = build_concat_command("/work/list.txt", "/work/out.mp4")

    assert isinstance(cmd, list)
    assert all(isinstance(arg, str) for arg in cmd)
    for pair in (("-f", "concat"), ("-safe", "0"), ("-c", "copy"), ("-movflags", "+faststart")):
        idx = cmd.index(pair[0])
        assert cmd[idx + 1] == pair[1], (
            "the concat command must carry -movflags +faststart -- without it "
            "the index sits at the end of the file and a player downloads the "
            "whole thing before the first frame"
        )


def test_stream_parameters_of_a_probe_result_are_read_not_guessed():
    from orchestrator.media.playlist_build import stream_parameters

    probe_json = _load_probe_json()
    params = stream_parameters(probe_json)

    assert params.width == 640
    assert params.height == 360
    assert params.fps == pytest.approx(24.0)
    assert params.video_codec == "h264"
    assert params.video_profile == "High"
    assert params.video_level == 41
    assert params.pix_fmt == "yuv420p"
    assert params.audio_codec == "aac"
    assert params.audio_sample_rate == 48000
    assert params.audio_channel_layout == "mono"


def test_concat_refusal_names_every_member_that_does_not_match():
    from orchestrator.media.playlist_build import parity_verdict

    members = [
        _member("Clip A"),
        _member("Clip B", fps=30.0),
        _member("Clip C", audio_channel_layout="stereo"),
    ]

    verdict = parity_verdict(members)

    assert verdict.ok is False
    names = {m.title_name for m in verdict.mismatches}
    assert names == {"Clip B", "Clip C"}
    params_by_name = {m.title_name: m.parameter for m in verdict.mismatches}
    assert "frame rate" in params_by_name["Clip B"] or "fps" in params_by_name["Clip B"]
    assert "channel" in params_by_name["Clip C"]
    # A catalogue key plus a parameter mapping -- never a formatted English
    # sentence assembled by this function itself.
    assert verdict.reason_key is not None
    assert isinstance(verdict.reason_params, dict)
    assert "Clip B" in " ".join(str(v) for v in verdict.reason_params.values())
    assert "Clip C" in " ".join(str(v) for v in verdict.reason_params.values())


def test_concat_refusal_treats_unmeasured_parameters_as_their_own_mismatch():
    from orchestrator.media.playlist_build import parity_verdict

    members = [_member("Clip A"), _member("Clip B", fps=None)]

    verdict = parity_verdict(members)

    assert verdict.ok is False
    names = {m.title_name for m in verdict.mismatches}
    assert "Clip B" in names, (
        "a file whose parameters were never measured is not a file known "
        "to match -- it must not be silently admitted"
    )


def test_concat_refusal_says_unmeasured_rather_than_mismatched():
    from orchestrator.media.playlist_build import (
        REASON_KEY_PARITY_MISMATCH,
        REASON_KEY_PARITY_UNMEASURED,
        parity_verdict,
    )

    assert REASON_KEY_PARITY_MISMATCH != REASON_KEY_PARITY_UNMEASURED, (
        "telling an operator two clips are not joinable, about a video "
        "that was simply added before anything measured it, is telling "
        "them something untrue -- the honest sentence names the "
        "measurement, not the clips"
    )

    members = [_member("Clip A"), _member("Clip B", fps=None)]
    verdict = parity_verdict(members)

    assert verdict.reason_key == REASON_KEY_PARITY_UNMEASURED


def test_parity_verdict_admits_identical_members():
    from orchestrator.media.playlist_build import parity_verdict

    members = [_member("Clip A"), _member("Clip B"), _member("Clip C")]

    verdict = parity_verdict(members)

    assert verdict.ok is True
    assert verdict.mismatches == ()
    assert verdict.reason_key is None


def test_parity_verdict_ignores_a_member_whose_video_was_deleted():
    from orchestrator.media.playlist_build import parity_verdict

    members = [
        _member("Clip A"),
        _member("Clip B", dropped=True, fps=999.0, audio_channel_layout="5.1"),
    ]

    verdict = parity_verdict(members)

    assert verdict.ok is True, "a member that will never be part of the artifact must not be counted"


def test_required_build_bytes_accounts_for_the_members_living_in_scratch():
    from orchestrator.media.add import required_free_bytes
    from orchestrator.media.playlist_build import required_build_bytes

    members = [
        {"size_bytes": 1_000_000_000},
        {"size_bytes": 2_000_000_000},
        {"size_bytes": 3_000_000_000},
    ]
    total = sum(m["size_bytes"] for m in members)

    built = required_build_bytes(members)
    add_path_figure = required_free_bytes(total, already_correct=False)

    assert built >= 3 * total
    assert built > add_path_figure, (
        "the add path's formula (2 * source + a poster budget) assumes the "
        "source never enters scratch at all -- a playlist build downloads "
        "every member into scratch, writes an output of the same total "
        "size beside them, and then rewrites that output to move its "
        "index to the front. Reusing the add path's arithmetic here would "
        "pass the precheck on a disk that cannot hold the build."
    )


def test_required_build_bytes_carries_no_poster_budget():
    from orchestrator.media.playlist_build import required_build_bytes

    assert required_build_bytes([]) == 0, (
        "a playlist build extracts no poster frame, so the add path's "
        "fixed poster allowance has nothing to pay for here"
    )
