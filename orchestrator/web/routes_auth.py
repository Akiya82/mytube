"""Operator auth: GET/POST /login, POST /logout.

The create-password route (`GET`/`POST /setup`) is plan 05's own module
addition -- nothing here assumes it exists beyond the one first-run check
below, which plan 05's own helper replaces.
"""
import asyncio
import logging
from urllib.parse import quote

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


@router.get("/login")
async def login_form_route(request: Request):
    operator_id, _ = await auth.session_operator_id(request)
    if operator_id is not None:
        # Never shown to somebody already signed in.
        return RedirectResponse("/library", status_code=302)

    # First-run branch -- a placeholder, plan 05's spot. On a fresh stack
    # there is no credential to check a password against, so this form
    # would be a deadlock -- the most likely address a person types from
    # memory or autocomplete. The check belongs here rather than in the
    # gate: moving the gate's own first-run branch above its public-path
    # check would put it in front of the static assets and the language
    # route too, and one of the two orderings there is load-bearing for
    # delivery (see auth.auth_gate). Plan 05 owns the cached
    # `app.state.operator_exists` flag this will read; left a no-op here
    # so plan 05 changes one branch rather than re-reading this route.

    next_url = auth.safe_next(request.query_params.get("next"))
    return render(
        request,
        "login.html",
        next_url=next_url,
        minimal_header=True,
        notice_key=_notice_key(request),
    )


async def _reject_login(request: Request, username: str, next_value: str):
    """The one failure branch both a wrong credential and a `create_session`
    race land on (D-80): identical message, identical delay, whichever
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
    current_url = f"/login?next={quote(next_url, safe='')}"

    return render(
        request,
        "login.html",
        status_code=200,
        next_url=next_url,
        minimal_header=True,
        error_key="login.error.invalid_credentials",
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

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await queries.credential_by_username(conn, username)
        if row is None:
            # Nothing to verify against -- run the same-cost dummy verify
            # so "no such user" is not measurably faster than "wrong
            # password" (T-02.1-21).
            await auth.verify_absent_credential()
            ok = False
        else:
            ok = await auth.verify_password(row["password_hash"], password)

        if not ok:
            return await _reject_login(request, username, next_value)

        raw, token_hash = auth.new_session_token()
        created = await queries.create_session(
            conn, token_hash, row["id"], auth.SESSION_LIFETIME_SECONDS, row["password_hash"]
        )
        if not created:
            # The credential changed between the read above and this
            # insert -- a password reset landing during the second or so
            # verification takes. From the operator's side it is not a
            # special case: the password they typed is no longer the
            # password (T-02.1-80).
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
    """Ends this device's session and no other (D-82) -- the CLI reset is
    the answer to a lost device, and a sign-out-everywhere button would be
    a new screen element for a situation a single operator meets once.
    Not in the public path list, so it already requires a session to
    reach.
    """
    raw = request.cookies.get(auth.SESSION_COOKIE_NAME)
    if raw is not None:
        pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            await queries.delete_session(conn, auth.hash_session_token(raw))
    logger.info("operator signed out")

    response = RedirectResponse("/login?signed_out=1", status_code=302)
    response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
    return response
