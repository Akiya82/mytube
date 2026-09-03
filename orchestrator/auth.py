"""Everything the interface needs to know who is asking: the password
hash, the session token, the public path list and the gate.

`auth_gate` is the whole of D-75/D-76: everything requires a session
except a short, commented list of paths that do not, and that list is the
only thing a future phase has to touch. The hashing calls are deliberately
expensive -- that is the whole point of a password hash -- and therefore
never run on the event loop this process shares with every viewer's
`/v/{slug}` request.
"""
import asyncio
import hashlib
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import argon2
import argon2.exceptions
import asyncpg
from fastapi.responses import PlainTextResponse, RedirectResponse

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.web.templates import resolve_lang, translator

logger = logging.getLogger(__name__)

# The sliding window (D-81): one constant both the cookie's Max-Age and
# the database's expires_at are built from, so the two can never disagree.
SESSION_LIFETIME_SECONDS = 30 * 24 * 3600

# The window is not rewritten until its remaining life has slipped inside
# this much of the full thirty days -- one day of slack. Writing on every
# request would make the sliding window the busiest writer in the system
# for a session this long-lived; see validate_and_renew_session in
# orchestrator/db/queries.py for the throttled query this constant feeds.
SESSION_RENEW_AFTER_SECONDS = 24 * 3600

SESSION_COOKIE_NAME = "mytube_session"

# --- The public path lists (D-75, D-90) -----------------------------------
#
# HOST_EXEMPT_PREFIXES, PUBLIC_PATHS and PUBLIC_PREFIXES are the whole of
# what does not require a session in this application. Everything else
# requires one, including paths that do not exist yet.
#
# Delivery addresses the edge proxies to this process. The edge overwrites
# the forwarded host on every request it sends here (see
# edge/nginx.conf.template), so a viewer reaching one of these through the
# delivery hostname must never be refused by the interface hostname check
# `auth_gate` runs below -- that check is skipped entirely for these
# prefixes. The playlist address `/p/` is in this tuple for the same
# reason `/v/` is: a video player handed a redirect to a login form it
# cannot render is the failure this line prevents, and it protects the
# public playlist address exactly as it already protects the video one.
#
# That note is not the guard. A comment asking a future phase to remember
# one line is the same class of protection this phase already rejected for
# the delivery `server` block, where a pinned digest replaced "please do
# not edit this". `tests/test_edge_config.py::
# test_every_delivery_location_the_edge_proxies_is_exempt_from_the_hostname_check`
# reads the delivery block's proxied `location` prefixes out of the edge
# template and asserts every one of them is in this tuple -- the day a
# proxied location is added to the delivery block without a matching
# entry here, that test fails at the moment the edit is made rather than
# the first time a player is handed HTML.
HOST_EXEMPT_PREFIXES: tuple[str, ...] = ("/v/", "/refresh/", "/p/")

# Addresses that need no session but have no reason to arrive on a foreign
# hostname, so they are checked for one (see auth_gate's ordering below).
# The health endpoint is here because Docker's healthcheck calls it on
# localhost inside the container, which arrives with no forwarded header at
# all and is therefore not refused; it is kept off the internet by the edge
# not proxying it rather than by this list. The setup page is here because
# on a fresh deploy there is no credential to check a password against, so
# redirecting it to the login form would be a deadlock.
PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/setup", "/healthz"})

# The language switcher is here because the login page carries it and it
# only writes a cookie and redirects.
#
# These two tuples are deliberately separate rather than one combined
# list. Running the public-path check before the hostname check (step 3
# before step 2 would be wrong; see auth_gate) is what keeps delivery
# alive, but a single combined list would extend the same bypass to the
# login screen, the create-password screen, the static assets and the
# language route -- so a stranger on a foreign hostname would still be
# served HTML and could still write a language cookie. The foreign-host
# refusal is stated over a request arriving with a foreign hostname, not
# over an authenticated one, and two tuples is what keeps the exemption as
# narrow as the decision.
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/", "/lang/")

# The worker prefix (D-111): opened by its own credential, never by being
# listed as exempt from the session check. This tuple is different in kind
# from the three above -- checked inside `auth_gate` after the hostname
# refusal and before the first-run branch, so a fresh deployment with no
# operator yet still behaves the same as a running one. A path matching it
# is passed straight to its route with **no operator identity set on the
# request** -- nothing here is authorized by passing through it, and a
# handler that expects an operator will find none. The route's own
# dependency (`routes_worker.require_worker_token`) is what actually
# refuses an uncredentialed caller with a 401. This constant grants
# *passage*, never *permission* -- the next person to read four
# similar-looking tuples should add a path here only if that path opens
# itself with its own credential check, never merely to skip the session.
TOKEN_GATED_PREFIXES: tuple[str, ...] = ("/worker/",)

# D-87's constant delay on every failed login attempt. No lockout and no
# attempt table: a lockout would let any passer-by lock the operator out
# of their own interface by hammering the form with their username.
FAILED_ATTEMPT_DELAY_SECONDS = 1.0

# Both `session_operator_id`'s own acquire and the two delivery routes in
# `orchestrator.resolve` are bounded by this -- one constant so the two
# surfaces answer on the same schedule. asyncpg's own pool.acquire() waits
# forever by default; without a bound a saturated pool would leave a
# request queued on an open socket with nothing ever returning (D-79).
ACQUIRE_TIMEOUT_SECONDS = 5.0

# --- First run (D-85) -------------------------------------------------

# The floor the create-password screen enforces. Argon2 does the heavy
# lifting against brute force, so this number exists to refuse the
# trivially guessable rather than to enforce a complexity policy --
# deliberately no strength meter and no live complexity checklist.
MIN_PASSWORD_LENGTH = 8

# How long a *negative* answer ("no operator yet") is trusted before
# operator_exists_cached re-queries. See that function's own docstring for
# why only the negative answer needs bounding at all.
FIRST_RUN_RECHECK_SECONDS = 5.0


async def operator_exists_cached(request) -> bool:
    """Whether the credential table has its one row, cached on
    `request.app.state` so the gate does not run a query on every request
    once an operator exists.

    Reads the flag as `getattr(request.app.state, "operator_exists", True)`
    and the timestamp as
    `getattr(request.app.state, "operator_exists_checked_at", 0.0)`.

    **The defaults are load-bearing, and `True` is deliberate.**
    `orchestrator.main`'s `lifespan` sets both attributes, but the
    in-process test transport this whole suite drives never runs the
    lifespan -- without these defaults every gated unit test written a
    wave earlier would raise an `AttributeError` the moment this function
    is called, and with a `False` default they would all see the
    create-password redirect instead of the login redirect they assert.
    `True` is also the safe direction for a real process that somehow
    answers a request without its lifespan having run: it means "an
    operator exists", which sends the caller to a login form that refuses
    them, whereas `False` would offer a stranger the create-password page.
    A test that wants the first-run branch asks for it explicitly through
    the `no_operator` fixture, which sets the flag false **and** stamps
    the timestamp so a unit test with no real pool never falls into the
    re-query below.

    This is a **negative** cache only. The value only ever moves from
    false to true, once, so caching `True` forever is correct and removes
    a query from the hot path for the life of the process. Caching `False`
    forever would not be: the credential can be created from the
    in-container CLI, in a different process, and this function is the
    only thing that ever notices -- without the re-check the interface
    would go on redirecting to the setup page until someone restarted the
    container. Re-checking on a short interval bounds the cost to one
    query every few seconds no matter how many scanners are knocking
    during first run, which is exactly the case the caching was for.

    When the re-query comes back true, this also clears
    `app.state.setup_token`. The token is minted at process start against
    an empty table and lives for the life of the process; the credential
    can be created from a different process (the CLI), and this is the
    only place that ever notices. Without the clear, an operator created
    from the command line would leave a live setup token behind -- printed
    in the log for anyone who has read the log -- that still opens the
    create-password page for as long as the container keeps running.
    """
    state = request.app.state
    if getattr(state, "operator_exists", True):
        return True

    checked_at = getattr(state, "operator_exists_checked_at", 0.0)
    if time.monotonic() - checked_at < FIRST_RUN_RECHECK_SECONDS:
        return False

    # Stamped before the query, not after -- a failed or slow query must
    # not turn into a re-query on every single request until it succeeds,
    # which is exactly the load the interval exists to bound.
    state.operator_exists_checked_at = time.monotonic()
    try:
        async with state.db_pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS) as conn:
            exists = await queries.operator_exists(conn)
    except (asyncpg.PostgresError, OSError, TimeoutError):
        logger.exception("operator-exists recheck failed, staying in first-run state")
        return False

    state.operator_exists = exists
    if exists:
        state.setup_token = None
    return exists


# Explicit cost parameters rather than the library defaults, which are
# platform-dependent. Measured on the machine this stack actually runs on
# (AMD Ryzen 7 9800X3D, 4 CPUs visible inside the container), 2026-08-24:
# repeated verify calls against a rebuilt image landed a median between
# 316 ms and 349 ms across separate measurement runs, comfortably inside
# the 250-500 ms band a single verify is meant to cost -- expensive enough
# that it is not free to guess, cheap enough that a worker thread is not
# tied up for a meaningful fraction of a second on a process that also
# serves video redirects. Note that `max_workers` below (the hashing
# executor) and `memory_cost` here multiply: raising one means looking at
# the other.
_hasher = argon2.PasswordHasher(time_cost=8, memory_cost=262144, parallelism=4)

# A dedicated executor for the two hashing calls, not `asyncio.to_thread`.
# `to_thread` uses the event loop's single default executor, and in this
# process that executor is shared by every probe, every transcode call,
# every S3 read, the poster reader and the startup sweep. The login route
# is public, unauthenticated by definition, and each verification holds
# its worker for the better part of a second and its configured memory for
# the whole of it. A dozen concurrent attempts on the default executor
# would occupy every worker and hold gigabytes transiently -- in the same
# process that answers /v/{slug} for viewers. Two workers bounds both
# numbers: at most twice `memory_cost` is resident for hashing at any
# moment, and a burst queues on this executor instead of on the one
# delivery shares. The edge's rate limit is the other half of that answer
# and lives in the edge configuration; this half is what survives a
# misconfigured edge.
_HASH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2")


async def hash_password(password: str) -> str:
    """Hash `password` with argon2id, off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HASH_EXECUTOR, _hasher.hash, password)


async def verify_password(stored_hash: str, password: str) -> bool:
    """Verify `password` against `stored_hash`, off the event loop.

    Returns False for a mismatch or a malformed stored hash; never raises
    on either. Any other error (e.g. an out-of-memory hashing failure)
    propagates.
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_HASH_EXECUTOR, _hasher.verify, stored_hash, password)
        return True
    except (argon2.exceptions.VerifyMismatchError, argon2.exceptions.InvalidHashError):
        return False


# A throwaway hash computed once at import, for verify_absent_credential
# below -- never a real operator's hash.
DUMMY_HASH = _hasher.hash("mytube-dummy-hash-for-timing-only")


async def verify_absent_credential() -> None:
    """Run a dummy verify and discard the result.

    When the username is not found there is nothing to verify against,
    and returning immediately would make "no such user" measurably faster
    than "wrong password". Not required by any locked decision; it costs
    four lines and closes a timing distinction that would otherwise tell
    a stranger which half of the credential they guessed right.
    """
    await verify_password(DUMMY_HASH, "irrelevant")


def new_session_token() -> tuple[str, str]:
    """Mint a fresh session token.

    The raw value goes into the cookie and is never stored; the digest
    goes into the row and is never enough to sign in with.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_session_token(raw)


def hash_session_token(raw: str) -> str:
    """The same digest new_session_token uses, so the gate can hash an
    incoming cookie without re-deriving the recipe.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


# --- Hostname and redirect machinery (D-76, D-77, T-02.1-59) ---------------


def normalize_host(value: str | None) -> str | None:
    """The one rule in this application for turning a header value into a
    comparable hostname: text before the first comma (a header can carry a
    comma-separated chain), stripped, with anything from the first colon
    (a port) dropped, lowercased. `None` for an absent or empty value.
    Everything that compares hostnames goes through this.
    """
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    first = first.split(":", 1)[0]
    first = first.lower()
    return first or None


def forwarded_host(request) -> str | None:
    """`normalize_host(X-Forwarded-Host)`.

    Reads the forwarded header, never `Host`: nginx does not pass the
    client's real `Host` through `proxy_pass` by default, and this
    project's edge configuration relies on that -- a check against `Host`
    would compare against the upstream block's own name and silently
    never fire.
    """
    return normalize_host(request.headers.get("x-forwarded-host"))


def request_host(request) -> str | None:
    """`forwarded_host(request)` when that is not `None`, otherwise
    `normalize_host(Host)`.

    This is what `reject_cross_origin_writes` in `orchestrator.main`
    compares an incoming `Origin` against, instead of the bare `Host`
    header it used to compare against -- `proxy_pass` overwrites `Host`
    with the upstream block's own name, which is not the operator's
    browser-visible hostname at all (T-02.1-56).
    """
    host = forwarded_host(request)
    if host is not None:
        return host
    return normalize_host(request.headers.get("host"))


def safe_next(value: str | None, fallback: str = "/library") -> str:
    """Keep `value` only when it is a same-site relative path, otherwise
    return `fallback`.

    A value starting with a single `/` whose second character is neither
    `/` nor `\\` is kept; everything else is replaced. The backslash case
    is the one the language route's own three-line check used to miss:
    browsers resolve a leading slash-backslash (`/\\evil.example`) to a
    foreign authority, and the post-login redirect is the canonical place
    such a value gets planted now that this surface is on the public
    internet (T-02.1-59). Deliberately a character check, not a URL
    parser -- there is nothing here a parser would catch that this does
    not.
    """
    if not value or not value.startswith("/"):
        return fallback
    if len(value) > 1 and value[1] in ("/", "\\"):
        return fallback
    return value


async def session_operator_id(request) -> tuple:
    """The one function both `auth_gate` and the login route call, and the
    one function every screen test's `signed_in` fixture monkeypatches --
    keep its signature stable.

    Reads the cookie named by SESSION_COOKIE_NAME; `(None, False)` if
    absent. Otherwise acquires a connection and calls
    `queries.validate_and_renew_session`, returning both halves of its
    answer straight through.

    Answers closed on a database failure rather than raising. A session
    that cannot be verified is not a session, and saying so is both the
    safe answer and the honest one: the caller sends the operator to the
    login form instead of rendering a stack trace as a bare 500 on a
    public hostname. The timeout is what makes this an answer at all --
    without it this branch would never be reached and the request would
    simply hang, since asyncpg's own acquire waits forever by default.
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None, False

    pool = request.app.state.db_pool
    try:
        async with pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS) as conn:
            return await queries.validate_and_renew_session(
                conn,
                hash_session_token(raw),
                SESSION_LIFETIME_SECONDS,
                SESSION_RENEW_AFTER_SECONDS,
            )
    except (asyncpg.PostgresError, OSError, TimeoutError):
        logger.exception("session lookup failed, answering closed")
        return None, False


UNSAFE_REDIRECT_METHODS = frozenset({"GET", "HEAD"})


async def auth_gate(request, call_next):
    """Deny by default: everything requires a session except
    HOST_EXEMPT_PREFIXES, PUBLIC_PATHS and PUBLIC_PREFIXES (D-75, D-76).

    Registered in `orchestrator.main` as the **outermost** piece of
    middleware -- Starlette inserts each newly added middleware at the
    front of the stack, so the last one registered runs first
    (`test_the_session_gate_runs_outside_the_cross_origin_middleware`).
    Steps run in this order and no other:

    1. A delivery path (`HOST_EXEMPT_PREFIXES`) reaches its route
       immediately, with no hostname check and no session check. The
       delivery `server` block forwards its own hostname on every `/v/`
       and `/refresh/` request it proxies to this application; running the
       hostname check before this step would answer 400 to every viewer
       in VRChat. That is the whole of criterion 4's protection at this
       layer, and `test_a_delivery_request_carrying_the_delivery_hostname_
       is_not_refused` is what keeps saying so.
    2. The hostname check. A request carrying no forwarded host at all is
       allowed through -- the container healthcheck reaches this process
       on localhost and never passes through nginx, so it arrives with no
       forwarded header, mirroring `reject_cross_origin_writes`'s own
       allowance for a request carrying no `Origin`. A request carrying a
       forwarded host that does not match `admin_fqdn` gets a fixed,
       pre-localized plain-text 400 -- rendering no template and
       interpolating nothing the caller supplied, so there is no
       reflection surface and no page URLs built from a hostname just
       refused (T-02.1-22, T-02.1-57).
    3. Public paths and prefixes reach their route with no session, but
       only after step 2 -- they need no session, but none of them has a
       reason to arrive on another hostname, and a bypass wide enough to
       serve the login screen to a stranger's hostname is wider than the
       refusal that was agreed.
    3a. `TOKEN_GATED_PREFIXES` (D-111) reach their route with no session
        either, also after step 2, but on the strength of a credential
        checked by the route itself rather than an exemption granted here
        -- see that constant's own comment for the distinction. Checked
        before step 4 so a converter is refused the same way (401, never a
        redirect) whether or not an operator account exists yet.
    4. First-run branch. On a fresh stack with no operator row, every
       non-public path routes to `/setup` instead of falling through to
       the session check below -- a `/login` a fresh deploy cannot pass
       (there is no credential to check a password against) would
       otherwise be the most likely address a person types from memory or
       autocomplete, and this is what turns that into the create-password
       page instead of a dead end. `operator_exists_cached` is the cache;
       `/setup` itself is already in `PUBLIC_PATHS` and never reaches this
       step.
    5. A valid session: `request.state.operator_id` is set, the route
       runs, and two things are applied to its response. A `Cache-Control:
       no-store`, but only via `setdefault` (a route that has an opinion
       about its own caching keeps it -- `/poster/{edition_id}` answers
       `public, max-age=31536000, immutable` on purpose) and only when the
       content type begins `text/html` (T-02.1-58): the sign-out route
       deletes the session row, which closes the server half of that
       promise, but both major browsers restore a page from their
       back-forward cache without making a request at all, so the library
       would still reappear on the back button after signing out -- this
       is the one place in the application that knows whether a request
       needed a session, so it is the one place that can mark the answer
       as not storable. And the cookie, re-issued with a fresh `max_age`
       when and only when `session_operator_id` renewed the row: the
       cookie carries its own `Max-Age`, and the browser drops it that far
       after it was last issued however heavily the session is used --
       without this the sliding window slides only in the database and
       the operator is signed out thirty days after logging in
       regardless.
    6. Otherwise: redirect to the login form, built from the request's
       method rather than from its path alone (T-02.1-81). A `GET`/`HEAD`
       carries `?next=`; any other method redirects to bare `/login`,
       because a browser follows a 302 with a GET, so a `next` captured
       from a `POST` would send the operator, after a correct sign-in, to
       a `GET` of a route that only accepts `POST` and gets a 405 on a
       form they filled in correctly -- `/logout` on a stale tab is the
       likeliest first encounter. A request that carried a session cookie
       which did not validate gets `expired=1` added to the redirect: the
       difference between "you are not signed in" and "you were, and it
       ended" is the only one of the three session-ending states the
       server can honestly tell apart (see routes_auth.py).
    """
    path = request.url.path

    if path.startswith(HOST_EXEMPT_PREFIXES):
        return await call_next(request)

    host = forwarded_host(request)
    if host is not None and host != get_settings().admin_fqdn:
        logger.warning("refused a request to %r from a foreign host (%r)", path, host)
        lang = resolve_lang(request)
        return PlainTextResponse(
            translator(lang)("auth.foreign_host"), status_code=400
        )

    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    if path.startswith(TOKEN_GATED_PREFIXES):
        return await call_next(request)

    if not await operator_exists_cached(request):
        return RedirectResponse("/setup", status_code=302)

    had_cookie = SESSION_COOKIE_NAME in request.cookies
    operator_id, renewed = await session_operator_id(request)

    if operator_id is not None:
        request.state.operator_id = operator_id
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-store")
        if renewed:
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=request.cookies[SESSION_COOKIE_NAME],
                max_age=SESSION_LIFETIME_SECONDS,
                httponly=True,
                secure=True,
                samesite="lax",
                path="/",
            )
        return response

    if request.method in UNSAFE_REDIRECT_METHODS:
        location = f"/login?next={quote(path, safe='')}"
    else:
        location = "/login"
    if had_cookie:
        location += ("&" if "?" in location else "?") + "expired=1"
    return RedirectResponse(location, status_code=302)
