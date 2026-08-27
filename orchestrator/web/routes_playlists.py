"""The playlist assembly screen and the public playlist address (D-101..
D-104, D-107, D-108).

Two different surfaces share this module for the same reason
`routes_titles.py` shares the Library screen and the add flow: they are
about the same catalog rows. `GET /playlists/new` and `POST /playlists`
sit behind the operator session, exactly like `/library/*`. `GET
/p/{slug}` is the one public address a video player reaches, unauthenticated
and unsignable from outside -- it joins `/v/` and `/refresh/` in
`orchestrator.auth.HOST_EXEMPT_PREFIXES`.

Every blocking call here goes through the acquire-with-timeout shape
`orchestrator.resolve` already uses, for the same reason that module's own
docstring gives: the event loop is shared with every other request this
process answers, including `GET /v/{slug}`.

The public address deliberately has no tombstone answer. A playlist slug
is a plain unique column with no history table behind it (D-103): renaming
a playlist breaks its old address on purpose, so there is nothing to
tombstone and an address that stops working stops working outright --
`GET /p/{slug}` never answers 410, unlike `/v/{slug}`.
"""
import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from orchestrator import auth
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.resolve import external_base_url
from orchestrator.signing import blob_path, sign_object_key
from orchestrator.slugs import SlugError, normalize_slug, pick_unique_slug
from orchestrator.web.templates import render

router = APIRouter()

# In VRChat every HTTP status a video player receives renders as the same
# black screen, so the only way a viewer can tell "the operator has not
# finished this playlist yet" from "the server is broken" is for the edge
# to answer each with a different stub clip -- and that mapping keys off
# the status code (D-104). The four codes the delivery block already maps
# are 404, 410, 403 and the 5xx group; this one is picked from what is
# free. 425 ("Too Early") is not otherwise meaningful to this
# application -- it exists here purely as a distinct signal edge/nginx.conf.
# template's error_page directive maps to the fourth clip.
PLAYLIST_NOT_READY_STATUS = 425


async def _acquire_and(pool, work):
    """Same shape as `orchestrator.resolve`'s own helper -- kept local
    rather than imported, since it is a private, three-line helper and
    this module's own acquire/timeout pair is bounded by the same shared
    constant (`auth.ACQUIRE_TIMEOUT_SECONDS`) the video address uses.
    """
    async with pool.acquire() as conn:
        return await work(conn)


def _sign_redirect(request: Request, object_key: str) -> RedirectResponse:
    """Build the signed `/blob/` redirect for a built playlist artifact.

    `object_key` is handed straight through to `sign_object_key`/
    `blob_path` with nothing appended -- it already carries its extension,
    mirroring `editions.object_key`'s own stored shape (see
    `orchestrator.signing`'s module docstring for what appending a second
    one would do to the address).
    """
    settings = get_settings()
    sig, exp = sign_object_key(
        object_key, settings.secure_link_secret, settings.sign_ttl_seconds
    )
    location = (
        f"{external_base_url(request)}{blob_path(object_key)}"
        f"?exp={exp}&sig={sig}"
    )
    return RedirectResponse(location, status_code=302)


@router.get("/playlists/new")
async def new_playlist_form_route(request: Request):
    return render(
        request,
        "playlist_form.html",
        editing=False,
        name_value="",
        slug_value="",
        slug_plan=None,
    )


@router.post("/playlists")
async def create_playlist_route(
    request: Request, name: str = Form(...), slug: str = Form("")
):
    """Derive the address through `orchestrator.slugs.normalize_slug` and
    `pick_unique_slug` against `taken_playlist_slugs` -- never `plan_slug`
    and never `taken_slugs`, the video-address machinery: `/p/` and `/v/`
    are separate namespaces under separate path prefixes, and playlists
    keep no tombstone to check (D-103). A collision is a plain collision
    and gets the auto-suffix, visible and editable, with no confirmation
    step -- there is no history here to ask about reusing.
    """
    clean_name = name.strip()
    try:
        candidate = normalize_slug(slug) if slug.strip() else normalize_slug(clean_name)
    except SlugError:
        return render(
            request,
            "playlist_form.html",
            status_code=400,
            editing=False,
            name_value=clean_name,
            slug_value=slug,
            slug_plan=None,
            slug_error=True,
        )

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        taken = await queries.taken_playlist_slugs(conn)
        chosen, _similar = pick_unique_slug(candidate, taken)
        await queries.create_playlist(conn, clean_name, chosen)

    return RedirectResponse("/library", status_code=302)


@router.get("/p/{slug}")
async def resolve_playlist_route(slug: str, request: Request):
    pool = request.app.state.db_pool
    try:
        row = await asyncio.wait_for(
            _acquire_and(pool, lambda conn: queries.resolve_playlist_slug(conn, slug)),
            timeout=auth.ACQUIRE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return Response(status_code=503)

    # Answer 404 for ANY status other than active, not only for a deleted
    # one -- stated as the condition rather than as a list of statuses, so
    # a status added later is refused by default instead of falling
    # through to the built branch below. A playlist inside its cancellable
    # delete grace window is not active; its address must stop resolving
    # the moment the operator presses delete, and there is nothing to
    # tombstone -- from outside, a deletion that can still be cancelled
    # and one that is finished look the same, which is what a namespace
    # with no history means (D-103). Deliberately NOT the branch that
    # answers 410 for a non-active status the way `/v/{slug}` does:
    # playlists keep no address history, so there is no tombstone to
    # answer for.
    if row is None or row["status"] != "active":
        return Response(status_code=404)

    if row["built_at"] is None:
        return Response(status_code=PLAYLIST_NOT_READY_STATUS)

    if row["mode"] == "concat":
        return _sign_redirect(request, row["built_object_key"])

    # mode == "hls": the manifest-generating branch the mechanism plan
    # fills in. Unreachable today -- built_at stays null for this mode
    # until that plan lands, so this row never reaches this point.
    return Response(status_code=PLAYLIST_NOT_READY_STATUS)
