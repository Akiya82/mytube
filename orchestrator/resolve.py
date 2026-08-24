"""Slug and edition-id resolution: GET /v/{slug}, GET /refresh/{edition_id}.

Both routes only ever redirect (302) to a signed /blob/ URL -- the bytes
themselves are served by edge nginx (plan 03's `slice` + tmpfs cache), not
here. D-06: even though experiment 3 (01-CONTEXT.md D-07) showed an
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

router = APIRouter()


def external_base_url(request: Request) -> str:
    """Build the "scheme://host" prefix for a redirect Location from the
    *requested* host (ADDR-07, D-11) -- never a config constant, since the
    same slug can be reached through more than one hostname (LAN vs. public,
    or a domain change) and the redirect has to stay on whichever one the
    request actually came in on.

    `api` publishes no port at all as of plan 03 -- it is reachable only
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
    request arriving on the *interface's own* hostname (D-77's
    `admin_fqdn`) is real -- an operator pasting a short link into the tab
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


def _sign_redirect(request: Request, edition_id, extra_query: str = "") -> RedirectResponse:
    settings = get_settings()
    sig, exp = sign_blob_url(
        str(edition_id), settings.secure_link_secret, settings.sign_ttl_seconds
    )
    location = (
        f"{external_base_url(request)}{blob_path(str(edition_id))}"
        f"?exp={exp}&sig={sig}{extra_query}"
    )
    return RedirectResponse(location, status_code=302)


async def _acquire_and(pool, work):
    """`async with pool.acquire() as conn: return await work(conn)`, kept
    as its own coroutine so the caller can bound the whole acquire-and-query
    pair with `asyncio.wait_for` (D-79) without passing a `timeout=` kwarg
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
    if row["status"] != "active":
        return Response(status_code=410)
    if row["default_edition_id"] is None:
        return Response(status_code=404)

    return _sign_redirect(request, row["default_edition_id"])


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
    if row["status"] != "active":
        return Response(status_code=410)

    # retry=1: the D-04 loop-guard marker. edge's map ($secure_link:$arg_retry)
    # sends a second bad-signature attempt to the stub clip instead of
    # bouncing back here again.
    return _sign_redirect(request, parsed, extra_query="&retry=1")
