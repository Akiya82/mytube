"""Share browsing: GET /, GET /add/{path:path}, GET /lang/{code}.

Every request path that turns into a filesystem call goes through
`safe_join` first (T-02-01) -- a rejected path renders the
"cannot reach the share" state, never a raw exception page. Breadcrumb and
row links are always built here, from the server-validated relative path,
never by echoing the raw request path back into a link (T-02-01).
"""
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from orchestrator.config import get_settings
from orchestrator.media.share import ShareEscape, ShareUnavailable, list_dir, safe_join
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
        # Clicking a file opens the add form (plan 03, D-45). Not built
        # yet here -- redirect back to the parent folder listing rather
        # than 404ing on a path the containment check already accepted.
        # Deliberate placeholder; must be replaced, not left, when plan 03
        # lands the add form on this same route.
        parent_rel = "/".join(path.split("/")[:-1])
        return RedirectResponse(_href(parent_rel), status_code=302)

    try:
        entries = list_dir(target, root)
    except ShareUnavailable:
        return _unreachable_response(request, status_code=503)

    rows = [
        {
            "name": entry.name,
            "is_dir": entry.is_dir,
            "size": entry.size,
            "href": _href(entry.rel_path),
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
