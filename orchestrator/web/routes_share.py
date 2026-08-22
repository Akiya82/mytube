"""Share browsing: GET /, GET /add/{path:path}, GET /lang/{code}.

Every request path that turns into a filesystem call goes through
`safe_join` first (T-02-01) -- a rejected path renders the
"cannot reach the share" state, never a raw exception page. Breadcrumb and
row links are always built here, from the server-validated relative path,
never by echoing the raw request path back into a link (T-02-01).

The file branch of `share_listing_route` is where `ffprobe` runs (D-56):
once, when the add form opens, and never while drawing a listing. The
directory branch never imports or calls anything from
`orchestrator.media.compat` for exactly that reason.
"""
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import SlugPlan, plan_slug, required_free_bytes
from orchestrator.media.compat import ProbeError, analyze, moov_before_mdat, probe
from orchestrator.media.share import ShareEscape, ShareUnavailable, list_dir, safe_join
from orchestrator.slugs import SlugError
from orchestrator.web.templates import LOCALES, render

router = APIRouter()


def _href(rel_path: str) -> str:
    """`/add/{rel_path}`, each segment percent-encoded so a folder or file
    name with a space or a reserved character still produces a working
    link -- the "/" separators between segments are never encoded, since
    those are ours, not part of any one name.
    """
    if not rel_path:
        return "/add"
    return "/add/" + "/".join(quote(part, safe="") for part in rel_path.split("/"))


def _breadcrumb(rel_path: str) -> list[dict]:
    """One entry per path segment plus the root, each carrying the label to
    display and the href to navigate to -- built from the validated path
    the server already resolved, never from the raw request path.
    """
    crumbs = [{"label": None, "href": "/add", "is_root": True}]
    if not rel_path:
        return crumbs
    parts = rel_path.split("/")
    accumulated: list[str] = []
    for part in parts:
        accumulated.append(part)
        crumbs.append({"label": part, "href": _href("/".join(accumulated)), "is_root": False})
    return crumbs


def _unreachable_response(request: Request, status_code: int):
    return render(
        request,
        "share_listing.html",
        status_code=status_code,
        share_unreachable=True,
        entries=[],
        breadcrumb=_breadcrumb(""),
    )


async def _render_add_form(request: Request, target: Path, rel_path: str):
    """Screen 2 (02-UI-SPEC.md): the verdict, the tracks, the address, and
    whether this exact source has been added before. `ffprobe` runs once,
    right here, for this one file -- D-56.
    """
    settings = get_settings()
    entry_name = target.name

    try:
        probe_json = probe(target)
    except ProbeError:
        probe_json = None

    verdict = None
    if probe_json is not None:
        try:
            moov_first = moov_before_mdat(target)
        except OSError:
            # The probe above already proved the file is readable media;
            # a byte-level read failing here would be a race with the
            # share (file removed/moved mid-request) rather than a real
            # verdict -- treat it the same as "not faststart", the safe
            # default that never claims already_correct incorrectly.
            moov_first = False
        verdict = analyze(probe_json, moov_first=moov_first)

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            slug_plan: SlugPlan = await plan_slug(conn, entry_name)
        except SlugError:
            # A filename that transliterates to nothing usable (pure
            # emoji/punctuation) is a real, if rare, edge on a share this
            # code does not control the naming of -- render the same
            # "cannot reach" state a rejected path already uses rather
            # than a raw 500, and let the operator rename the file on the
            # share and try again.
            return _unreachable_response(request, status_code=400)

        already_added_rows = await queries.editions_by_source_paths(conn, [rel_path])

    already_added = already_added_rows[0] if already_added_rows else None

    reuse_confirmed = request.query_params.get("confirm_reuse") == "1"

    try:
        size_bytes = target.stat().st_size
    except OSError:
        size_bytes = 0
    if verdict is not None and verdict.size_bytes:
        size_bytes = verdict.size_bytes

    already_correct = verdict.already_correct if verdict is not None else False
    free_bytes = shutil.disk_usage(settings.work_dir).free
    not_enough_space = free_bytes < required_free_bytes(
        size_bytes, already_correct=already_correct
    )

    estimate_seconds = 0.0
    if settings.add_estimate_mb_per_second > 0:
        estimate_seconds = size_bytes / (settings.add_estimate_mb_per_second * 1024 * 1024)

    can_add = bool(
        verdict is not None
        and verdict.eligible
        and (not slug_plan.is_tombstone or reuse_confirmed)
        and not not_enough_space
    )

    return render(
        request,
        "add_form.html",
        entry_name=entry_name,
        rel_path=rel_path,
        verdict=verdict,
        probe_failed=probe_json is None,
        slug_plan=slug_plan,
        already_added=already_added,
        reuse_confirmed=reuse_confirmed,
        size_bytes=size_bytes,
        estimate_seconds=estimate_seconds,
        not_enough_space=not_enough_space,
        can_add=can_add,
        reuse_href=_href(rel_path) + "?confirm_reuse=1",
        form_action=_href(rel_path),
    )


@router.get("/")
async def share_root_route():
    return RedirectResponse("/add", status_code=302)


@router.get("/add")
@router.get("/add/{path:path}")
async def share_listing_route(request: Request, path: str = ""):
    settings = get_settings()
    root = Path(settings.share_root)

    try:
        target = safe_join(root, path)
    except ShareEscape:
        return _unreachable_response(request, status_code=400)

    if target.is_file():
        return await _render_add_form(request, target, path)

    try:
        entries = list_dir(target, root)
    except ShareUnavailable:
        return _unreachable_response(request, status_code=503)

    # One query for the whole folder (never one per row, D-48): which of
    # these files already have an edition, keyed by source path.
    file_paths = [entry.rel_path for entry in entries if not entry.is_dir]
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        already_added_rows = await queries.editions_by_source_paths(conn, file_paths)
    already_added_by_path = {row["source_path"]: row for row in already_added_rows}

    rows = [
        {
            "name": entry.name,
            "is_dir": entry.is_dir,
            "size": entry.size,
            "href": _href(entry.rel_path),
            "already_added": already_added_by_path.get(entry.rel_path),
        }
        for entry in entries
    ]

    return render(
        request,
        "share_listing.html",
        share_unreachable=False,
        entries=rows,
        breadcrumb=_breadcrumb(path.strip("/")),
    )


@router.get("/lang/{code}")
async def set_lang_route(code: str, request: Request):
    next_url = request.query_params.get("next", "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"

    response = RedirectResponse(next_url, status_code=302)
    if code in LOCALES:
        response.set_cookie("mytube_lang", code)
    return response
