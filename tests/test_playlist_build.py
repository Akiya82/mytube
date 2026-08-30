"""Pure functions only -- no ffmpeg, no database, in the style of
tests/test_remux.py. Every test imports its subject inside its own body
rather than at module level, so a module that is absent reports as its own
failure instead of one collection error hiding the rest of the file.

The two tests that used to pin argument builders for a physically joined
file are gone, and their absence is deliberate rather than convenient.
The mechanism this project assembles with was settled by measurement --
a video player inside VRChat carries a boundary between clips whose
resolution, frame rate or audio channel layout differ, so the members are
listed in a generated manifest and never joined into one file. Those
builders have no subject any more. What replaced them is the validator
that stands in front of the generated document, which had no
specification anywhere and is the one failure in this project that
reaches every viewer in an instance at once rather than one of them.
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
    """The two findings stay distinguishable in the log, and neither is
    spelled as a translation catalogue key. A clip that was simply added
    before anything measured it has not been found to differ from
    anything, so reporting it as a mismatch would report something untrue;
    and since this verdict is logged rather than shown, a key in the
    namespace the interface renders from would promise a sentence nothing
    can display.
    """
    from orchestrator.media.playlist_build import (
        PARITY_LOG_MISMATCH,
        PARITY_LOG_UNMEASURED,
        parity_verdict,
    )

    assert PARITY_LOG_MISMATCH != PARITY_LOG_UNMEASURED
    for tag in (PARITY_LOG_MISMATCH, PARITY_LOG_UNMEASURED):
        assert not tag.startswith("playlist."), (
            f"{tag!r} sits in the namespace the library row renders from, "
            "but nothing writes this verdict to a row -- a translated "
            "sentence no code can reach reads as a warning the operator "
            "was given"
        )

    members = [_member("Clip A"), _member("Clip B", fps=None)]
    verdict = parity_verdict(members)

    assert verdict.reason_key == PARITY_LOG_UNMEASURED


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


def _section(title_name, *durations, prefix="seg"):
    """One member's worth of segments, already signed. `object_key` carries
    the extension the delivery address pattern demands, exactly as
    `playlist_artifacts` stores it.
    """
    from orchestrator.media.playlist_build import Section, Segment

    segments = tuple(
        Segment(
            object_key=f"{prefix}-{index}.mp4",
            duration_seconds=duration,
            url=f"https://mytube.example/blob/{prefix}-{index}.mp4?exp=1&sig=x",
        )
        for index, duration in enumerate(durations)
    )
    return Section(title_name=title_name, segments=segments)


def _known(*sections):
    return {seg.object_key for section in sections for seg in section.segments}


def test_render_manifest_marks_every_boundary_between_two_members():
    from orchestrator.media.playlist_build import render_manifest

    a = _section("Clip A", 8.0, prefix="a")
    b = _section("Clip B", 12.0, prefix="b")
    c = _section("Clip C", 4.0, prefix="c")

    document = render_manifest([a, b, c])

    assert document.startswith("#EXTM3U\n")
    assert document.count("#EXT-X-DISCONTINUITY") == 2, (
        "a boundary marker goes between every pair of members, emitted "
        "without first asking whether their parameters differ -- a spurious "
        "one costs a decoder reset, a missing one is the failure the "
        "boundary measurement existed to catch"
    )
    # Taken from the longest real segment, never assumed: a target duration
    # below a real segment is malformed, and malformed hangs the instance.
    assert "#EXT-X-TARGETDURATION:12" in document
    assert document.rstrip().endswith("#EXT-X-ENDLIST")


def test_validate_manifest_refuses_a_segment_that_is_not_there():
    from orchestrator.media.playlist_build import validate_manifest

    present = _section("Clip A", 8.0, prefix="a")
    absent = _section("Clip B", 8.0, prefix="ghost")

    verdict = validate_manifest([present, absent], _known(present))

    assert verdict.ok is False
    assert verdict.reason_key is not None
    assert "ghost-0.mp4" in verdict.reason_params["keys"]
    assert "instance" in verdict.reason_params["detail"], (
        "the failure has to say what serving it would do -- a player stalls "
        "for every viewer in the world, not for the one that asked"
    )


def test_validate_manifest_refuses_a_duration_that_cannot_be_scheduled():
    from orchestrator.media.playlist_build import validate_manifest

    section = _section("Clip A", 8.0, 0.0, prefix="a")

    verdict = validate_manifest([section], _known(section))

    assert verdict.ok is False
    assert "a-1.mp4" in verdict.reason_params["keys"]
    assert "instance" in verdict.reason_params["detail"]


def test_validate_manifest_refuses_a_document_whose_boundary_was_lost():
    from orchestrator.media.playlist_build import render_manifest, validate_manifest

    a = _section("Clip A", 8.0, prefix="a")
    b = _section("Clip B", 8.0, prefix="b")
    known = _known(a, b)

    document = render_manifest([a, b])
    assert validate_manifest([a, b], known, document=document).ok is True

    mangled = document.replace("#EXT-X-DISCONTINUITY\n", "")
    verdict = validate_manifest([a, b], known, document=mangled)

    assert verdict.ok is False, (
        "the bytes that go out are what gets validated, not the structure "
        "they were rendered from"
    )
    assert verdict.reason_params["found"] == 0
    assert "instance" in verdict.reason_params["detail"]


def test_validate_manifest_refuses_an_empty_list_and_a_truncated_one():
    from orchestrator.media.playlist_build import render_manifest, validate_manifest

    empty = validate_manifest([], set())
    assert empty.ok is False
    assert "instance" in empty.reason_params["detail"]

    section = _section("Clip A", 8.0, prefix="a")
    truncated = render_manifest([section]).replace("#EXT-X-ENDLIST\n", "")
    verdict = validate_manifest([section], _known(section), document=truncated)

    assert verdict.ok is False
    assert "instance" in verdict.reason_params["detail"]


def test_the_segment_command_copies_packets_and_never_re_encodes():
    from orchestrator.media.playlist_build import build_segment_command

    cmd = build_segment_command("/work/member.mp4", "/work/member.ts")

    assert isinstance(cmd, list)
    assert all(isinstance(arg, str) for arg in cmd)
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-f") + 1] == "mpegts"
    # Nothing is encoded, so nothing here may name an encoder or a rate --
    # a member is delivered with exactly the parameters it was stored with.
    for forbidden in ("-c:v", "-c:a", "-b:v", "-b:a", "-vf", "-crf"):
        assert forbidden not in cmd
