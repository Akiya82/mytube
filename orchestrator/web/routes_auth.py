"""Operator auth: GET/POST /login, POST /logout, GET/POST /setup.

`/setup` is the first-run counterpart to `/login`: reachable only via the
one-time token `orchestrator.main`'s lifespan prints to the container log
when the credential table is empty, it either renders the create-password
form or one of two fixed banners -- "no account yet, here is where the
link is" or "this link is no longer valid" -- keyed on nothing but
whether an operator already exists (see setup_form_route's own
docstring).
"""
import asyncio
import logging
import secrets
import uuid
from urllib.parse import quote

import asyncpg
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from orchestrator import auth
from orchestrator.db import queries
from orchestrator.web.templates import render

router = APIRouter()
logger = logging.getLogger(__name__)


def _notice_key(request: Request) -> str | None:
    """Which of the three ways a session can end this visit is announcing,
    as far as the query string says. Both flags are booleans read off the
    query string and neither is interpolated into the page; the banner
    text is fixed catalogue copy, so an attacker-supplied query string can
    at worst show the operator a true sentence.

    The third case -- a password reset revoking every device -- is
    deliberately not distinguished here: it is byte-identical to a session
    that expired and was swept (both leave a cookie whose row is gone),
    and telling them apart would need a tombstone per revoked session kept
    for as long as a cookie might outlive it, for a signal one operator
    meets perhaps once. That fact lands in the reset command's own output
    instead, to the person who just caused it.
    """
    if "signed_out" in request.query_params:
        return "login.notice.signed_out"
    if "expired" in request.query_params:
        return "login.notice.session_ended"
    return None


# Same shape as `_notice_key` above, and the same argument: `error` is a
# flag read off the query string, mapped through a fixed allow-list, and
# what the banner renders is catalogue copy chosen by that map -- never the
# submitted value. A hand-crafted link can at worst show the operator one of
# the four sentences the screen already shows on its own. Two maps, not one
# shared map, so `/login?error=password_mismatch` cannot borrow a
# create-password sentence for the sign-in screen.
#
# Be precise about what that does *not* claim, because the obvious reading is
# wrong and someone will act on it. The submitted value does reach the page:
# `templates.render` rebuilds `current_url` out of the request's own query
# string verbatim, and `base.html` puts it inside both language-switcher
# hrefs. `/login?error=<anything>` therefore returns a body containing that
# text. What makes it inert is the `urlencode` filter overridden in
# `templates.py` plus Jinja's autoescaping -- not this allow-map, which only
# governs which sentence the banner shows. Weaken either of those and this
# comment stops being true, so they are load-bearing security machinery
# rather than the formatting nicety their own docstring makes them sound.
#
# The typed username deliberately does not travel with this flag. It was
# weighed against carrying it and rejected: echoing operator-submitted
# text into a query string puts it in browser history and in the edge's
# access log, retyping a name costs the one operator using this
# deployment a moment, and the field coming back empty on the far side of
# a language switch is the intended behaviour, not an oversight.
_LOGIN_ERROR_KEYS = {"invalid_credentials": "login.error.invalid_credentials"}
_SETUP_ERROR_KEYS = {
    "username_required": "setup.error.username_required",
    "password_mismatch": "setup.error.password_mismatch",
    "password_too_short": "setup.error.password_too_short",
}


def _error_key(request: Request, allowed: dict[str, str]) -> str | None:
    return allowed.get(request.query_params.get("error", ""))


def _error_flag(flag: str, allowed: dict[str, str]) -> tuple[str, str]:
    """The query fragment and the catalogue key for `flag`, from one place.

    A failing branch has to state the same fact twice -- the flag it writes
    into the switcher's address, and the key it hands the template to render
    right now -- and the two are only correct while they agree. Written as
    separate literals they drift silently and in the one direction nobody
    tests: the failing submission still shows its banner, and only the page
    that comes back from a language switch renders bare, which is the exact
    defect this flag was added to close.

    Taking the key out of the same map the reader looks in leaves the flag as
    the only thing a branch spells out, and turns a mistyped one into a
    KeyError here rather than a missing banner somewhere else.
    """
    return f"&error={flag}", allowed[flag]


@router.get("/login")
async def login_form_route(request: Request):
    operator_id, _ = await auth.session_operator_id(request)
    if operator_id is not None:
        # Never shown to somebody already signed in.
        return RedirectResponse("/library", status_code=302)

    # First-run branch. On a fresh stack there is no credential to check a
    # password against, so this form would be a deadlock -- the most
    # likely address a person types from memory or autocomplete. The
    # check belongs here rather than in the gate: moving the gate's own
    # first-run branch above its public-path check would put it in front
    # of the static assets and the language route too, and one of the two
    # orderings there is load-bearing for delivery (see auth.auth_gate).
    #
    # Gated on the *absence* of a `next` value, not on first-run alone.
    # `next` only ever arrives here two ways: the gate's own redirect
    # (`/login?next=...`), which never fires on first run because the gate
    # redirects a non-public path straight to `/setup` before it would
    # ever build a `/login` redirect -- or a deliberate direct visit
    # carrying one. Either way, a `next` present means this exact URL is
    # already the operator's destination in hand (an existing screen's
    # own "keep the query string" behaviour, exercised with no operator
    # present by this suite's own no_operator-fixtured tests), and this
    # branch leaves that alone rather than redirecting it away.
    if request.query_params.get("next") is None and not await auth.operator_exists_cached(
        request
    ):
        return RedirectResponse("/setup", status_code=302)

    next_url = auth.safe_next(request.query_params.get("next"))
    return render(
        request,
        "login.html",
        next_url=next_url,
        minimal_header=True,
        notice_key=_notice_key(request),
        error_key=_error_key(request, _LOGIN_ERROR_KEYS),
    )


async def _reject_login(request: Request, username: str, next_value: str):
    """The one failure branch both a wrong credential and a `create_session`
    race land on: identical message, identical delay, whichever
    reason produced it. Logs the submitted username through `%r` (never
    bare -- `repr` escapes newlines, and an unescaped value in a log line
    is how a caller writes their own log entries), then sleeps the
    constant delay before re-rendering, so the timing tells a stranger
    nothing about which half of the credential they guessed right.
    """
    logger.warning("failed login attempt for username %r", username)
    await asyncio.sleep(auth.FAILED_ATTEMPT_DELAY_SECONDS)

    next_url = auth.safe_next(next_value)
    # Override for the language switcher (see templates.render's own
    # docstring on why this goes before **context, not after): a POST's
    # state lives in the body, not in the query string, so the computed
    # current_url would otherwise be bare /login and switching language
    # after a mistake would throw away the destination the operator was
    # heading to.
    error_query, error_key = _error_flag("invalid_credentials", _LOGIN_ERROR_KEYS)
    current_url = f"/login?next={quote(next_url, safe='')}{error_query}"

    return render(
        request,
        "login.html",
        status_code=200,
        next_url=next_url,
        minimal_header=True,
        error_key=error_key,
        username_value=username,
        current_url=current_url,
    )


@router.post("/login")
async def login_submit_route(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_value: str = Form("", alias="next"),
):
    # A stale duplicate tab resubmitting a login form while another tab's
    # sign-in already went through: honour the session already in hand
    # rather than re-verifying a password against it (the same check
    # GET /login opens with, for the same reason).
    operator_id, _ = await auth.session_operator_id(request)
    if operator_id is not None:
        return RedirectResponse(auth.safe_next(next_value), status_code=302)

    # Three separate steps on purpose: read the credential, release the
    # connection, verify, take a connection again to insert the session.
    # The verify in the middle deliberately costs the better part of a
    # second, and the pool it would otherwise hold is the same pool
    # `GET /v/{slug}` draws from, so holding a connection across it turns a
    # burst of unauthenticated login attempts into a resource attack on
    # delivery -- the exact threat the edge's rate-limit zone was written
    # for, arriving by a different road. `setup_submit_route` already
    # hashes before it acquires, for the same reason.
    #
    # Splitting the connection costs nothing in correctness: the
    # read-then-insert race is closed inside `create_session`'s single
    # statement by `FOR SHARE OF c`, not by holding one connection across
    # both halves.
    pool = request.app.state.db_pool
    async with pool.acquire(timeout=auth.ACQUIRE_TIMEOUT_SECONDS) as conn:
        row = await queries.credential_by_username(conn, username)

    if row is None:
        # Nothing to verify against -- run the same-cost dummy verify
        # so "no such user" is not measurably faster than "wrong
        # password".
        await auth.verify_absent_credential()
        ok = False
    else:
        ok = await auth.verify_password(row["password_hash"], password)

    if not ok:
        return await _reject_login(request, username, next_value)

    raw, token_hash = auth.new_session_token()
    async with pool.acquire(timeout=auth.ACQUIRE_TIMEOUT_SECONDS) as conn:
        created = await queries.create_session(
            conn, token_hash, row["id"], auth.SESSION_LIFETIME_SECONDS, row["password_hash"]
        )
    if not created:
        # The credential changed between the read above and this
        # insert -- a password reset landing during the second or so
        # verification takes. From the operator's side it is not a
        # special case: the password they typed is no longer the
        # password.
        return await _reject_login(request, username, next_value)

    logger.info("operator %r signed in", username)
    response = RedirectResponse(auth.safe_next(next_value), status_code=302)
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=raw,
        max_age=auth.SESSION_LIFETIME_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        # No domain= here, deliberately -- a host-only cookie is strictly
        # narrower than a domain-scoped one, and this interface has
        # exactly one hostname. Do not add it back.
    )
    return response


@router.post("/logout")
async def logout_route(request: Request):
    """Ends this device's session and no other -- the CLI reset is
    the answer to a lost device, and a sign-out-everywhere button would be
    a new screen element for a situation a single operator meets once.
    Not in the public path list, so it already requires a session to
    reach.
    """
    raw = request.cookies.get(auth.SESSION_COOKIE_NAME)

    # The response is built before the delete is attempted and returned
    # whatever the database does. An unreachable database used to raise
    # straight out of here into a 500, which left the browser holding the
    # cookie the operator had just asked to be rid of -- sign-out failing
    # open, in the one direction it must never fail. Clearing the cookie
    # regardless is strictly better: the row may outlive the click, but it
    # carries an expiry and the sweep collects it, whereas a cookie left in
    # a browser nobody is watching does not.
    response = RedirectResponse("/login?signed_out=1", status_code=302)
    response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")

    if raw is not None:
        pool = request.app.state.db_pool
        try:
            async with pool.acquire(timeout=auth.ACQUIRE_TIMEOUT_SECONDS) as conn:
                await queries.delete_session(conn, auth.hash_session_token(raw))
        except (asyncpg.PostgresError, OSError, asyncio.TimeoutError):
            # Loud, because the session really does stay live server-side
            # until it expires, and that is worth an operator knowing.
            logger.exception("sign-out could not delete the session row")
            return response

    logger.info("operator signed out")
    return response


def _setup_token_valid(request: Request, token: str) -> bool:
    """Constant-time, crash-proof comparison of a submitted token against
    `app.state.setup_token`.

    `token` is compared as **bytes**, never as `str`:
    `secrets.compare_digest` on two `str` requires both to be ASCII-only
    and raises `TypeError` otherwise, so a submission carrying a
    non-ASCII character (`/setup?token=ü`) would be a `500` on the first
    page of every fresh deploy, reachable by anyone who can type. Encoding
    first turns every input into a comparison instead of an exception and
    loses nothing: the generated token is ASCII, so a non-ASCII submission
    is simply wrong.

    `None` for the stored token (no lifespan has run, or a transport that
    never ran one) makes every submission invalid -- closed is the right
    answer, not an exception.
    """
    stored = getattr(request.app.state, "setup_token", None)
    if stored is None:
        return False
    return secrets.compare_digest(token.encode("utf-8"), stored.encode("utf-8"))


@router.get("/setup")
async def setup_form_route(request: Request):
    """Two states, keyed on one thing: whether an operator exists. Not
    three, and not keyed on the token -- that is what makes the security
    contract hold and the first run survivable at the same time.

    **An operator exists.** The spent-link banner renders for every
    request, whatever token was submitted and whether or not one was. A
    wrong token and an arrival after the account already exists are
    indistinguishable on purpose -- checked first, before the
    token is even read, so the two cases share not just their output but
    their code path.

    **No operator yet.** Either the token opens the form, or the page
    tells the operator where the current link is (the first-arrival
    state) -- this is what a bare `/setup` used to answer with the
    spent-link banner before this route existed, telling a brand-new
    operator on their very first minute with the product that their link
    was already spent, with no other way in.
    """
    # current_url pinned to bare /setup for both banner states, not the
    # request's own query string: neither state's copy depends on the
    # token, and test_the_setup_page_looks_the_same_once_an_account_exists
    # pins that a wrong token and no token at all render byte-identical
    # bodies -- which the language switcher's next= link would otherwise
    # break, since templates.render()'s own default current_url embeds
    # whatever query string this exact request carried.
    if await auth.operator_exists_cached(request):
        return render(
            request, "setup.html", minimal_header=True, setup_state="spent",
            current_url="/setup",
        )

    token = request.query_params.get("token") or ""
    if not _setup_token_valid(request, token):
        return render(
            request, "setup.html", minimal_header=True, setup_state="needed",
            current_url="/setup",
        )

    return render(
        request,
        "setup.html",
        minimal_header=True,
        setup_state="form",
        token=token,
        min_password_length=auth.MIN_PASSWORD_LENGTH,
        error_key=_error_key(request, _SETUP_ERROR_KEYS),
    )


@router.post("/setup")
async def setup_submit_route(
    request: Request,
    token: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
):
    """Re-validates exactly as the GET does, in the same order: operator
    exists first, then the token. Any state other than "no operator and a
    valid token" applies the constant failed-attempt delay, logs a
    warning, and renders the matching banner with status 200 -- the
    spent-link and first-arrival bodies are each identical between GET and
    POST, in status and in timing, so a stranger probing the address
    learns nothing they could not learn by loading the page.
    """
    if await auth.operator_exists_cached(request):
        await asyncio.sleep(auth.FAILED_ATTEMPT_DELAY_SECONDS)
        logger.warning("setup submission refused: an operator already exists")
        return render(
            request, "setup.html", status_code=200, minimal_header=True,
            setup_state="spent", current_url="/setup",
        )

    if not _setup_token_valid(request, token):
        await asyncio.sleep(auth.FAILED_ATTEMPT_DELAY_SECONDS)
        logger.warning("setup submission refused: invalid or missing token")
        return render(
            request, "setup.html", status_code=200, minimal_header=True,
            setup_state="needed", current_url="/setup",
        )

    # A POST's state lives in the body, not the query string -- see
    # templates.render()'s own docstring on why the language switcher
    # needs an explicit current_url here: the computed one would be bare
    # /setup, and switching language after a mismatch or a too-short
    # password would destroy the one-time link and land the operator on
    # the spent-link banner instead of their own form again.
    current_url = f"/setup?token={quote(token, safe='')}"

    # Checked before the password pair, because it is the first field on
    # the form and because the schema admits exactly one operator: a blank
    # name is not a row somebody corrects later, it is the only credential
    # this deployment will ever have, with no name to sign in under and no
    # second account to fix it from. Stored stripped, so the name typed at
    # the login form is the name that was saved.
    username = username.strip()
    if not username:
        error_query, error_key = _error_flag("username_required", _SETUP_ERROR_KEYS)
        return render(
            request,
            "setup.html",
            status_code=200,
            minimal_header=True,
            setup_state="form",
            token=token,
            username_value=username,
            min_password_length=auth.MIN_PASSWORD_LENGTH,
            error_key=error_key,
            current_url=f"{current_url}{error_query}",
        )

    if password != password_confirm:
        error_query, error_key = _error_flag("password_mismatch", _SETUP_ERROR_KEYS)
        return render(
            request,
            "setup.html",
            status_code=200,
            minimal_header=True,
            setup_state="form",
            token=token,
            username_value=username,
            min_password_length=auth.MIN_PASSWORD_LENGTH,
            error_key=error_key,
            current_url=f"{current_url}{error_query}",
        )

    if len(password) < auth.MIN_PASSWORD_LENGTH:
        error_query, error_key = _error_flag("password_too_short", _SETUP_ERROR_KEYS)
        return render(
            request,
            "setup.html",
            status_code=200,
            minimal_header=True,
            setup_state="form",
            token=token,
            username_value=username,
            min_password_length=auth.MIN_PASSWORD_LENGTH,
            error_key=error_key,
            current_url=f"{current_url}{error_query}",
        )

    operator_id = uuid.uuid4()
    password_hash = await auth.hash_password(password)
    raw, token_hash = auth.new_session_token()

    pool = request.app.state.db_pool
    try:
        async with pool.acquire(timeout=auth.ACQUIRE_TIMEOUT_SECONDS) as conn:
            await queries.create_operator(conn, operator_id, username, password_hash)
            await queries.create_session(
                conn, token_hash, operator_id, auth.SESSION_LIFETIME_SECONDS, password_hash
            )
    except asyncpg.exceptions.UniqueViolationError:
        # Two submissions at once, or a credential created from the
        # command line between this request's check above and this
        # insert. The database now admits exactly one
        # operator by constraint -- this is what a genuine race looks
        # like, and the constraint is the truth the fast check above only
        # approximates.
        request.app.state.operator_exists = True
        request.app.state.setup_token = None
        logger.warning("setup submission lost a race: an operator was created concurrently")
        return render(
            request, "setup.html", status_code=200, minimal_header=True,
            setup_state="spent", current_url="/setup",
        )

    # Consumed the instant it works, in process, with no extra query on
    # the hot path ever again -- an operator does exist now, whichever
    # process's request this was.
    request.app.state.operator_exists = True
    request.app.state.setup_token = None
    logger.info("operator account %r created", username)

    response = RedirectResponse("/library", status_code=302)
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=raw,
        max_age=auth.SESSION_LIFETIME_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response
