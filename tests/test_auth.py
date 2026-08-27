"""Behaviour spec for operator authentication -- the gate, the cookie, the
foreign-host refusal, sign-out, the sliding session window and first run.

Unit half drives `orchestrator.main.app` through `httpx.ASGITransport`
against a mocked pool (`base_url="http://testserver"`), exactly like
`test_resolve.py`. Integration half runs against the testcontainers
Postgres this whole session shares, on `base_url="https://testserver"` --
load-bearing, not a style choice: the session cookie is `Secure`, and
Python's cookie jar silently refuses to attach a `Secure` cookie to a
plaintext request, so an `http://` client would turn every "the stale
cookie no longer works" assertion into a tautology.

RED state: `orchestrator.auth` does not exist yet. This whole module fails
to import until a later plan creates it -- that failure is this plan's
deliverable, not a bug in this file. Written against the module's assumed
future shape so the module that finally exists has something concrete to
satisfy:

- `HOST_EXEMPT_PREFIXES: tuple[str, ...]` -- delivery addresses the edge
  proxies, skipped by the hostname check entirely.
- `PUBLIC_PATHS` / `PUBLIC_PREFIXES` -- addresses that need no session.
- `SESSION_COOKIE_NAME = "mytube_session"`.
- `async def session_operator_id(request) -> tuple[uuid.UUID | None, bool]`
  -- the one function both the gate and this suite's stubbed-session
  fixture call; monkeypatched here exactly the way a later plan's own
  `conftest.py` fixture does it for every other screen's tests.
- `safe_next(value, fallback="/library") -> str`.
"""
import re
import time
import urllib.parse
import uuid

import httpx
import pytest

from orchestrator import auth  # RED: orchestrator/auth.py does not exist yet.
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.main import app

from conftest import ADMIN_FQDN

# A same-site browser POST carries this pair; the edge's admin block sets
# X-Forwarded-Host on every request it proxies, and a browser attaches
# Origin to every same-site form submission.
ADMIN_ORIGIN = f"https://{ADMIN_FQDN}"

# The public delivery hostname (matches conftest's `settings` fixture's
# MYTUBE_PUBLIC_BASE_URL="https://mytube.example").
DELIVERY_FQDN = "mytube.example"

STUB_OPERATOR_ID = uuid.uuid4()
SESSION_COOKIE = {"mytube_session": "test-session-token"}


class _DummyConn:
    pass


class _DummyAcquireCtx:
    async def __aenter__(self):
        return _DummyConn()

    async def __aexit__(self, *exc_info):
        return False


class _DummyPool:
    def acquire(self, *args, **kwargs):
        return _DummyAcquireCtx()

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _app_env(settings):
    """Pulls in conftest's `settings` fixture for its side effect (env vars
    get_settings() needs). get_settings is @lru_cache'd, so clear it before
    and after -- otherwise whichever test in the whole session calls it
    first pins its Settings() for every later test/module.
    """
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def client():
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
def signed_in_client(monkeypatch):
    """A client carrying a session cookie whose validity is stubbed rather
    than real -- these are the gate's own tests, not a screen's, and a test
    that had to perform a real login before checking a header would be
    testing the login twice and the gate once.
    """

    async def _fake_session_operator_id(request):
        return STUB_OPERATOR_ID, False

    monkeypatch.setattr(auth, "session_operator_id", _fake_session_operator_id)
    app.state.db_pool = _DummyPool()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=SESSION_COOKIE
    )


def _active_row():
    return {
        "title_id": uuid.uuid4(),
        "status": "active",
        "default_edition_id": uuid.uuid4(),
    }


# --- Unit half ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/login",
        "/setup",
        "/static/style.css",
        "/lang/en",
        f"/v/{uuid.uuid4()}",
        f"/refresh/{uuid.uuid4()}",
    ],
)
async def test_public_paths_reach_their_route_without_a_session(
    monkeypatch, client, operator_present, path
):
    monkeypatch.setattr(queries, "resolve_slug", lambda conn, slug: _future(_active_row()))
    monkeypatch.setattr(
        queries, "resolve_edition", lambda conn, edition_id: _future(_active_row())
    )

    async with client:
        resp = await client.get(path, follow_redirects=False)

    if resp.status_code == 302:
        assert not resp.headers["location"].startswith("/login")


def _future(value):
    async def _coro(*args, **kwargs):
        return value

    return _coro()


async def test_an_unknown_path_with_no_route_still_redirects_to_the_login_form(
    client, operator_present
):
    async with client:
        resp = await client.get(
            "/this-route-does-not-exist-yet", follow_redirects=False
        )

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


async def test_an_admin_path_without_a_session_redirects_to_the_login_form_carrying_next(
    client, operator_present
):
    async with client:
        resp = await client.get("/library", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?next=%2Flibrary"


@pytest.mark.parametrize(
    "path", ["/library", "/login", "/setup", "/static/style.css", "/lang/en"]
)
async def test_a_foreign_forwarded_host_is_refused_in_plain_text(
    client, operator_present, path
):
    async with client:
        resp = await client.get(
            path, headers={"x-forwarded-host": "evil.example"}, follow_redirects=False
        )

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/plain")
    assert "evil.example" not in resp.text
    if path == "/lang/en":
        assert "set-cookie" not in resp.headers


async def test_a_request_with_no_forwarded_host_is_not_refused(client, operator_present):
    async with client:
        resp = await client.get("/healthz", follow_redirects=False)

    assert resp.status_code != 400


@pytest.mark.parametrize(
    "forwarded_host",
    [ADMIN_FQDN, f"{ADMIN_FQDN}:8080", ADMIN_FQDN.upper()],
)
async def test_a_matching_forwarded_host_is_accepted(
    client, operator_present, forwarded_host
):
    async with client:
        resp = await client.get(
            "/healthz",
            headers={"x-forwarded-host": forwarded_host},
            follow_redirects=False,
        )

    assert resp.status_code != 400


@pytest.mark.parametrize("path", ["/v/some-slug", f"/refresh/{uuid.uuid4()}"])
async def test_a_delivery_request_carrying_the_delivery_hostname_is_not_refused(
    monkeypatch, client, path
):
    monkeypatch.setattr(queries, "resolve_slug", lambda conn, slug: _future(_active_row()))
    monkeypatch.setattr(
        queries, "resolve_edition", lambda conn, edition_id: _future(_active_row())
    )

    async with client:
        resp = await client.get(
            path,
            headers={"x-forwarded-host": DELIVERY_FQDN},
            follow_redirects=False,
        )

    assert resp.status_code != 400


async def test_cookie_attributes_on_a_successful_login(monkeypatch, client, operator_present):
    async def _fake_credential(conn, username):
        return {"id": uuid.uuid4(), "password_hash": "$argon2id$fake"}

    async def _fake_verify(stored_hash, password):
        return True

    async def _fake_create_session(conn, token_hash, operator_id, lifetime_seconds, expected_password_hash):
        return True

    monkeypatch.setattr(queries, "credential_by_username", _fake_credential)
    monkeypatch.setattr(auth, "verify_password", _fake_verify)
    monkeypatch.setattr(queries, "create_session", _fake_create_session)

    async with client:
        resp = await client.post(
            "/login",
            data={"username": "op", "password": "correct password"},
            follow_redirects=False,
        )

    raw_cookie = resp.headers.get("set-cookie", "")
    assert raw_cookie, "a successful login must set a session cookie"
    assert "HttpOnly" in raw_cookie
    assert "Secure" in raw_cookie
    assert "samesite=lax" in raw_cookie.lower()
    assert "Path=/" in raw_cookie
    assert "Domain=" not in raw_cookie


@pytest.mark.parametrize("path", ["/library", f"/library/{uuid.uuid4()}/dismiss"])
async def test_the_session_gate_runs_outside_the_cross_origin_middleware(
    client, operator_present, path
):
    async with client:
        resp = await client.post(
            path,
            headers={"origin": "https://evil.example"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")
    assert resp.status_code != 403


@pytest.mark.parametrize("path", ["/login", f"/library/{uuid.uuid4()}/dismiss"])
async def test_a_browser_post_from_the_interface_own_hostname_is_not_refused(
    monkeypatch, signed_in_client, operator_present, path
):
    monkeypatch.setattr(
        queries,
        "failed_title_object_keys",
        lambda conn, title_id: _raise(LookupError()),
    )

    async with signed_in_client:
        resp = await signed_in_client.post(
            path,
            headers={
                "origin": ADMIN_ORIGIN,
                "x-forwarded-host": ADMIN_FQDN,
            },
            data={"username": "op", "password": "irrelevant", "next": "/library"},
            follow_redirects=False,
        )

    assert resp.status_code != 403


async def _raise(exc):
    raise exc


async def test_a_browser_post_from_another_site_is_still_refused(client, operator_present):
    async with client:
        resp = await client.post(
            "/login",
            headers={
                "origin": "https://evil.example",
                "x-forwarded-host": ADMIN_FQDN,
            },
            data={"username": "op", "password": "irrelevant"},
            follow_redirects=False,
        )

    assert resp.status_code == 403


@pytest.mark.parametrize(
    "next_value",
    ["//evil.example", "https://evil.example", "/\\evil.example"],
)
async def test_a_next_value_that_leaves_the_site_is_replaced_by_the_library(
    client, no_operator, next_value
):
    """no_operator here only stands in for "an operator exists" being
    irrelevant to this behaviour -- the redirect-sanitising rule applies
    whether or not the credential table has a row; a real login is
    exercised in the integration half.
    """
    async with client:
        resp = await client.get(
            f"/login?next={urllib.parse.quote(next_value, safe='')}",
            follow_redirects=False,
        )

    # A GET to /login never redirects to /library on its own (that only
    # happens after a successful POST or an already-signed-in visit) --
    # this only proves safe_next() itself never echoes an off-site value
    # back into the rendered form's hidden field.
    assert next_value not in resp.text


async def test_an_authenticated_page_is_not_stored_by_the_browser(
    monkeypatch, signed_in_client
):
    monkeypatch.setattr(queries, "resolve_slug", lambda conn, slug: _future(_active_row()))
    monkeypatch.setattr(
        queries,
        "library_row",
        lambda conn, title_id: _future({"title_name": "Some Title"}),
    )

    async with signed_in_client:
        resp = await signed_in_client.get("/check/some-slug", follow_redirects=False)

    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers["content-type"].startswith("text/html")


async def test_a_public_asset_is_still_cacheable(client):
    async with client:
        resp = await client.get("/static/style.css", follow_redirects=False)

    assert "no-store" not in resp.headers.get("cache-control", "")


async def test_a_poster_keeps_its_own_cache_header_through_the_gate(
    monkeypatch, signed_in_client
):
    import orchestrator.web.routes_titles as routes_titles

    monkeypatch.setattr(routes_titles, "get_object_bytes", lambda key: b"fake-jpeg-bytes")

    async with signed_in_client:
        resp = await signed_in_client.get(f"/poster/{uuid.uuid4()}", follow_redirects=False)

    assert resp.headers.get("cache-control") == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("path", ["/logout", f"/library/{uuid.uuid4()}/dismiss"])
async def test_a_stale_post_redirects_to_the_login_form_without_a_next_value(
    client, operator_present, path
):
    async with client:
        resp = await client.post(path, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_a_dead_session_cookie_says_the_session_ended(monkeypatch, client, operator_present):
    async def _fake_session_operator_id_none(request):
        return None, False

    monkeypatch.setattr(auth, "session_operator_id", _fake_session_operator_id_none)
    client.cookies.update({"mytube_session": "a-cookie-that-does-not-validate"})

    async with client:
        with_cookie = await client.get("/library", follow_redirects=False)

    client2_transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=client2_transport, base_url="http://testserver"
    ) as no_cookie_client:
        without_cookie = await no_cookie_client.get("/library", follow_redirects=False)

    assert "expired=1" in with_cookie.headers["location"]
    assert "expired=1" not in without_cookie.headers["location"]


async def test_login_on_a_fresh_stack_redirects_to_the_setup_page(client, no_operator):
    async with client:
        resp = await client.get("/login", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup"


def test_the_admin_hostname_may_not_equal_the_delivery_hostname(monkeypatch):
    monkeypatch.setenv("MYTUBE_ADMIN_FQDN", DELIVERY_FQDN)
    get_settings.cache_clear()

    from orchestrator.config import Settings

    with pytest.raises(Exception) as exc_info:
        Settings()

    message = str(exc_info.value)
    assert "MYTUBE_ADMIN_FQDN" in message
    assert "MYTUBE_PUBLIC_BASE_URL" in message
    get_settings.cache_clear()


async def test_the_language_switcher_preserves_the_query_string(client, no_operator):
    current = "/login?next=%2Flibrary"
    expected_param = urllib.parse.quote(current, safe="")

    async with client:
        resp = await client.get(current, follow_redirects=False)

    assert resp.text.count(f"next={expected_param}") >= 2


def _ru_switcher_href(text: str) -> str:
    """Pulls the Russian language-switcher's own href out of a rendered
    page, rather than reconstructing one -- the href is what a dropped
    error banner broke, so a hand-built address would pass against the
    broken code too.
    """
    match = re.search(r'href="(/lang/ru\?next=[^"]*)"', text)
    assert match is not None, "no Russian language-switcher link on the page"
    return match.group(1)


async def test_a_failed_login_survives_a_language_switch(
    client, operator_present, monkeypatch
):
    monkeypatch.setattr(
        queries, "credential_by_username", lambda conn, username: _future(None)
    )
    monkeypatch.setattr(
        auth, "verify_absent_credential", lambda: _future(None)
    )
    monkeypatch.setattr(auth, "FAILED_ATTEMPT_DELAY_SECONDS", 0)

    async with client:
        failure = await client.post(
            "/login", data={"username": "someone", "password": "wrong"}
        )
        href = _ru_switcher_href(failure.text)
        switched = await client.get(href, follow_redirects=True)

    assert 'lang="ru"' in switched.text
    assert "banner--error" in switched.text
    assert 'id="login-password"' in switched.text
    assert re.search(r'<input[^>]*id="login-username"[^>]*value=""', switched.text)
    # Not the same claim as the empty field above: this one fails if the name
    # reappears anywhere at all -- a hidden input, a switcher href, a log line
    # rendered into the page -- which is what "it does not travel" has to mean
    # for the reason it was decided (browser history, the edge access log).
    assert "someone" not in switched.text


async def test_a_failed_password_setup_survives_a_language_switch(
    client, no_operator
):
    async with client:
        failure = await client.post(
            "/setup",
            data={
                "token": "test-setup-token",
                "username": "someone",
                "password": "one-password",
                "password_confirm": "another-password",
            },
        )
        href = _ru_switcher_href(failure.text)
        switched = await client.get(href, follow_redirects=True)

    assert 'lang="ru"' in switched.text
    assert "banner--error" in switched.text
    assert re.search(r'<input[^>]*id="setup-username"[^>]*value=""', switched.text)
    assert 'name="token" value="test-setup-token"' in switched.text
    assert "someone" not in switched.text


async def test_an_error_flag_cannot_cross_between_the_two_screens(
    client, operator_present
):
    """The two allow-maps are separate on purpose, and nothing else notices.

    Collapsing `_LOGIN_ERROR_KEYS` and `_SETUP_ERROR_KEYS` into one map is the
    obvious tidying edit -- they sit together, hold disjoint keys and have the
    same shape -- and the whole suite stays green while the sign-in screen
    starts accepting create-password errors from anyone who can get the
    operator to open a link. This is the test that goes red instead.
    """
    async with client:
        borrowed = await client.get("/login?error=password_mismatch")
        invented = await client.get("/login?error=no-such-flag")
        hostile = await client.get("/login?error=<script>alert(1)</script>")
        genuine = await client.get("/login?error=invalid_credentials")

    assert "banner--error" not in borrowed.text
    assert "banner--error" not in invented.text
    assert "banner--error" not in hostile.text
    assert "banner--error" in genuine.text
    # A crafted value still reaches the page, inside the switcher's own href,
    # and asserting it is absent would be asserting the wrong thing. What it
    # must never do is arrive there as markup: percent-encoded is the whole
    # defence, and the page carries a legitimate <script> tag of its own, so
    # the payload is what gets named here rather than the tag.
    assert "<script>alert" not in hostile.text
    # Encoded twice, and that is correct rather than a quirk: the flag is
    # already a query value inside `current_url`, and `current_url` is then
    # itself a query value inside the switcher's `next=`.
    assert "%253Cscript%253Ealert" in hostile.text


# --- Integration half ----------------------------------------------------


@pytest.mark.integration
async def _signed_up_operator(raw_conn, username="op", password="correct horse battery"):
    """Not a fixture -- a helper the integration tests below call directly
    once orchestrator.auth/db.queries exist, to seed a real operator row
    without going through the CLI or the setup route.
    """
    from orchestrator import auth as _auth

    operator_id = uuid.uuid4()
    password_hash = await _auth.hash_password(password)
    await queries.create_operator(raw_conn, operator_id, username, password_hash)
    return operator_id, username, password


@pytest.fixture
def integration_client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="https://testserver")


@pytest.mark.integration
async def test_signing_in_returns_the_operator_to_where_they_were_going(
    app_pool, auth_tables, raw_conn, integration_client
):
    _, username, password = await _signed_up_operator(raw_conn)

    async with integration_client:
        resp = await integration_client.post(
            "/login",
            data={"username": username, "password": password, "next": "/library"},
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/library"
    assert "set-cookie" in resp.headers


@pytest.mark.integration
async def test_a_wrong_password_re_renders_the_form_with_the_generic_error(
    app_pool, auth_tables, raw_conn, integration_client
):
    _, username, _ = await _signed_up_operator(raw_conn)

    async with integration_client:
        resp = await integration_client.post(
            "/login",
            data={"username": username, "password": "definitely-wrong"},
            follow_redirects=False,
        )

    assert resp.status_code == 200
    assert (
        "login.error.invalid_credentials" in resp.text
        or "Incorrect username or password" in resp.text
    )
    # Generic for both an unknown username and a wrong password -- the
    # message never distinguishes which field was wrong.
    assert "username is" not in resp.text.lower()
    assert "password is wrong" not in resp.text.lower()


@pytest.mark.integration
async def test_signout_deletes_the_session_row_and_the_stale_cookie_stops_working(
    app_pool, auth_tables, raw_conn, integration_client
):
    _, username, password = await _signed_up_operator(raw_conn)

    async with integration_client:
        login_resp = await integration_client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        assert "set-cookie" in login_resp.headers

        # Prove the cookie actually travels before concluding anything
        # from it failing to, later.
        still_working = await integration_client.get("/library", follow_redirects=False)
        assert still_working.status_code == 200

        rows_before = await raw_conn.fetch("SELECT * FROM sessions")
        assert len(rows_before) == 1

        await integration_client.post("/logout", follow_redirects=False)

        rows_after = await raw_conn.fetch("SELECT * FROM sessions")
        assert len(rows_after) == 0

        replay = await integration_client.get("/library", follow_redirects=False)

    assert replay.status_code == 302
    assert replay.headers["location"].startswith("/login")


@pytest.mark.integration
async def test_signing_out_leaves_another_device_signed_in(
    app_pool, auth_tables, raw_conn, integration_client
):
    _, username, password = await _signed_up_operator(raw_conn)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as device_a, httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as device_b:
        await device_a.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )
        await device_b.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )

        await device_a.post("/logout", follow_redirects=False)

        still_signed_in = await device_b.get("/library", follow_redirects=False)

    assert still_signed_in.status_code == 200


@pytest.mark.integration
async def test_the_sliding_window_extends_a_live_session(
    app_pool, auth_tables, raw_conn, integration_client
):
    _, username, password = await _signed_up_operator(raw_conn)

    async with integration_client:
        await integration_client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )

        row = await raw_conn.fetchrow("SELECT token_hash, expires_at FROM sessions")
        await raw_conn.execute(
            "UPDATE sessions SET expires_at = now() + interval '2 days' WHERE token_hash = $1",
            row["token_hash"],
        )
        before = await raw_conn.fetchval(
            "SELECT expires_at FROM sessions WHERE token_hash = $1", row["token_hash"]
        )

        await integration_client.get("/library", follow_redirects=False)

        after = await raw_conn.fetchval(
            "SELECT expires_at FROM sessions WHERE token_hash = $1", row["token_hash"]
        )

    assert after > before


@pytest.mark.integration
async def test_the_expiry_sweep_removes_a_timed_out_session(app_pool, auth_tables, raw_conn):
    from orchestrator.reaper import sweep_sessions_once

    _, username, _ = await _signed_up_operator(raw_conn, username="expiry-op")
    operator_id = await raw_conn.fetchval(
        "SELECT id FROM operator_credential WHERE username = $1", "expiry-op"
    )

    await raw_conn.execute(
        "INSERT INTO sessions (token_hash, operator_id, expires_at) "
        "VALUES ('expired-token-hash', $1, now() - interval '1 day')",
        operator_id,
    )
    await raw_conn.execute(
        "INSERT INTO sessions (token_hash, operator_id, expires_at) "
        "VALUES ('live-token-hash', $1, now() + interval '1 day')",
        operator_id,
    )

    await sweep_sessions_once(app_pool)

    remaining = {
        r["token_hash"] for r in await raw_conn.fetch("SELECT token_hash FROM sessions")
    }
    assert remaining == {"live-token-hash"}


@pytest.mark.integration
async def test_first_run_with_an_empty_credential_table_routes_to_the_setup_page(
    app_pool, auth_tables, integration_client
):
    async with integration_client:
        resp = await integration_client.get("/library", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup"


@pytest.mark.integration
async def test_the_setup_page_refuses_a_wrong_token(app_pool, auth_tables, integration_client):
    async with integration_client:
        resp = await integration_client.get("/setup?token=definitely-wrong", follow_redirects=False)

    assert resp.status_code == 200
    assert "<form" not in resp.text or 'type="password"' not in resp.text


@pytest.mark.integration
async def test_creating_the_password_signs_the_operator_in(app_pool, auth_tables):
    """The live token is minted at process startup (app.state.setup_token),
    which this in-process transport never runs -- this test is written
    against that eventual mechanism and stays red until plan 05 exposes a
    way to set it directly for a test.
    """
    token = getattr(app.state, "setup_token", None)
    if token is None:
        pytest.skip("app.state.setup_token not wired until plan 05")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        resp = await c.post(
            "/setup",
            data={
                "token": token,
                "username": "first-operator",
                "password": "a genuinely long passphrase",
                "password_confirm": "a genuinely long passphrase",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/library"
    assert "set-cookie" in resp.headers


@pytest.mark.integration
async def test_the_setup_page_looks_the_same_once_an_account_exists(
    app_pool, auth_tables, raw_conn, integration_client
):
    await _signed_up_operator(raw_conn)

    async with integration_client:
        with_wrong_token = await integration_client.get(
            "/setup?token=wrong", follow_redirects=False
        )
        with_no_token = await integration_client.get("/setup", follow_redirects=False)

    assert with_wrong_token.status_code == with_no_token.status_code == 200
    assert with_wrong_token.text == with_no_token.text


@pytest.mark.integration
async def test_the_setup_page_tells_a_first_arrival_where_the_link_is(
    app_pool, auth_tables, integration_client
):
    async with integration_client:
        resp = await integration_client.get("/setup", follow_redirects=False)

    assert resp.status_code == 200
    # Opposite state from the once-an-account-exists case above: this must
    # not render whatever that one renders when no token is supplied.
    assert "docker compose logs" in resp.text.lower() or "container log" in resp.text.lower()


@pytest.mark.integration
@pytest.mark.parametrize("token_value", [None, "", "wrong", "ü"])
async def test_the_setup_page_survives_every_shape_of_token(
    app_pool, auth_tables, integration_client, token_value
):
    query = "" if token_value is None else f"?token={urllib.parse.quote(token_value)}"

    async with integration_client:
        resp = await integration_client.get(f"/setup{query}", follow_redirects=False)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.integration
async def test_a_second_operator_cannot_be_created(app_pool, auth_tables, raw_conn):
    await _signed_up_operator(raw_conn, username="first-op")

    with pytest.raises(Exception):
        await queries.create_operator(
            raw_conn, uuid.uuid4(), "second-op", "$argon2id$fake"
        )


@pytest.mark.integration
async def test_setup_refuses_once_an_operator_exists_even_with_the_live_token(
    app_pool, auth_tables, raw_conn, integration_client
):
    token = getattr(app.state, "setup_token", None)
    if token is None:
        pytest.skip("app.state.setup_token not wired until plan 05")

    await _signed_up_operator(raw_conn)

    async with integration_client:
        resp = await integration_client.post(
            "/setup",
            data={
                "token": token,
                "username": "sneaky",
                "password": "irrelevant password value",
                "password_confirm": "irrelevant password value",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 200
    count = await raw_conn.fetchval("SELECT count(*) FROM operator_credential")
    assert count == 1


@pytest.mark.integration
async def test_a_login_in_flight_across_a_password_reset_does_not_create_a_session(
    app_pool, auth_tables, raw_conn
):
    from orchestrator import auth as _auth

    operator_id, username, password = await _signed_up_operator(raw_conn)
    old_hash = await raw_conn.fetchval(
        "SELECT password_hash FROM operator_credential WHERE id = $1", operator_id
    )

    new_hash = await _auth.hash_password("a brand new password entirely")
    await queries.set_password_and_revoke_sessions(raw_conn, operator_id, new_hash)

    raw, token_hash = _auth.new_session_token()
    created = await queries.create_session(
        raw_conn, token_hash, operator_id, _auth.SESSION_LIFETIME_SECONDS, old_hash
    )

    assert created is False
    remaining = await raw_conn.fetchval("SELECT count(*) FROM sessions")
    assert remaining == 0


@pytest.mark.integration
async def test_the_cookie_lifetime_moves_when_the_window_does(
    app_pool, auth_tables, raw_conn, integration_client
):
    _, username, password = await _signed_up_operator(raw_conn)

    async with integration_client:
        await integration_client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )

        row = await raw_conn.fetchrow("SELECT token_hash FROM sessions")
        await raw_conn.execute(
            "UPDATE sessions SET expires_at = now() + interval '28 days' WHERE token_hash = $1",
            row["token_hash"],
        )
        before = await raw_conn.fetchval(
            "SELECT expires_at FROM sessions WHERE token_hash = $1", row["token_hash"]
        )

        first_resp = await integration_client.get("/library", follow_redirects=False)
        after_first = await raw_conn.fetchval(
            "SELECT expires_at FROM sessions WHERE token_hash = $1", row["token_hash"]
        )

        second_resp = await integration_client.get("/library", follow_redirects=False)
        after_second = await raw_conn.fetchval(
            "SELECT expires_at FROM sessions WHERE token_hash = $1", row["token_hash"]
        )

    assert after_first > before
    assert "set-cookie" in first_resp.headers
    assert after_second == after_first
    assert "set-cookie" not in second_resp.headers


@pytest.mark.integration
async def test_setup_refuses_a_blank_operator_name(
    app_pool, auth_tables, no_operator, integration_client
):
    """The schema admits exactly one operator, so a blank name is not a row
    somebody corrects later -- it is the only credential the deployment
    will ever have, with no name to sign in under and no second account to
    fix it from. The form comes back carrying its error instead, and no
    operator is created.
    """
    async with integration_client:
        resp = await integration_client.post(
            "/setup",
            data={
                "token": "test-setup-token",
                "username": "   ",
                "password": "a genuinely long passphrase",
                "password_confirm": "a genuinely long passphrase",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers
    # Still the form, not the redirect a successful create would send.
    assert 'type="password"' in resp.text


@pytest.mark.integration
async def test_signing_out_clears_the_cookie_even_when_the_database_is_unreachable(
    app_pool, auth_tables, signed_in, integration_client, monkeypatch
):
    """Sign-out must not fail open. The delete used to raise straight out of
    the handler into a 500, which left the browser holding the very cookie
    the operator had just asked to be rid of -- the one direction this must
    never fail. The row can outlive the click, because it carries an expiry
    and the sweep collects it; a cookie sitting in a browser nobody is
    watching does not.
    """
    called = []

    async def _unreachable(*args, **kwargs):
        called.append(True)
        raise OSError("database is unreachable")

    monkeypatch.setattr(queries, "delete_session", _unreachable)

    async with integration_client:
        resp = await integration_client.post(
            "/logout",
            headers={"Cookie": f"{auth.SESSION_COOKIE_NAME}=a-cookie-the-browser-still-holds"},
            follow_redirects=False,
        )

    # Without this the test would pass trivially on a request that never
    # carried a session cookie, proving nothing about the failure path.
    assert called, "the delete was never attempted -- the test proved nothing"

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?signed_out=1"
    assert any(
        auth.SESSION_COOKIE_NAME in value and "Max-Age=0" in value
        for value in resp.headers.get_list("set-cookie")
    ), resp.headers.get_list("set-cookie")
