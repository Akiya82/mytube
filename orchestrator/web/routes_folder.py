"""Folder-wide add: `GET /add-folder/{path:path}`, the confirmation screen
this phase is named after.

Shows a whole season folder as the series it would become -- one title
(new or existing), every episode's own recognised number, one set of
folder-wide track choices, one action for the whole folder -- before
anything is probed beyond the click that opened it and before anything at
all is written. The save this screen's own form points at is a later
plan's own work; this module is the read side only.

Every rule `orchestrator.web.routes_share`'s own module docstring states
applies here too, for the identical reason: this route shares an event
loop with `GET /v/{slug}` and `/healthz`, so every filesystem call and
every `ffprobe` this module runs goes through `asyncio.to_thread`.
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from mytube_media.share import ShareEscape, ShareUnavailable, list_dir
from orchestrator.config import get_settings
from orchestrator.web.routes_share import (
    MIN_VIDEO_FILES_FOR_FOLDER_ADD,
    _is_video_file,
    _resolve_share_path,
    _unreachable_response,
)

router = APIRouter()


@router.get("/add-folder/{path:path}")
async def folder_confirm_route(request: Request, path: str, group: str = ""):
    settings = get_settings()
    root = Path(settings.share_root)

    # `_resolve_share_path` is the identical `safe_join`-backed containment
    # check every other share route in this project runs -- a second entry
    # point into one mechanism, not a second mechanism (T-05-27).
    try:
        target, rel_path, is_file = await _resolve_share_path(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)

    if is_file:
        return _unreachable_response(request, status_code=400)

    try:
        entries = await asyncio.to_thread(list_dir, target, root)
    except ShareUnavailable:
        return _unreachable_response(request, status_code=503)

    video_entries = sorted(
        (e for e in entries if not e.is_dir and _is_video_file(e.name)),
        key=lambda e: e.name.casefold(),
    )
    if len(video_entries) < MIN_VIDEO_FILES_FOR_FOLDER_ADD:
        # The same answer an unopenable path already gives (D-166's own
        # gate, re-checked here since the route is reachable directly by
        # address, not only through the button that already hides itself
        # below this bound) -- never an empty screen.
        return _unreachable_response(request, status_code=400)

    # The recognition, probing and rendering this screen exists for is a
    # later task's own commit -- this skeleton only proves the path
    # resolves, is contained, and is a real directory with enough video
    # files to be worth a screen at all.
    return PlainTextResponse("")
