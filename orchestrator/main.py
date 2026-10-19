"""FastAPI application entrypoint.

/healthz, plus /v/{slug} and /refresh/{edition_id}, plus the delayed-delete reaper running as a
background task for the lifetime of the process. Also the share-browsing interface:
/static/ assets and the share router. The addition consumer
is a second lifespan-scoped background task, started
and cancelled exactly the way the reaper task already is -- see
orchestrator.media.queue.run_add_consumer. The assembly consumer is a
third, in the same shape and entirely independent of the addition
consumer's own loop -- see
orchestrator.media.playlist_queue.run_playlist_consumer.

Two pieces of middleware run here, in this order:
`auth.auth_gate`, registered last and therefore outermost, closes the
interface by default; `reject_cross_origin_writes`, registered first and
therefore innermost, refuses a same-site-cookie-bearing mutating request
whose `Origin` names a different site. Both carry their own docstrings
with the threat model and the reasons for what they do not attempt.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from orchestrator import auth
from orchestrator.config import get_settings
from orchestrator.db import create_pool, queries
from orchestrator.media.playlist_queue import run_playlist_consumer
from orchestrator.media.queue import run_add_consumer
from orchestrator.reaper import run_reaper
from orchestrator.resolve import router as resolve_router
from orchestrator.web.routes_auth import router as auth_router
from orchestrator.web.routes_episodes import router as episodes_router
from orchestrator.web.routes_folder import router as folder_router
from orchestrator.web.routes_playlists import router as playlists_router
from orchestrator.web.routes_preview import router as preview_router
from orchestrator.web.routes_share import router as share_router
from orchestrator.web.routes_titles import router as titles_router
from orchestrator.web.routes_worker import router as worker_router

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)

# How often each consumer polls the durable queue when it finds nothing to
# claim. The trade this makes: shorter means an addition or an
# assembly starts sooner after it is confirmed; longer means fewer idle
# queries against a queue that is empty most of the time. Weighted toward
# the operator noticing a prompt start -- this service serves 7-15 people
# who are already in the room waiting, not a queue with any real
# throughput to protect, and an idle SELECT every half second costs this
# database nothing at this scale.
CONSUMER_POLL_INTERVAL_SECONDS = 0.5


def _clear_stale_scratch_files(work_dir: Path) -> None:
    """A process that was killed never ran its own scratch cleanup -- this
    is the only place that promise can be kept. /work is a plain
    container-layer directory (never a named volume), so on a genuine
    recreate it is already empty; this also covers a plain restart, which
    does not clear it on its own.

    Recovery for an interrupted *job* no longer needs a startup pass at
    all: the row is durable now, its heartbeat simply goes stale, and the
    reaper's own continuously-running sweep (`orchestrator.reaper
    .sweep_stale_jobs`) requeues or fails it without waiting for a
    restart to notice. A startup sweep that also marked rows failed would
    race that sweep's own requeue for the same row -- the same
    interruption producing two outcomes for one job. This function is
    what remains of the old startup sweep once that race is removed along
    with the sweep itself: leftover scratch bytes on local disk, which
    nothing else in this project clears.
    """
    if work_dir.is_dir():
        for leftover in work_dir.iterdir():
            if leftover.is_file():
                leftover.unlink()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await create_pool()

    async with app.state.db_pool.acquire() as conn:
        app.state.operator_exists = await queries.operator_exists(conn)
    app.state.operator_exists_checked_at = time.monotonic()

    # The setup token authorises exactly one act -- creating the first
    # operator credential -- and is minted fresh on every start that finds
    # an empty table, so a restart invalidates whatever link was printed
    # before it. It lives only in this process, with no table and no
    # file, because a secret that outlives the one act it authorises is a
    # secret somebody has to remember to delete. It is cleared the instant
    # the credential is created (orchestrator/web/routes_auth.py) or the
    # instant this process notices one was created elsewhere
    # (auth.operator_exists_cached).
    #
    # It still travels three places this repository does not fully close
    # on its own: the query string lands in the edge's access log -- a
    # volume this container mounts -- unless that log is configured not to
    # record query strings, which the edge configuration does; in the
    # operator's own reverse-proxy log, if one sits in front of this
    # stack, which is outside this repository; and in the browser's own
    # history. The token's one-time nature is what bounds the other two:
    # whichever log or history entry survives, the link it recorded stops
    # working the moment the account exists.
    app.state.setup_token = None
    if not app.state.operator_exists:
        app.state.setup_token = secrets.token_urlsafe(32)
        logger.warning(
            "No operator account exists yet. Open this address once to "
            "create one: https://%s/setup?token=%s",
            get_settings().admin_fqdn,
            app.state.setup_token,
        )

    _clear_stale_scratch_files(Path(get_settings().work_dir))

    add_worker_task = asyncio.create_task(
        run_add_consumer(
            app.state.db_pool, poll_interval=CONSUMER_POLL_INTERVAL_SECONDS
        )
    )
    playlist_worker_task = asyncio.create_task(
        run_playlist_consumer(
            app.state.db_pool, poll_interval=CONSUMER_POLL_INTERVAL_SECONDS
        )
    )
    reaper_task = asyncio.create_task(run_reaper(app.state.db_pool))
    try:
        yield
    finally:
        add_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await add_worker_task
        playlist_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await playlist_worker_task
        reaper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper_task
        await app.state.db_pool.close()


app = FastAPI(title="mytube-orchestrator", lifespan=lifespan)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def reject_cross_origin_writes(request, call_next):
    """Refuse a state-changing request whose `Origin` is some other site.

    The argument for keeping this middleware no longer rests on "one
    operator, no accounts, and no login to fixate" -- this phase makes all
    three false. It stays anyway, as a second layer: the session cookie is
    `SameSite=Lax`, which browsers do not attach to a cross-site POST, so
    cross-site request forgery is already closed by the session itself,
    and per-form tokens threaded through nine templates would buy nothing
    extra for their price. `Strict` was rejected because it breaks
    arriving at the page the operator was originally going to after
    signing in. This middleware's deliberate allowance of a mutating
    request carrying no `Origin` at all stops being a hole for a different
    reason than it used to be: the host port is gone and the
    application is closed by default, so such a request still needs
    a valid session cookie to reach anything.

    **What it compares.** Not `Host` any more -- `auth.request_host`
    instead, the same forwarded-host accessor `auth.auth_gate` uses, so
    exactly one place in the application knows how a hostname reaches it.
    Comparing against the literal `Host` header was inert
    only by accident: it worked while the operator reached this process on
    a published host port, where the browser's `Origin` and the `Host` it
    sent agreed. That port is gone: the interface is now routed
    through `proxy_pass` to the `orchestrator` upstream block -- and nginx
    derives the outgoing `Host` it sends upstream from the *upstream
    block's own name*, not from the client's request. `Host` therefore
    becomes the literal string `orchestrator` while a browser sends
    `Origin: https://<the admin hostname>` on every same-site POST, and
    the old comparison would refuse every one of them: the login itself
    first, then every add, rename and delete -- `auth_gate` lets `/login`
    through as public and this inner middleware would refuse it anyway.
    Both sides of the comparison are normalised through
    `auth.normalize_host`, which also fixes the case of an external proxy
    on a non-default port, where `Origin` carries a port and a forwarded
    host may not.

    The alternative -- setting a real `Host` header on the admin `server`
    block in nginx -- is also one line and would also work. It is not the
    one chosen: it would put a security control's correctness into a
    different file, in a different container, where nothing in this test
    suite can reach it. The in-process test that proves this behaviour
    runs through `ASGITransport` with no nginx anywhere, so with the nginx
    fix that test would pass trivially and would not catch a regression
    the next time this middleware is touched. Fixing it in the application
    is what makes the test mean something.

    Two things it does not do, both on purpose:

    * A request with no `Origin` at all is allowed. That is curl, the CLI,
      and non-browser clients, and every such request still has to carry
      a valid session cookie to reach anything now that the interface is
      closed by default.
    * Only the host is compared, not the scheme. The threat is a
      *different site*, and an attacker cannot make their page's host
      match this one. Comparing schemes as well would turn a future
      https-terminating proxy in front of this service into a blanket 403
      on every action, which is a worse failure than the one it prevents.
    """
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin:
            origin_host = auth.normalize_host(urlsplit(origin).netloc)
            if origin_host != auth.request_host(request):
                logger.warning(
                    "refused a %s to %s from a foreign origin (%s)",
                    request.method,
                    request.url.path,
                    origin,
                )
                return PlainTextResponse(
                    "This request came from another site and was refused.",
                    status_code=403,
                )
    return await call_next(request)


# auth_gate must be registered last so it is outermost: Starlette inserts
# each newly added @app.middleware("http") at the front of the stack, so
# the last one registered runs first and last. See auth.auth_gate's
# own docstring for the full ordering rationale.
app.middleware("http")(auth.auth_gate)

app.include_router(resolve_router)
app.include_router(share_router)
# The folder confirmation screen -- see routes_folder's own
# module docstring for what it does and does not do yet.
app.include_router(folder_router)
app.include_router(titles_router)
# Per-episode actions and per-episode/edition check pages
# (orchestrator/web/routes_episodes.py) -- routes scoped one level below
# routes_titles's own title-wide ones.
app.include_router(episodes_router)
app.include_router(preview_router)
app.include_router(auth_router)
app.include_router(playlists_router)
# The converter's own router -- same application, same hostname, no
# second host and no second port. Reachable through the existing edge
# proxy exactly like every other route here; see routes_worker's own
# module docstring for the authorization rule.
app.include_router(worker_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz():
    """Docker's healthcheck calls this on localhost inside the container
    -- a failure here has to be a deliberate "unhealthy" answer,
    never an unhandled exception. A crash mid-request is not a status
    Docker's healthcheck (or a test's ASGI transport) can read; a caught
    failure returning 503 is.
    """
    try:
        async with app.state.db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        logger.exception("healthcheck: database check failed")
        return Response(status_code=503)
    return {"status": "ok"}
