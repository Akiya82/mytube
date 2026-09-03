"""Failing argv contract for `mytube_media.transcode` (CONV-04..08, D-133,
D-135, D-136, D-137, D-140) -- pure argv-builder assertions in the style
of `tests/test_remux.py`, no ffmpeg process started anywhere in this
file.

Every subject is imported inside the test body (or inside one of the
`_plan`/`_subtitle` factories below, themselves only ever called from
inside a test body) so this module collects cleanly while
`mytube_media.transcode` does not exist yet -- a missing module reports
as a failing assertion per test, never a collection error that hides the
rest of the file.

This module also fixes the public surface plan 07 implements against:
the constants, the two dataclasses (`TranscodePlan`, `SubtitleChoice`),
the `FilterGraph` return type and the `TranscodeRefused` exception. A
later plan may not rename any of these without this file failing loudly.
"""
import pytest


# --- shared factories (import deferred to call time) ------------------------


def _plan(**overrides):
    from mytube_media.transcode import TranscodePlan

    defaults = dict(
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
    defaults.update(overrides)
    return TranscodePlan(**defaults)


def _subtitle(**overrides):
    from mytube_media.transcode import SubtitleChoice

    defaults = dict(
        kind="none",
        stream_index=None,
        path=None,
        language=None,
        title=None,
        font_dir=None,
        charenc=None,
    )
    defaults.update(overrides)
    return SubtitleChoice(**defaults)


# a path carrying the three characters the filter grammar has to escape:
# a single quote, a colon and a backslash.
_DANGEROUS_SUBTITLE_PATH = r"/share/O'Brien: back\slash sub.srt"


# --- encoder flags: the part CLAUDE.md already paid for once ---------------


def test_the_transcode_argv_carries_the_explicit_pixel_format_profile_and_level():
    """A source that leaves the pixel format implicit hands a ten-bit
    stream to a player that shows a black screen (`.claude/CLAUDE.md`
    trap 2) -- adjacency by index, not membership, because a flag whose
    value drifted to another position is not the same command.
    """
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv", "/work/out.mp4", plan=_plan(), subtitle=_subtitle()
    )

    idx = cmd.index("-pix_fmt")
    assert cmd[idx + 1] == "yuv420p"
    idx = cmd.index("-profile:v")
    assert cmd[idx + 1] == "high"
    idx = cmd.index("-level")
    assert cmd[idx + 1] == "4.1"


def test_the_transcode_argv_ends_the_output_with_faststart():
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv", "/work/out.mp4", plan=_plan(), subtitle=_subtitle()
    )

    idx = cmd.index("-movflags")
    assert cmd[idx + 1] == "+faststart"


def test_the_transcode_argv_asks_for_machine_readable_progress():
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv", "/work/out.mp4", plan=_plan(), subtitle=_subtitle()
    )

    idx = cmd.index("-progress")
    assert cmd[idx + 1] == "pipe:1"
    assert "-nostats" in cmd


def test_the_transcode_argv_uses_the_nvenc_encoder_with_the_locked_preset():
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv", "/work/out.mp4", plan=_plan(), subtitle=_subtitle()
    )

    idx = cmd.index("-c:v")
    assert cmd[idx + 1] == "h264_nvenc"
    idx = cmd.index("-preset")
    assert cmd[idx + 1] == "p5"
    idx = cmd.index("-rc")
    assert cmd[idx + 1] == "vbr"


@pytest.mark.parametrize("fps", [23.976, 25, 29.97, 50, 60])
def test_the_gop_is_two_seconds_of_frames(fps):
    from mytube_media.transcode import build_transcode_command, gop_for

    expected = gop_for(fps)
    assert expected == round(fps * 2)

    cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(gop=expected),
        subtitle=_subtitle(),
    )
    idx = cmd.index("-g")
    assert cmd[idx + 1] == str(expected)
    idx = cmd.index("-keyint_min")
    assert cmd[idx + 1] == str(expected)


def test_gop_for_none_has_a_defined_fallback_rather_than_raising():
    from mytube_media.transcode import gop_for

    assert gop_for(None) > 0


# --- rate control -------------------------------------------------------


def test_the_bitrate_scales_with_output_area():
    from mytube_media.transcode import bitrate_for_output

    assert bitrate_for_output(1920, 1080, "2d") == 6_000_000
    assert bitrate_for_output(1280, 720, "2d") == pytest.approx(2_670_000, rel=0.01)


def test_a_4k_source_is_capped_at_the_1080p_figure_because_the_output_scales_down_first():
    from mytube_media.transcode import bitrate_for_output, output_dimensions

    width, height = output_dimensions(3840, 2160, "2d")
    assert (width, height) == (1920, 1080)
    assert bitrate_for_output(width, height, "2d") == 6_000_000


def test_a_full_frame_stereo_source_is_budgeted_by_area_but_never_above_the_ceiling():
    """D-137 authorises scaling the bitrate budget by area, and a
    full-frame stereo frame genuinely carries twice the picture, so it
    earns more than one eye's budget -- but the per-viewer bandwidth
    figure D-136 exists to warn about is the same 9 Mbit/s ceiling, and a
    stereo conversion that shipped at 12 Mbit/s (a bare doubling of the
    reference) would silently exceed the very ceiling the analyzer warns
    other files about. Budget by area, cap at the ceiling.
    """
    from mytube_media.transcode import (
        BITRATE_CEILING_BPS,
        FULL_FRAME_LAYOUTS,
        bitrate_for_output,
    )

    assert "sbs_full" in FULL_FRAME_LAYOUTS
    assert "sbs_half" not in FULL_FRAME_LAYOUTS

    full_frame_bitrate = bitrate_for_output(3840, 1080, "sbs_full")
    assert full_frame_bitrate == BITRATE_CEILING_BPS

    half_frame_bitrate = bitrate_for_output(1920, 1080, "sbs_half")
    assert half_frame_bitrate == 6_000_000


def test_the_ceiling_is_a_parameter_rather_than_a_literal():
    from mytube_media.transcode import bitrate_for_output

    lower_ceiling = 4_000_000
    assert bitrate_for_output(3840, 1080, "sbs_full", ceiling=lower_ceiling) == lower_ceiling


def test_maxrate_and_bufsize_keep_their_ratio_to_the_target():
    from mytube_media.transcode import (
        BUFSIZE_RATIO,
        MAXRATE_RATIO,
        build_transcode_command,
    )

    for target in (3_000_000, 9_000_000):
        cmd = build_transcode_command(
            "/share/movie.mkv",
            "/work/out.mp4",
            plan=_plan(video_bitrate_bps=target),
            subtitle=_subtitle(),
        )
        idx = cmd.index("-maxrate")
        assert cmd[idx + 1] == str(round(target * MAXRATE_RATIO))
        idx = cmd.index("-bufsize")
        assert cmd[idx + 1] == str(round(target * BUFSIZE_RATIO))


# --- scaling and aspect (CONV-08) ----------------------------------------


def test_scaling_never_upsizes():
    from mytube_media.transcode import output_dimensions

    assert output_dimensions(1280, 720, "2d") == (1280, 720)
    width, height = output_dimensions(2560, 1440, "2d")
    assert width == 1920
    assert height % 2 == 0


@pytest.mark.parametrize("layout", ["sbs_full", "sbs_half", "ou_full", "ou_half"])
def test_a_stereo_source_is_never_scaled(layout):
    """Halving the width of a side-by-side frame does not shrink a
    picture, it destroys the pair.
    """
    from mytube_media.transcode import output_dimensions

    assert output_dimensions(3840, 1080, layout) == (3840, 1080)


def test_a_non_square_pixel_aspect_is_carried_through_the_filter_chain():
    """A half-width stereo frame is exactly the anamorphic case this
    protects.
    """
    from mytube_media.transcode import build_filter_chain

    anamorphic = build_filter_chain(
        plan=_plan(stereo_layout="sbs_half", sample_aspect_ratio="2:1"),
        subtitle=_subtitle(kind="none"),
    )
    assert "setsar=2:1" in anamorphic.value

    square = build_filter_chain(
        plan=_plan(stereo_layout="sbs_half", sample_aspect_ratio="1:1"),
        subtitle=_subtitle(kind="none"),
    )
    assert "setsar" not in square.value


# --- the filter chain (CONV-07) ------------------------------------------


def test_scale_comes_before_subtitles_in_the_chain():
    """Subtitles burned before the scale are resampled with the picture
    and go soft.
    """
    from mytube_media.transcode import build_filter_chain

    graph = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(kind="embedded_text", stream_index=2, path="/share/movie.mkv"),
    )
    assert graph.value.index("scale") < graph.value.index("subtitles")


@pytest.mark.parametrize("kind", ["none", "embedded_text", "external"])
def test_the_three_simple_branches_are_a_one_input_graph(kind):
    from mytube_media.transcode import build_filter_chain

    subtitle_kwargs = {"kind": kind}
    if kind == "embedded_text":
        subtitle_kwargs.update(stream_index=2, path="/share/movie.mkv")
    elif kind == "external":
        subtitle_kwargs.update(path="/share/movie.srt")

    graph = build_filter_chain(plan=_plan(), subtitle=_subtitle(**subtitle_kwargs))

    assert graph.flag == "-vf"
    assert graph.video_map == "0:v:0"


def test_a_bitmap_subtitle_track_uses_a_labelled_overlay_graph():
    """The exact shape a real ffmpeg refuses: a two-input labelled graph
    handed to the simple flag is rejected before a frame is read, with a
    message about a simple filtergraph having been expected to have
    exactly one input.
    """
    from mytube_media.transcode import build_filter_chain

    graph = build_filter_chain(
        plan=_plan(), subtitle=_subtitle(kind="embedded_bitmap", stream_index=3)
    )

    assert "overlay" in graph.value
    assert "subtitles=" not in graph.value
    assert graph.flag == "-filter_complex"
    assert graph.video_map != "0:v:0"
    assert graph.video_map in graph.value


def test_an_external_subtitle_file_is_passed_by_path_and_its_path_is_escaped():
    """This is the injection surface the threat model names (T-04-02);
    assert it here rather than trusting the builder.
    """
    import re

    from mytube_media.transcode import build_filter_chain

    graph = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(kind="external", path=_DANGEROUS_SUBTITLE_PATH),
    )

    assert _DANGEROUS_SUBTITLE_PATH not in graph.value

    term = graph.value.split("subtitles=", 1)[1]
    # strip every already-escaped two-character sequence (\X); whatever
    # colon remains unescaped after that must be a structural separator
    # this builder itself introduced, never one from the raw path.
    stripped = re.sub(r"\\.", "", term)
    assert stripped.count(":") <= 1


def test_no_subtitle_choice_produces_a_chain_with_only_scaling():
    from mytube_media.transcode import build_filter_chain

    graph = build_filter_chain(plan=_plan(), subtitle=_subtitle(kind="none"))

    assert "subtitles" not in graph.value
    assert "overlay" not in graph.value


def test_the_font_directory_is_a_filter_parameter_and_never_a_command_flag():
    """A real ffmpeg answers "Unrecognized option" for `-fontsdir` as a
    command-line flag and exits before encoding anything, while
    `ffmpeg -h filter=subtitles` lists the same name as a filter option --
    so the burn-in only ever sees fonts when the value rides inside the
    filter term. <!-- planner-discipline-allow: fontsdir -->
    """
    from mytube_media.transcode import build_filter_chain, build_preview_command, build_transcode_command

    with_dir = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(
            kind="embedded_text", stream_index=2, path="/share/movie.mkv", font_dir="/fonts"
        ),
    )
    assert "fontsdir=/fonts" in with_dir.value

    without_dir = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(kind="embedded_text", stream_index=2, path="/share/movie.mkv"),
    )
    assert "fontsdir" not in without_dir.value

    convert_cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(),
        subtitle=_subtitle(
            kind="embedded_text", stream_index=2, path="/share/movie.mkv", font_dir="/fonts"
        ),
    )
    preview_cmd = build_preview_command(
        "/share/movie.mkv",
        "/work/preview.jpg",
        plan=_plan(),
        subtitle=_subtitle(
            kind="embedded_text", stream_index=2, path="/share/movie.mkv", font_dir="/fonts"
        ),
        at_seconds=90.0,
    )
    for cmd in (convert_cmd, preview_cmd):
        assert "-fontsdir" not in cmd
        assert not any(arg.startswith("-") and "fontsdir" in arg for arg in cmd)


def test_a_non_utf8_subtitle_file_is_burned_with_its_own_encoding():
    """The operator's own library is full of subtitle files in the older
    single-byte Cyrillic encoding, and a burn-in that assumes Unicode
    renders every one of them as mojibake -- a conversion that succeeds,
    uploads, and is wrong.
    """
    from mytube_media.transcode import build_filter_chain

    cyrillic = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(
            kind="embedded_text", stream_index=2, path="/share/movie.mkv", charenc="windows-1251"
        ),
    )
    assert "charenc=windows-1251" in cyrillic.value

    default_unicode = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(kind="embedded_text", stream_index=2, path="/share/movie.mkv", charenc=None),
    )
    assert "charenc" not in default_unicode.value

    explicit_utf8 = build_filter_chain(
        plan=_plan(),
        subtitle=_subtitle(
            kind="embedded_text", stream_index=2, path="/share/movie.mkv", charenc="UTF-8"
        ),
    )
    assert "charenc" not in explicit_utf8.value


# --- decode side (D-133) --------------------------------------------------


@pytest.mark.parametrize("codec", sorted(["h264", "hevc", "vp9", "av1"]))
def test_a_supported_source_codec_decodes_on_the_card(codec):
    from mytube_media.transcode import NVDEC_SUPPORTED_CODECS, build_transcode_command

    assert codec in NVDEC_SUPPORTED_CODECS

    cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(hwaccel="cuda"),
        subtitle=_subtitle(),
    )
    idx_hwaccel = cmd.index("-hwaccel")
    assert cmd[idx_hwaccel + 1] == "cuda"
    assert idx_hwaccel < cmd.index("-i")


def test_an_unsupported_source_codec_decodes_on_the_processor_without_refusing():
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(hwaccel=None),
        subtitle=_subtitle(),
    )
    assert "-hwaccel" not in cmd
    assert "-hwaccel_output_format" not in cmd


# --- audio (D-135) ---------------------------------------------------------


def test_an_aac_source_track_is_copied_rather_than_re_encoded():
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(reencode_audio=False),
        subtitle=_subtitle(),
    )
    idx = cmd.index("-c:a")
    assert cmd[idx + 1] == "copy"
    assert "-b:a" not in cmd


@pytest.mark.parametrize("channels", [2, 6])
def test_a_re_encoded_track_keeps_its_channel_count_and_the_shared_bitrate_formula(channels):
    """A fixed stereo downmix was the older shape; one formula now serves
    both branches.
    """
    from mytube_media.remux import aac_bitrate_for
    from mytube_media.transcode import build_transcode_command

    cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(reencode_audio=True, audio_channels=channels),
        subtitle=_subtitle(),
    )
    idx = cmd.index("-b:a")
    assert cmd[idx + 1] == f"{aac_bitrate_for(channels)}k"
    assert "-ac" not in cmd


@pytest.mark.parametrize("kind", ["none", "embedded_text", "external", "embedded_bitmap"])
def test_only_the_chosen_audio_track_is_mapped(kind):
    from mytube_media.transcode import build_transcode_command

    subtitle_kwargs = {"kind": kind}
    if kind == "embedded_text":
        subtitle_kwargs.update(stream_index=2, path="/share/movie.mkv")
    elif kind == "external":
        subtitle_kwargs.update(path="/share/movie.srt")
    elif kind == "embedded_bitmap":
        subtitle_kwargs.update(stream_index=3)

    cmd = build_transcode_command(
        "/share/movie.mkv",
        "/work/out.mp4",
        plan=_plan(audio_stream_index=4),
        subtitle=_subtitle(**subtitle_kwargs),
    )

    assert cmd.count("-map") == 2
    if kind == "embedded_bitmap":
        assert "[out]" in cmd
        video_map = "[out]"
    else:
        video_map = "0:v:0"
    idx = cmd.index("-map")
    assert cmd[idx + 1] == video_map
    idx = cmd.index("-map", idx + 1)
    assert cmd[idx + 1] == "0:4"


# --- refusal (D-140) --------------------------------------------------------


@pytest.mark.parametrize("layout", ["sbs_full", "sbs_half", "ou_full", "ou_half"])
@pytest.mark.parametrize("kind", ["embedded_text", "embedded_bitmap", "external"])
def test_burning_subtitles_into_a_stereo_source_is_refused_with_a_catalog_key(layout, kind):
    from mytube_media.transcode import TranscodeRefused, build_filter_chain

    subtitle_kwargs = {"kind": kind}
    if kind == "embedded_text":
        subtitle_kwargs.update(stream_index=2, path="/share/movie.mkv")
    elif kind == "external":
        subtitle_kwargs.update(path="/share/movie.srt")
    elif kind == "embedded_bitmap":
        subtitle_kwargs.update(stream_index=3)

    with pytest.raises(TranscodeRefused) as excinfo:
        build_filter_chain(plan=_plan(stereo_layout=layout), subtitle=_subtitle(**subtitle_kwargs))

    assert "." in excinfo.value.reason_key
    assert "ffmpeg" not in str(excinfo.value).lower()


@pytest.mark.parametrize("layout", ["sbs_full", "sbs_half", "ou_full", "ou_half"])
def test_no_subtitle_on_a_stereo_source_is_allowed(layout):
    from mytube_media.transcode import build_filter_chain

    graph = build_filter_chain(plan=_plan(stereo_layout=layout), subtitle=_subtitle(kind="none"))
    assert graph is not None


# --- the preview (CONV-04, D-125/D-126) -------------------------------------


def test_the_preview_command_renders_exactly_one_frame():
    from mytube_media.transcode import build_preview_command

    cmd = build_preview_command(
        "/share/movie.mkv", "/work/preview.jpg", plan=_plan(), subtitle=_subtitle(), at_seconds=90.0
    )
    idx = cmd.index("-frames:v")
    assert cmd[idx + 1] == "1"


def test_the_preview_seeks_to_the_requested_time_before_opening_the_input():
    """Input seeking is what makes the render take seconds instead of
    minutes, and without absolute timestamps the burn-in filter would
    draw the cue from the start of the file instead of the cue at that
    moment.
    """
    from mytube_media.transcode import build_preview_command

    cmd = build_preview_command(
        "/share/movie.mkv", "/work/preview.jpg", plan=_plan(), subtitle=_subtitle(), at_seconds=90.0
    )
    idx_ss = cmd.index("-ss")
    assert cmd[idx_ss + 1] == "90.0"
    assert idx_ss < cmd.index("-i")
    assert "-copyts" in cmd


def test_the_preview_never_asks_for_the_card():
    from mytube_media.transcode import build_preview_command

    cmd = build_preview_command(
        "/share/movie.mkv",
        "/work/preview.jpg",
        plan=_plan(hwaccel="cuda"),
        subtitle=_subtitle(),
        at_seconds=90.0,
    )
    assert "h264_nvenc" not in cmd
    assert "-hwaccel" not in cmd


@pytest.mark.parametrize("kind", ["none", "embedded_text", "external", "embedded_bitmap"])
def test_the_preview_and_the_conversion_share_one_filter_chain(kind):
    """This is the assertion that makes the preview an honest preview,
    and parametrising it is what stops the bitmap branch from being
    previewed through a different graph than it is converted through.
    """
    from mytube_media.transcode import build_preview_command, build_transcode_command

    subtitle_kwargs = {"kind": kind}
    if kind == "embedded_text":
        subtitle_kwargs.update(stream_index=2, path="/share/movie.mkv")
    elif kind == "external":
        subtitle_kwargs.update(path="/share/movie.srt")
    elif kind == "embedded_bitmap":
        subtitle_kwargs.update(stream_index=3)

    plan = _plan()
    subtitle = _subtitle(**subtitle_kwargs)

    convert_cmd = build_transcode_command("/share/movie.mkv", "/work/out.mp4", plan=plan, subtitle=subtitle)
    preview_cmd = build_preview_command(
        "/share/movie.mkv", "/work/preview.jpg", plan=plan, subtitle=subtitle, at_seconds=90.0
    )

    expected_flag = "-filter_complex" if kind == "embedded_bitmap" else "-vf"
    for cmd in (convert_cmd, preview_cmd):
        idx = cmd.index(expected_flag)
        flag, value = cmd[idx], cmd[idx + 1]
        assert flag == expected_flag
        if cmd is convert_cmd:
            shared_value = value
        else:
            assert value == shared_value


def test_the_bitmap_preview_maps_the_graph_output():
    """A preview that mapped the raw video stream would render the frame
    without the subtitle that is the entire reason the operator asked
    for a preview.
    """
    from mytube_media.transcode import build_preview_command

    cmd = build_preview_command(
        "/share/movie.mkv",
        "/work/preview.jpg",
        plan=_plan(),
        subtitle=_subtitle(kind="embedded_bitmap", stream_index=3),
        at_seconds=90.0,
    )
    idx = cmd.index("-map")
    assert cmd[idx + 1] == "[out]"
    assert "0:v:0" not in cmd
    assert "-frames:v" in cmd
    assert "h264_nvenc" not in cmd


# --- free space --------------------------------------------------------


def test_the_free_space_estimate_is_double_the_encoded_output():
    from mytube_media.transcode import required_free_bytes_for_transcode

    encoded_bytes = 6_000_000 * 1440.0 / 8
    assert required_free_bytes_for_transcode(6_000_000, 1440.0) == pytest.approx(encoded_bytes * 2)
