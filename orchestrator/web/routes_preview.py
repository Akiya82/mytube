"""The on-demand preview frame (CONV-04, D-125, D-126, D-140).

`POST /preview/{path:path}` renders one real frame, with the operator's
currently-chosen subtitle track burned in, through the exact same filter
chain the eventual conversion will use -- `build_preview_command` is the
one function both this route and the worker's own command call through
(`mytube_media.transcode.build_filter_chain`), so what is previewed here
is provably what would be produced. It runs on the api pod's own
processor, in seconds, never on the shared card: no `-hwaccel`, no video
encoder, one frame out.

`GET /preview/frame/{token}.jpg` serves a rendered frame back from
scratch. The identifier is never a caller-supplied path -- it is the
freshly generated name this route itself gave the file when it rendered
it, and the route accepts only that shape (T-04-29). Rendered frames never
reach the bucket (D-125): they live in `settings.work_dir / "previews"`
and are swept by age on every render, exactly the ephemeral scratch
discipline `orchestrator.main._clear_stale_scratch_files` already applies
to the rest of `work_dir`.

**A failed preview never blocks a conversion.** It renders the panel's own
error state and nothing else -- a broken preview does not prove the burn
itself will fail (a corrupt subtitle stream, an unreadable font), and
refusing the conversion over it would trade a real capability (CONV-05/06)
for a cosmetic one (CONV-04). The route's own POST answers `200` in every
case for exactly this reason: the response is always a fragment to swap
in, never an error page, and the fragment's own state (rendered/refused/
error) is what tells the story.

Every blocking call here -- the probe, the subtitle extraction/read, the
font collection, the render itself, and the frame sweep -- goes through
`asyncio.to_thread`, the same rule `orchestrator.web.routes_share`'s own
docstring states for the same reason: this route shares an event loop with
`GET /v/{slug}` and `/healthz`.
"""
import asyncio
import re
import secrets
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from mytube_media.compat import ProbeError, probe
from mytube_media.share import ShareEscape, safe_join
from mytube_media.subtitles import (
    SubtitleExtractError,
    SubtitleReadError,
    collect_fonts,
    cue_times,
    extract_embedded_track,
    first_cue_after,
    bitmap_cue_after,
    missing_font_from_log,
    read_text,
    subtitle_ordinal,
)
from mytube_media.transcode import (
    SubtitleChoice,
    TranscodePlan,
    TranscodeRefused,
    build_preview_command,
)
from orchestrator.config import get_settings
from orchestrator.media.add import SubtitleChoiceRefused, embedded_subtitle_kind, parse_subtitle_choice
from orchestrator.web.routes_share import _resolve_share_path
from orchestrator.web.templates import render

router = APIRouter()

# T-04-29: the frame route accepts only an identifier of this shape -- the
# same alphabet secrets.token_urlsafe produces -- and refuses anything
# else outright. A generated identifier with a strict pattern needs no
# other check to be safe to open inside the scratch directory.
_FRAME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# Generous for a real film's subprocess start-up plus one frame's worth of
# decode -- and finite, so a genuinely wedged ffmpeg cannot occupy the api
# pod's own request loop forever (T-04-30).
PREVIEW_TIMEOUT_SECONDS = 20

# How long a rendered frame is kept before the next render's own sweep
# removes it -- long enough that a slow operator still sees "Try another
# frame" work, short enough that the scratch directory cannot grow without
# bound across a whole evening of adds (T-04-30).
PREVIEW_FRAME_LIFETIME_SECONDS = 300


def _previews_dir(settings) -> Path:
    return Path(settings.work_dir) / "previews"


def _sweep_old_frames(directory: Path) -> None:
    if not directory.is_dir():
        return
    cutoff = time.time() - PREVIEW_FRAME_LIFETIME_SECONDS
    for entry in directory.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def _video_stream(probe_json: dict) -> dict | None:
    return next(
        (s for s in probe_json.get("streams", []) if s.get("codec_type") == "video"), None
    )


def _error_panel(request: Request, *, rel_path: str, state: str = "error"):
    return render(
        request, "partials/preview_panel.html", status_code=200, state=state, rel_path=rel_path
    )


@router.post("/preview/{path:path}")
async def preview_render_route(
    request: Request,
    path: str,
    stereo_layout: str = Form("2d"),
    subtitle_choice: str = Form("none"),
    subtitle_manual_path: str = Form(""),
    after: str = Form("0"),
):
    settings = get_settings()
    root = Path(settings.share_root)

    try:
        source, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return _error_panel(request, rel_path=path)
    if not is_file:
        return _error_panel(request, rel_path=path)

    # From here on every refusal names the canonical rel_path, and a
    # closure keeps every call site below from having to repeat it.
    def panel_error(state: str = "error"):
        return _error_panel(request, rel_path=rel_path, state=state)

    try:
        after_seconds = float(after)
    except (TypeError, ValueError):
        after_seconds = 0.0

    try:
        kind, stream_index, sub_rel_path = parse_subtitle_choice(
            subtitle_choice, manual_path=subtitle_manual_path.strip(), rel_path=rel_path
        )
    except SubtitleChoiceRefused:
        return panel_error()

    if kind == "none":
        # Nothing to preview -- the panel's Idle state renders nothing at
        # all (routes_share never calls this route for it); reaching this
        # route anyway with "none" selected has nothing to show either.
        return panel_error()

    if stereo_layout != "2d":
        # D-140: a preview of a burn-in that will be refused is not worth
        # rendering -- this is the same refusal build_filter_chain would
        # raise, answered here before any process runs.
        return panel_error(state="refused")

    try:
        probe_json = await asyncio.to_thread(probe, source)
    except ProbeError:
        return panel_error()

    video = _video_stream(probe_json)
    if video is None:
        return panel_error()

    scratch_dir = _previews_dir(settings)
    try:
        await asyncio.to_thread(scratch_dir.mkdir, parents=True, exist_ok=True)
    except OSError:
        return panel_error()

    subtitle_path_for_burn: str | None = None
    charenc: str | None = None
    ordinal: int | None = None
    cues: list[float] = []

    if kind == "embedded":
        embedded_kind = await asyncio.to_thread(embedded_subtitle_kind, probe_json, stream_index)
        if embedded_kind is None:
            return panel_error()
        if embedded_kind == "embedded_text":
            try:
                extracted = await asyncio.to_thread(
                    extract_embedded_track, source, stream_index, scratch_dir
                )
                text, charenc = await asyncio.to_thread(read_text, extracted)
            except (SubtitleExtractError, SubtitleReadError, OSError):
                return panel_error()
            subtitle_path_for_burn = str(source)
            # What the burn-in filter counts is subtitle streams, not file
            # streams -- the preview has to hand it the same ordinal the
            # conversion will, or the two render different pictures (and
            # the file index it would otherwise carry renders none).
            ordinal = subtitle_ordinal(probe_json, stream_index)
            cues = cue_times(text)
            at_seconds = first_cue_after(cues, after_seconds)
        else:
            at_seconds = await asyncio.to_thread(
                bitmap_cue_after, source, stream_index, after_seconds
            )
        subtitle = SubtitleChoice(
            kind=embedded_kind,
            stream_index=stream_index,
            subtitle_ordinal=ordinal,
            path=subtitle_path_for_burn,
            font_dir=None,
            charenc=charenc,
        )
    else:  # kind == "external"
        try:
            subtitle_target = safe_join(root, sub_rel_path)
        except ShareEscape:
            return panel_error()
        exists = await asyncio.to_thread(subtitle_target.is_file)
        if not exists:
            return panel_error()
        try:
            text, charenc = await asyncio.to_thread(read_text, subtitle_target)
        except (SubtitleReadError, OSError):
            return panel_error()
        cues = cue_times(text)
        at_seconds = first_cue_after(cues, after_seconds)
        subtitle_path_for_burn = str(subtitle_target)
        subtitle = SubtitleChoice(
            kind="external", path=subtitle_path_for_burn, font_dir=None, charenc=charenc
        )

    if at_seconds is None:
        at_seconds = 0.0

    font_dir = await asyncio.to_thread(
        collect_fonts, source, subtitle_path_for_burn, scratch_dir
    )
    subtitle = SubtitleChoice(
        kind=subtitle.kind,
        stream_index=subtitle.stream_index,
        subtitle_ordinal=subtitle.subtitle_ordinal,
        path=subtitle.path,
        font_dir=font_dir,
        charenc=subtitle.charenc,
    )

    plan = TranscodePlan(
        width=video.get("width") or 0,
        height=video.get("height") or 0,
        sample_aspect_ratio=video.get("sample_aspect_ratio"),
        video_bitrate_bps=0,
        maxrate_bps=0,
        bufsize_bps=0,
        gop=0,
        hwaccel=None,
        audio_stream_index=0,
        audio_channels=0,
        reencode_audio=False,
        stereo_layout=stereo_layout,
    )

    token = secrets.token_urlsafe(16)
    destination = scratch_dir / f"{token}.jpg"

    try:
        cmd = build_preview_command(
            source, destination, plan=plan, subtitle=subtitle, at_seconds=at_seconds
        )
    except TranscodeRefused:
        return panel_error(state="refused")

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, capture_output=True, timeout=PREVIEW_TIMEOUT_SECONDS, check=False
        )

    try:
        result = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return panel_error()

    await asyncio.to_thread(_sweep_old_frames, scratch_dir)

    if result.returncode != 0 or not destination.is_file():
        return panel_error()

    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    font_notice = missing_font_from_log(stderr_text)

    return render(
        request,
        "partials/preview_panel.html",
        status_code=200,
        state="rendered",
        rel_path=rel_path,
        image_url=f"/preview/frame/{token}.jpg",
        frame_time_seconds=at_seconds,
        font_notice=font_notice,
    )


@router.get("/preview/frame/{token}.jpg")
async def preview_frame_route(token: str, request: Request):
    if not _FRAME_ID_RE.match(token):
        return Response(status_code=404)

    settings = get_settings()
    frame_path = _previews_dir(settings) / f"{token}.jpg"

    try:
        exists = await asyncio.to_thread(frame_path.is_file)
    except OSError:
        return Response(status_code=404)
    if not exists:
        return Response(status_code=404)

    try:
        data = await asyncio.to_thread(frame_path.read_bytes)
    except OSError:
        return Response(status_code=404)

    # Never cached client-side (unlike the poster route's year-long cache):
    # the same token is never reused for two different frames, but a
    # frame is swept off disk well before a browser's own cache would
    # naturally expire, and a stale 200 for a swept frame would be worse
    # than a fresh re-render.
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
