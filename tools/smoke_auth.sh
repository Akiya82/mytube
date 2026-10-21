#!/usr/bin/env bash
# End-to-end smoke test for operator authentication: the interface answers on
# its own FQDN, stays closed without a session, and delivery is untouched.
# Fourteen numbered steps, one pass/fail line each. Mirrors
# tools/smoke_blob.sh's scaffold.
#
# Usage, from the repository root: bash tools/smoke_auth.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SRC_ROOT"

DD_BIN="/c/Users/misha/AppData/Local/Programs/DockerDesktop/resources/bin"
[ -d "$DD_BIN" ] && export PATH="$DD_BIN:$PATH"
# MSYS_NO_PATHCONV is scoped to docker/dc() only, NOT exported globally --
# see tools/smoke_blob.sh's own comment on why (it also disables Git Bash's
# POSIX->Windows path translation for curl).
dk() { MSYS_NO_PATHCONV=1 docker "$@"; }
dc() { MSYS_NO_PATHCONV=1 docker compose "$@"; }

FAIL_COUNT=0
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo "[....] $*" >&2; }
skipped() { echo "[SKIP] $*"; }

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found in PATH" >&2; exit 1; }
dk info >/dev/null 2>&1 || { echo "ERROR: docker daemon unreachable (docker info failed) — this is an environment failure, not a smoke-test result." >&2; exit 1; }
[ -f .env ] || { echo "ERROR: src/.env missing — copy .env.example and fill in real values first." >&2; exit 1; }

set -a
source .env
set +a

: "${MYTUBE_ADMIN_FQDN:?MYTUBE_ADMIN_FQDN is not set in .env — the interface has no address to answer on}"
: "${MYTUBE_EDGE_PORT:?MYTUBE_EDGE_PORT is not set in .env}"

EDGE_URL="http://127.0.0.1:${MYTUBE_EDGE_PORT}"
ADMIN="-H Host:${MYTUBE_ADMIN_FQDN}"
# A browser attaches Origin to every same-site form submission; curl does
# not, unless told to. Without this on every POST step, the script cannot
# see the one failure mode where the interface is unusable from a browser
# and perfect from a terminal -- exactly the shape of the cross-origin
# refusal once the host port is retired (see main.py's
# reject_cross_origin_writes and the browser_post tests in test_auth.py).
ORIGIN="-H Origin:https://${MYTUBE_ADMIN_FQDN}"

SMOKE_DIR="$SRC_ROOT/build/smoke"
mkdir -p "$SMOKE_DIR"
# No cookie-jar file: the session cookie carries Secure, and curl's cookie
# engine -- correctly, the same rule a browser applies -- refuses to store
# or resend a Secure cookie received over a plain http:// connection, which
# is exactly what EDGE_URL is (TLS is terminated by the operator's own
# reverse proxy in front of this stack, never by edge itself). Against a
# jar, every later step silently carried no cookie at all rather than the
# one just issued. Each step below extracts the raw cookie value out of the
# response headers instead and passes it explicitly with -b "name=value",
# which bypasses curl's Secure-attribute storage rule entirely because that
# form is a literal Cookie header, not a jar lookup.
extract_cookie() { grep -i '^set-cookie:' | sed -E 's/^[Ss]et-[Cc]ookie: *([^;]+);.*/\1/' | tr -d '\r'; }

RUN_USERNAME="smoke-op-$(date +%s)"
RUN_PASSWORD="smoke-password-$(date +%s)-not-a-real-secret"

api_exec() { dc exec -T api "$@"; }

# ---------------------------------------------------------------------------
# Step 1: bring the stack up.
# ---------------------------------------------------------------------------
info "step 1: dc up -d --wait"
if dc up -d --wait; then
  pass "step 1: stack is up"
else
  fail "step 1: dc up -d --wait did not converge"
  dc ps
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: the interface publishes no host port any more.
# ---------------------------------------------------------------------------
info "step 2: api publishes no host port"
PORT_OUT=$(dc port api 8000 2>&1)
if [ -z "$PORT_OUT" ] || [[ "$PORT_OUT" == *"no port"* ]] || [[ "$PORT_OUT" == *"invalid"* ]]; then
  pass "step 2: dc port api 8000 prints nothing (got: '$PORT_OUT')"
else
  fail "step 2: dc port api 8000 printed a mapping: '$PORT_OUT' — the interface is still reachable directly"
fi

# ---------------------------------------------------------------------------
# Step 3: create (or reset, if one already exists) the operator credential.
#
# This has to happen before the gate checks below it. It originally ran
# later (after the unauthenticated-redirect and login-form checks), on the
# unstated assumption that an operator would already exist by the time this
# script runs against a real stack. Once the empty-table first-run redirect
# existed, that assumption stopped holding: against a genuinely fresh
# database, every one of those checks landed on the create-password page
# instead, because the application was correctly refusing to show a login
# form nobody could pass yet. That is not a defect in the gate; it is this
# script asserting the wrong page for a state it never accounted for. Moving
# the credential's own creation first tests the fresh-database path
# explicitly (the create branch below, not just the always-passing reset
# branch) and lets every later step assert against the steady state the
# rest of this file is actually about.
# ---------------------------------------------------------------------------
info "step 3: create-operator (or reset-password if one already exists)"
CREATE_OUT=$(api_exec sh -c "echo '${RUN_PASSWORD}
${RUN_PASSWORD}' | python -m orchestrator.cli create-operator --username '${RUN_USERNAME}'" 2>&1)
CREATE_EXIT=$?
if [ "$CREATE_EXIT" -eq 0 ]; then
  LOGIN_USERNAME="$RUN_USERNAME"
  pass "step 3: operator credential created ($LOGIN_USERNAME)"
elif echo "$CREATE_OUT" | grep -qi "already exists"; then
  # A credential from an earlier run of this script is still present. Its
  # username was generated from that earlier run's own timestamp and is
  # unknown here, so this resets without --username, which resolves the
  # single existing row itself and prints back which account it changed --
  # a mismatched --username would have found no row and silently left the
  # old password in place, which is exactly what happened before this line
  # existed: reset-password --username '${RUN_USERNAME}' looked up a row
  # that was never there on a second run, and the previous check here
  # accepted "No operator account exists to reset." as success because it
  # loosely matched the substring "reset" in its own failure message.
  RESET_OUT=$(api_exec sh -c "echo '${RUN_PASSWORD}
${RUN_PASSWORD}' | python -m orchestrator.cli reset-password" 2>&1)
  RESET_EXIT=$?
  LOGIN_USERNAME=$(echo "$RESET_OUT" | sed -n "s/.*Password for '\([^']*\)'.*/\1/p")
  if [ "$RESET_EXIT" -eq 0 ] && [ -n "$LOGIN_USERNAME" ]; then
    pass "step 3: operator credential reset ($LOGIN_USERNAME)"
  else
    fail "step 3: create-operator reported an existing account, and reset-password did not confirm: $RESET_OUT"
    LOGIN_USERNAME="$RUN_USERNAME"
  fi
else
  fail "step 3: create-operator failed unexpectedly (exit $CREATE_EXIT): $CREATE_OUT"
  LOGIN_USERNAME="$RUN_USERNAME"
fi

# ---------------------------------------------------------------------------
# Step 4: unauthenticated GET /library redirects to the login form.
# ---------------------------------------------------------------------------
info "step 4: unauthenticated /library -> 302 /login?next=%2Flibrary"
HDRS4=$(curl -s -o /dev/null -D - $ADMIN "${EDGE_URL}/library" 2>&1)
CODE4=$(echo "$HDRS4" | head -1 | awk '{print $2}')
LOCATION4=$(echo "$HDRS4" | grep -i '^location:' | tr -d '\r' | awk '{print $2}')
if [ "$CODE4" = "302" ] && [ "$LOCATION4" = "/login?next=%2Flibrary" ]; then
  pass "step 4: 302 -> $LOCATION4"
else
  fail "step 4: expected 302 to /login?next=%2Flibrary, got code=$CODE4 location='$LOCATION4'"
fi

# ---------------------------------------------------------------------------
# Step 5: the login form itself renders.
# ---------------------------------------------------------------------------
info "step 5: /login renders a password field"
LOGIN_BODY=$(curl -s $ADMIN "${EDGE_URL}/login")
LOGIN_CTYPE=$(curl -s -o /dev/null -D - $ADMIN "${EDGE_URL}/login" | grep -i '^content-type:' | tr -d '\r')
if [[ "$LOGIN_CTYPE" == *"text/html"* ]] && [[ "$LOGIN_BODY" == *'type="password"'* ]]; then
  pass "step 5: 200 text/html with a password field"
else
  fail "step 5: expected 200 text/html with a password input, content-type='$LOGIN_CTYPE'"
fi

# ---------------------------------------------------------------------------
# Step 6: /healthz is not proxied externally.
# ---------------------------------------------------------------------------
info "step 6: /healthz is refused by nginx, not answered by the application"
HEALTHZ_CODE=$(curl -s -o /dev/null -w '%{http_code}' $ADMIN "${EDGE_URL}/healthz")
if [ "$HEALTHZ_CODE" = "404" ]; then
  pass "step 6: /healthz -> 404 from nginx"
else
  fail "step 6: expected 404, got $HEALTHZ_CODE — an anonymous endpoint that queries the database has no reason to be public"
fi

# ---------------------------------------------------------------------------
# Step 7: a wrong password takes the constant delay and gives the generic
# error.
# ---------------------------------------------------------------------------
info "step 7: wrong password -> generic error, at least one second wall-clock"
START7=$(date +%s%N)
HDRS7=$(curl -s -o /tmp/smoke_login_wrong_body -D - $ADMIN $ORIGIN -X POST \
  -d "username=${LOGIN_USERNAME}&password=definitely-the-wrong-password" \
  "${EDGE_URL}/login" 2>&1)
END7=$(date +%s%N)
ELAPSED_MS7=$(( (END7 - START7) / 1000000 ))
CODE7=$(echo "$HDRS7" | head -1 | awk '{print $2}')
BODY7=$(cat /tmp/smoke_login_wrong_body 2>/dev/null)
if [ "$CODE7" = "200" ] && [ "$ELAPSED_MS7" -ge 1000 ]; then
  pass "step 7: 200 after ${ELAPSED_MS7}ms (>= 1000ms constant delay observed)"
else
  fail "step 7: expected 200 after >=1000ms, got code=$CODE7 after ${ELAPSED_MS7}ms"
fi

# ---------------------------------------------------------------------------
# Step 8: the correct credentials sign in and set the session cookie.
# ---------------------------------------------------------------------------
info "step 8: correct credentials -> 302 /library, cookie set"
HDRS8=$(curl -s -o /dev/null -D - $ADMIN $ORIGIN -X POST \
  -d "username=${LOGIN_USERNAME}&password=${RUN_PASSWORD}" \
  "${EDGE_URL}/login" 2>&1)
CODE8=$(echo "$HDRS8" | head -1 | awk '{print $2}')
LOCATION8=$(echo "$HDRS8" | grep -i '^location:' | tr -d '\r')
SETCOOKIE8=$(echo "$HDRS8" | grep -i '^set-cookie:' | tr -d '\r')
SESSION_COOKIE=$(echo "$HDRS8" | extract_cookie)
if [ "$CODE8" = "302" ] \
  && [[ "$LOCATION8" == *"/library"* ]] \
  && [[ "$SETCOOKIE8" == *"mytube_session"* ]] \
  && [[ "$SETCOOKIE8" == *"HttpOnly"* ]] \
  && [[ "$SETCOOKIE8" == *"Secure"* ]] \
  && [[ "$SETCOOKIE8" == *"SameSite=Lax"* || "$SETCOOKIE8" == *"SameSite=lax"* ]] \
  && [[ "$SETCOOKIE8" != *"Domain="* ]] \
  && [ -n "$SESSION_COOKIE" ]; then
  pass "step 8: signed in, cookie carries HttpOnly, Secure, SameSite=Lax, no Domain"
else
  fail "step 8: expected 302 /library with a properly-attributed cookie, got code=$CODE8 location='$LOCATION8' set-cookie='$SETCOOKIE8'"
fi

# ---------------------------------------------------------------------------
# Step 9: the cookie actually grants access.
# ---------------------------------------------------------------------------
info "step 9: /library with the session cookie -> 200"
CODE9=$(curl -s -o /dev/null -w '%{http_code}' -b "$SESSION_COOKIE" $ADMIN "${EDGE_URL}/library")
if [ "$CODE9" = "200" ]; then
  pass "step 9: 200"
else
  fail "step 9: expected 200, got $CODE9"
fi

# ---------------------------------------------------------------------------
# Step 10: sign-out kills the session server-side.
# ---------------------------------------------------------------------------
info "step 10: sign-out, then the stale cookie no longer works"
CODE10_LOGOUT=$(curl -s -o /dev/null -w '%{http_code}' -b "$SESSION_COOKIE" $ADMIN $ORIGIN -X POST "${EDGE_URL}/logout")
CODE10_REPLAY=$(curl -s -o /dev/null -w '%{http_code}' -b "$SESSION_COOKIE" $ADMIN "${EDGE_URL}/library")
LOCATION10=$(curl -s -o /dev/null -D - -b "$SESSION_COOKIE" $ADMIN "${EDGE_URL}/library" | grep -i '^location:' | tr -d '\r')
if [ "$CODE10_LOGOUT" = "302" ] && [ "$CODE10_REPLAY" = "302" ] && [[ "$LOCATION10" == *"/login"* ]]; then
  pass "step 10: signed out, stale cookie now redirects to $LOCATION10"
else
  fail "step 10: expected 302/302 with a login redirect, got logout=$CODE10_LOGOUT replay=$CODE10_REPLAY location='$LOCATION10'"
fi

# ---------------------------------------------------------------------------
# Step 11: delivery is untouched -- an admin path on the delivery host still
# gets the stub clip, not HTML.
# ---------------------------------------------------------------------------
info "step 11: /library on the delivery host -> stub clip, not HTML"
HDRS11=$(curl -s -o /dev/null -D - "${EDGE_URL}/library" 2>&1)
CODE11=$(echo "$HDRS11" | head -1 | awk '{print $2}')
CTYPE11=$(echo "$HDRS11" | grep -i '^content-type:' | tr -d '\r')
if [ "$CODE11" = "200" ] && [[ "$CTYPE11" == *"video/mp4"* ]]; then
  pass "step 11: 200 $CTYPE11 (stub clip, unchanged)"
else
  fail "step 11: expected 200 video/mp4, got code=$CODE11 '$CTYPE11'"
fi

# ---------------------------------------------------------------------------
# Step 12: a poster keeps its own long-lived caching header through the gate.
#
# This has to run before the rate-limit burst below it, not after. It
# originally ran after: the burst step drives twenty rapid requests at
# /login and, by design, gets most of them refused with 503, which drains
# the same shared rate-limit zone this step's own sign-in POST also has to
# pass through. Running this step second meant its login could itself land
# on a freshly-drained zone and come back 503, which this step's own cookie
# extraction turned into an empty string, which then read as "no poster in
# the library" instead of the rate limit it actually was -- a real
# authenticated-poster answer never got checked, silently. Running the
# poster check first, while the zone is still fresh, is the fix; the burst
# step deliberately runs last, since nothing after it needs the zone clear.
# ---------------------------------------------------------------------------
info "step 12: a poster's caching header survives the gate"
HDRS12_LOGIN=$(curl -s -o /dev/null -D - $ADMIN $ORIGIN -X POST \
  -d "username=${LOGIN_USERNAME}&password=${RUN_PASSWORD}" "${EDGE_URL}/login" 2>&1)
POSTER_SESSION_COOKIE=$(echo "$HDRS12_LOGIN" | extract_cookie)
LIBRARY_HTML=$(curl -s -b "$POSTER_SESSION_COOKIE" $ADMIN "${EDGE_URL}/library")
POSTER_ID=$(echo "$LIBRARY_HTML" | grep -o '/poster/[0-9a-f-]\{36\}' | head -1 | sed 's#/poster/##')
if [ -z "$POSTER_ID" ]; then
  skipped "step 12: no poster in the library yet — nothing to check"
else
  POSTER_CACHE=$(curl -s -o /dev/null -D - -b "$POSTER_SESSION_COOKIE" $ADMIN "${EDGE_URL}/poster/${POSTER_ID}" | grep -i '^cache-control:' | tr -d '\r')
  if [[ "$POSTER_CACHE" == *"immutable"* ]] && [[ "$POSTER_CACHE" != *"no-store"* ]]; then
    pass "step 12: poster ${POSTER_ID} kept its own caching header: $POSTER_CACHE"
  else
    fail "step 12: expected the long-lived immutable header, got '$POSTER_CACHE'"
  fi
fi

# ---------------------------------------------------------------------------
# Step 13: the rate limit fires before a password hash ever runs.
# ---------------------------------------------------------------------------
info "step 13: twenty rapid wrong-password POSTs, at least one 503"
STATUS_LIST=""
for _ in $(seq 1 20); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' $ADMIN $ORIGIN -X POST \
    -d "username=${LOGIN_USERNAME}&password=still-wrong" \
    "${EDGE_URL}/login")
  STATUS_LIST="$STATUS_LIST $CODE"
done
COUNT_503=$(echo "$STATUS_LIST" | tr ' ' '\n' | grep -c '^503$')
info "twenty statuses observed:$STATUS_LIST"
if [ "$COUNT_503" -ge 1 ]; then
  pass "step 13: $COUNT_503 of 20 requests refused with 503 by the edge"
else
  fail "step 13: expected at least one 503 among 20 rapid attempts, got none — the rate-limit zone parses but is not applying"
fi

# This burst deliberately empties the shared mytube_auth zone (rate=10r/m,
# burst=5, nodelay -- edge/nginx.conf.template), which is exactly the point
# of the check above. Left alone, a second consecutive run of this script
# starts against that same still-draining zone: its own real login attempts
# a few steps later land on the leftover excess from this burst and come
# back 503 themselves, which is this test abusing itself, not a defect. At
# 10r/m the zone regenerates one slot every 6 seconds; waiting for the full
# five-slot burst to recover (30 seconds, plus a small margin) here is what
# makes the script genuinely re-runnable back to back, rather than only
# re-runnable after enough wall-clock time has passed on its own.
info "waiting for the rate-limit zone to recover before this run ends"
sleep 32

# ---------------------------------------------------------------------------
# Step 14: the application-level foreign-host refusal, exercised in-container
# because nginx overwrites X-Forwarded-Host on everything it proxies, so this
# is the only place a genuinely foreign value ever reaches the application.
# Runs through python3 -c, not curl: the api image is python:3.13-slim and
# ships no curl at all (see tools/smoke_blob.sh's own api_exec steps).
# ---------------------------------------------------------------------------
info "step 14: a foreign X-Forwarded-Host is refused in plain text, from inside the container"
RESULT14=$(api_exec python3 -c "
import urllib.request, urllib.error
req = urllib.request.Request('http://localhost:8000/library', headers={'X-Forwarded-Host': 'evil.example'})
try:
    resp = urllib.request.urlopen(req, timeout=5)
    print(resp.status, resp.headers.get('Content-Type', ''))
except urllib.error.HTTPError as e:
    print(e.code, e.headers.get('Content-Type', ''))
" 2>&1 | tail -1)
if [[ "$RESULT14" == 400\ text/plain* ]]; then
  pass "step 14: $RESULT14"
else
  fail "step 14: expected '400 text/plain...', got '$RESULT14'"
fi

echo ""
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "===== SMOKE TEST: ALL STEPS PASSED ====="
  exit 0
else
  echo "===== SMOKE TEST: $FAIL_COUNT STEP(S) FAILED ====="
  exit 1
fi
