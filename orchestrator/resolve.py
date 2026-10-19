"""Slug and edition-id resolution: GET /v/{slug}, GET /refresh/{edition_id}.

Both routes only ever redirect (302) to a signed /blob/ URL -- the bytes
themselves are served by edge nginx (`slice` + tmpfs cache), not
here. Even though an experiment showed an
extensionless address with the right Content-Type plays directly, direct
serving is not implemented -- there is nowhere to put the signature without
dirtying the short link.
"""

import asyncio
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from orchestrator import auth
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.signing import blob_path, sign_blob_url
from orchestrator.slugs import AddressRefused, parse_public_address

router = APIRouter()


def external_base_url(request: Request) -> str:
    """Build the "scheme://host" prefix for a redirect Location from the
    *requested* host -- never a config constant, since the
    same slug can be reached through more than one hostname (LAN vs. public,
    or a domain change) and the redirect has to stay on whichever one the
    request actually came in on.

    `api` publishes no port at all -- it is reachable only
    through `edge`, which overwrites `X-Forwarded-Host`/`X-Forwarded-Proto`
    on every request it proxies to `/v/` and `/refresh/` (see
    `edge/nginx.conf.template`). A client can still steer the value it
    receives here by sending a hostname that matches neither `server_name`
    in the edge config: such a request falls into the delivery block's own
    catch-all and is forwarded carrying the client's own chosen name, which
    hands that client a signed address pointing at a host that does not
    have the bytes -- a self-inflicted outcome that harms nobody else, the
    same one the previous revision of this docstring accepted, reached by
    a different route.

    One case is handled specially rather than left to that same fate: a
    request arriving on the *interface's own* hostname
    (`admin_fqdn`) is real -- an operator pasting a short link into the tab
    they are already signed in on -- and building the redirect from that
    hostname would point at a `/blob/` address the interface's own server
    block does not serve. That case is caught below and answered from
    `public_base_url` instead, so a short address pasted into the wrong
    tab simply works.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    settings = get_settings()
    if auth.normalize_host(host) == settings.admin_fqdn:
        return settings.public_base_url
    proto = request.headers.get("x-forwarded-proto", "https")
    return f"{proto}://{host}"


def _sign_redirect(
    request: Request, edition_id, extra_query: str = ""
) -> RedirectResponse:
    settings = get_settings()
    sig, exp = sign_blob_url(
        str(edition_id), settings.secure_link_secret, settings.sign_ttl_seconds
    )
    location = (
        f"{external_base_url(request)}{blob_path(str(edition_id))}"
        f"?exp={exp}&sig={sig}{extra_query}"
    )
    return RedirectResponse(location, status_code=302)


# A preparing edition or title answers this status, never 410 or 404 --
# see edge/nginx.conf.template's fifth error_page line and
# tests/test_edge_config.py's own test tying the two together, the same
# pattern orchestrator.web.routes_playlists.PLAYLIST_NOT_READY_STATUS
# already establishes for a playlist that has not finished building. 404,
# 410, 425 and the 403/5xx group are already mapped to the four existing
# stub clips; reusing one of them here would tell a viewer their still-
# converting episode was deleted, or that the address never existed.
EDITION_PREPARING_STATUS = 423


def _readiness_status_code(row) -> int | None:
    """Map one resolved row's title/episode/edition statuses to the HTTP
    answer it deserves, or `None` when the row names a playable edition
    ready to redirect to.

    Shared by all four delivery routes so a fifth route added later
    cannot answer differently from the rest by accident -- see the module
    docstring's own history for why this collapsed into 410 alone before
    this phase.

    Readiness now lives on the episode and the edition, not on the title
    alone: the title's status still decides "gone" for the whole address
    (pending_delete/deleted/failed) and "preparing" while the row itself is
    still being created, but once the title is active, an episode marked
    for deletion or deleted answers gone, an edition (or the title) that is
    preparing answers `EDITION_PREPARING_STATUS`, and a failed edition or a
    resolved row with no edition at all answers not found.

    **Falls back to the title-status-only rule** when the row carries
    neither `episode_status` nor `edition_status` at all -- the shape every
    row had before this phase, and the shape a caller unaware of the new
    columns (a test double built against the old query surface) still hands
    in. Every existing caller keeps its exact prior behaviour.
    """
    data = dict(row)
    title_status = data.get("status")
    episode_status = data.get("episode_status")
    edition_status = data.get("edition_status")
    edition_id = data.get("edition_id")
    if edition_id is None:
        edition_id = data.get("default_edition_id")

    if episode_status is None and edition_status is None:
        if title_status != "active":
            return 410
        return None if edition_id is not None else 404

    if title_status in ("pending_delete", "deleted", "failed"):
        return 410
    if title_status == "preparing":
        return EDITION_PREPARING_STATUS
    if episode_status in ("pending_delete", "deleted"):
        return 410
    if edition_id is None:
        return 404
    if edition_status == "preparing":
        return EDITION_PREPARING_STATUS
    if edition_status == "failed":
        return 404
    if edition_status == "pending_delete":
        # An edition mid-countdown is gone the moment the
        # delete route flips it, exactly like a pending_delete episode two
        # lines above -- the operator's own undo window, not a window a
        # viewer's still-open address gets to play through.
        return 410
    return None


async def _acquire_and(pool, work):
    """`async with pool.acquire() as conn: return await work(conn)`, kept
    as its own coroutine so the caller can bound the whole acquire-and-query
    pair with `asyncio.wait_for` without passing a `timeout=` kwarg
    to `pool.acquire()` itself -- the real `asyncpg.Pool.acquire` accepts
    one, but every test double in this suite's `_DummyPool` classes is a
    bare `def acquire(self):` with no parameters, and this module is not
    the place to widen that shape across a dozen unrelated test files. The
    outer `wait_for` bounds the wait exactly the same way a `timeout=`
    kwarg would, for both the real pool and every test double alike -- the
    same bound `auth.session_operator_id` applies to its own acquire, so
    the two can never disagree.
    """
    async with pool.acquire() as conn:
        return await work(conn)


@router.get("/v/{slug}")
async def resolve_slug_route(slug: str, request: Request):
    pool = request.app.state.db_pool
    try:
        row = await asyncio.wait_for(
            _acquire_and(pool, lambda conn: queries.resolve_slug(conn, slug)),
            timeout=auth.ACQUIRE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return Response(status_code=503)

    if row is None:
        return Response(status_code=404)
    code = _readiness_status_code(row)
    if code is not None:
        return Response(status_code=code)

    return _sign_redirect(request, row["default_edition_id"])


@router.get("/v/{slug}/{segment}")
async def resolve_episode_or_edition_route(slug: str, segment: str, request: Request):
    """A numbered episode (GET /v/{slug}/{number}), or a film's alternative
    edition (GET /v/{slug}/{edition_slug}) -- decided by
    `orchestrator.slugs.parse_public_address` from
    `segment`'s own shape, before either resolver runs: a segment made only
    of digits is an episode number, parsed to an integer, which is what
    collapses every leading-zero form onto the same episode; anything else
    is an edition slug of whatever the bare address plays. This is exactly
    why an edition slug may never be made only of digits (see
    orchestrator.slugs.normalize_edition_slug) -- the two address forms
    would otherwise compete for the same segment shape. A refusal from the
    grammar (an absurdly long digit run, an edition slug that fails the
    edition rule) answers not found, the same as before this moved.
    """
    pool = request.app.state.db_pool

    try:
        address = parse_public_address(f"{slug}/{segment}")
    except AddressRefused:
        return Response(status_code=404)

    if address.number is not None:
        resolver = lambda conn: queries.resolve_episode_slug(
            conn, address.slug, address.number
        )
    else:
        resolver = lambda conn: queries.resolve_title_edition_slug(
            conn, address.slug, address.edition_slug
        )

    try:
        row = await asyncio.wait_for(
            _acquire_and(pool, resolver), timeout=auth.ACQUIRE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return Response(status_code=503)

    if row is None:
        return Response(status_code=404)
    code = _readiness_status_code(row)
    if code is not None:
        return Response(status_code=code)

    return _sign_redirect(request, row["edition_id"])


@router.get("/v/{slug}/{number}/{edition_slug}")
async def resolve_episode_edition_route(
    slug: str, number: str, edition_slug: str, request: Request
):
    """An episode's own alternative edition -- the three-segment address a
    series' numbered episode gets when it, too, carries more than one
    edition. Read through `parse_public_address` exactly
    as the two-segment route is; a refusal (the second segment is not a
    number, an absurdly long digit run, an edition slug that fails the
    edition rule) answers not found, the same as before this moved.
    """
    try:
        address = parse_public_address(f"{slug}/{number}/{edition_slug}")
    except AddressRefused:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    try:
        row = await asyncio.wait_for(
            _acquire_and(
                pool,
                lambda conn: queries.resolve_episode_slug(
                    conn,
                    address.slug,
                    address.number,
                    edition_slug=address.edition_slug,
                ),
            ),
            timeout=auth.ACQUIRE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return Response(status_code=503)

    if row is None:
        return Response(status_code=404)
    code = _readiness_status_code(row)
    if code is not None:
        return Response(status_code=code)

    return _sign_redirect(request, row["edition_id"])


@router.get("/refresh/{edition_id}")
async def refresh_edition_route(edition_id: str, request: Request):
    try:
        parsed = uuid.UUID(edition_id)
    except ValueError:
        return Response(status_code=404)

    pool = request.app.state.db_pool
    try:
        row = await asyncio.wait_for(
            _acquire_and(pool, lambda conn: queries.resolve_edition(conn, parsed)),
            timeout=auth.ACQUIRE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return Response(status_code=503)

    if row is None:
        return Response(status_code=404)
    code = _readiness_status_code(row)
    if code is not None:
        return Response(status_code=code)

    # retry=1: the loop-guard marker. edge's map ($secure_link:$arg_retry)
    # sends a second bad-signature attempt to the stub clip instead of
    # bouncing back here again.
    return _sign_redirect(request, parsed, extra_query="&retry=1")
