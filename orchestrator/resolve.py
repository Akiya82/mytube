"""Slug and edition-id resolution: GET /v/{slug}, GET /refresh/{edition_id}.

Both routes only ever redirect (302) to a signed /blob/ URL -- the bytes
themselves are served by edge nginx (plan 03's `slice` + tmpfs cache), not
here. D-06: even though experiment 3 (01-CONTEXT.md D-07) showed an
extensionless address with the right Content-Type plays directly, direct
serving is not implemented -- there is nowhere to put the signature without
dirtying the short link.
"""
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

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

    Trusting `X-Forwarded-Host`/`X-Forwarded-Proto` here rested on `api`
    publishing no port; that stopped being true once the interface arrived
    (D-57 onward) -- `api` now publishes a host port, defaulting to
    loopback. A client that can reach that port directly can set the
    forwarded headers itself and receive a redirect naming a host of its
    choosing, which hands that client a signed URL pointing at a host that
    does not have the bytes: a self-inflicted redirect with no third party
    harmed, on a tool with one operator on a LAN (T-02-03, accepted rather
    than unnoticed). Through `edge`, this is still exactly as safe as
    before: `edge`'s own src/edge/nginx.conf.template unconditionally sets
    `X-Forwarded-Host: $host` / `X-Forwarded-Proto: $scheme` on every
    request it proxies to `/v/` and `/refresh/`, overwriting whatever a
    client sent, so the header value on arrival through that path is always
    edge's own. The moment the interface is reachable from beyond the LAN,
    the forwarded headers need an allowlist instead of this trust.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
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


@router.get("/v/{slug}")
async def resolve_slug_route(slug: str, request: Request):
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.resolve_slug(conn, slug)

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
    async with pool.acquire() as conn:
        row = await queries.resolve_edition(conn, parsed)

    if row is None:
        return Response(status_code=404)
    if row["status"] != "active":
        return Response(status_code=410)

    # retry=1: the D-04 loop-guard marker. edge's map ($secure_link:$arg_retry)
    # sends a second bad-signature attempt to the stub clip instead of
    # bouncing back here again.
    return _sign_redirect(request, parsed, extra_query="&retry=1")
