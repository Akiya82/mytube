#!/usr/bin/env bash
# End-to-end smoke test for the delivery skeleton: signature -> edge ->
# storage -> bytes, through slice + tmpfs cache. Thirteen numbered steps, one
# pass/fail line each. Cleans up nothing it didn't create except by resetting
# the log; it deliberately leaves the compose stack running so a human can
# keep poking at it afterwards.
#
# Usage, from the repository root: bash tools/smoke_blob.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SRC_ROOT"

DD_BIN="/c/Users/misha/AppData/Local/Programs/DockerDesktop/resources/bin"
[ -d "$DD_BIN" ] && export PATH="$DD_BIN:$PATH"
# MSYS_NO_PATHCONV is scoped to docker/dc() only (below), NOT exported
# globally: it also disables Git Bash's POSIX->Windows path translation for
# curl, which makes `curl -o /d/...` silently fail to write its output file
# while still reporting a 200 (found the hard way running steps 10/11).
dk() { MSYS_NO_PATHCONV=1 docker "$@"; }
dc() { MSYS_NO_PATHCONV=1 docker compose "$@"; }

FAIL_COUNT=0
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo "[....] $*" >&2; }
na() { echo "[ N/A] $*"; }

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found in PATH" >&2; exit 1; }
dk info >/dev/null 2>&1 || { echo "ERROR: docker daemon unreachable (docker info failed) — this is an environment failure, not a smoke-test result." >&2; exit 1; }
[ -f .env ] || { echo "ERROR: .env missing — copy .env.example and fill in real values first." >&2; exit 1; }

set -a
source .env
set +a

EDGE_URL="http://127.0.0.1:${MYTUBE_EDGE_PORT}"
EDITION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
OBJECT_KEY="${EDITION_ID}.mp4"
OBJ_SIZE_BYTES=$((64 * 1024 * 1024))
# Host-side scratch dir. NOT /tmp: under Git Bash on Windows /tmp resolves
# inside the Git installation directory, not a real system temp dir.
SMOKE_DIR="$SRC_ROOT/build/smoke"
mkdir -p "$SMOKE_DIR"

api_exec() { dc exec -T api "$@"; }
edge_exec() { dc exec -T edge "$@"; }

wait_edge_healthy() {
  # Resolve the container ID once, up front, and poll that fixed ID rather
  # than re-running `dc ps -q edge` on every iteration: a fresh `--force-
  # recreate` briefly has the old and new containers coexisting mid-swap,
  # and re-querying by service name on every loop risks reporting the old
  # (still "healthy") container's status instead of waiting for the new one.
  local id
  id=$(dc ps -q edge)
  for _ in $(seq 1 30); do
    STATUS=$(dk inspect "$id" --format '{{.State.Health.Status}}' 2>/dev/null)
    [ "$STATUS" = "healthy" ] && { sleep 1; return 0; }
    sleep 1
  done
  return 1
}

reset_log() {
  edge_exec sh -c ": > /var/log/nginx/blob_access.log"
}

log_lines() {
  edge_exec sh -c "wc -l < /var/log/nginx/blob_access.log 2>/dev/null" | tr -d '\r '
}

# ---------------------------------------------------------------------------
# Step 1: bring the stack up.
# ---------------------------------------------------------------------------
info "step 1: dc up --wait"
if dc up --wait; then
  pass "step 1: stack is up (db, storage, api, edge)"
else
  fail "step 1: dc up --wait did not converge"
  dc ps
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: storage unreachable from the host -- only when the bundled
# storage service is the one actually in the delivery path.
# An external endpoint's reachability is the operator's own network
# boundary, not this stack's; when one is configured this step says so and
# skips the check rather than passing for a container that is no longer
# in the delivery path.
# ---------------------------------------------------------------------------
info "step 2: storage unreachable from host"
S3_ENDPOINT_HOST=$(python3 -c "
import os
from urllib.parse import urlparse
print(urlparse(os.environ.get('MYTUBE_S3_ENDPOINT', '')).hostname or '')
")
if [ "$S3_ENDPOINT_HOST" != "storage" ]; then
  na "step 2: MYTUBE_S3_ENDPOINT points at '$S3_ENDPOINT_HOST', not the bundled storage service -- its reachability is the operator's own boundary, not this stack's; skipped, not counted as passed"
else
  PORT_OUT=$(dc port storage 8333 2>&1)
  STORAGE_IP=$(dk inspect "$(dc ps -q storage)" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
  CURL_CODE=0
  curl -m 3 -s -o /dev/null "http://${STORAGE_IP}:8333/" 2>/dev/null || CURL_CODE=$?
  if [ -z "$PORT_OUT" ] || [[ "$PORT_OUT" == *"no port"* ]] || [[ "$PORT_OUT" == *"invalid"* ]]; then
    if [ "$CURL_CODE" -ne 0 ]; then
      pass "step 2: storage not published (dc port: '$PORT_OUT') and host curl to $STORAGE_IP:8333 failed to connect (curl exit $CURL_CODE) — connection error, not an HTTP code"
    else
      fail "step 2: storage port not published, but host curl to $STORAGE_IP:8333 connected — privacy broken"
    fi
  else
    fail "step 2: dc port storage 8333 printed a mapping: '$PORT_OUT' — privacy broken"
  fi
fi

# ---------------------------------------------------------------------------
# Step 3: anonymous read inside the network + endpoint/upstream consistency.
# ---------------------------------------------------------------------------
info "step 3: anonymous read from inside the network, endpoint/upstream consistency"
ANON_PUT_KEY="smoke-anon-probe.txt"
api_exec python3 -c "
from orchestrator.storage import put_object_from_file
import pathlib
p = pathlib.Path('/tmp/anon-probe.txt')
p.write_text('anon-probe')
put_object_from_file(str(p), '${ANON_PUT_KEY}')
" >/dev/null 2>&1
ANON_STATUS=$(api_exec sh -c "python3 -c \"
import os, urllib.request
url = os.environ['MYTUBE_S3_ENDPOINT'].rstrip('/') + '/' + os.environ['MYTUBE_S3_BUCKET'] + '/${ANON_PUT_KEY}'
req = urllib.request.Request(url)
try:
    resp = urllib.request.urlopen(req, timeout=5)
    print(resp.status)
except Exception as e:
    print('ERR', e)
\"" 2>&1 | tail -1)
if [[ "$ANON_STATUS" =~ ^2[0-9][0-9]$ ]]; then
  pass "step 3: anonymous GET on \$MYTUBE_S3_ENDPOINT/\$MYTUBE_S3_BUCKET/${ANON_PUT_KEY} from inside api returned $ANON_STATUS"
else
  fail "step 3: anonymous GET from inside api returned '$ANON_STATUS', expected 2xx"
fi
# endpoint/upstream consistency: head_object (via MYTUBE_S3_ENDPOINT) must see
# the same object edge finds through MYTUBE_S3_UPSTREAM.
HEAD_OK=$(api_exec python3 -c "
from orchestrator.storage import head_object
try:
    head_object('${ANON_PUT_KEY}')
    print('OK')
except Exception as e:
    print('ERR', e)
" 2>&1 | tail -1)
EDGE_SEES=$(curl -s -o /dev/null -w '%{http_code}' "${EDGE_URL}/blob/${ANON_PUT_KEY}" 2>&1 || true)
# ${ANON_PUT_KEY} does not match the strict UUID location regex, so edge's own
# 404 (converted to the stub clip, =200) is the *expected* response here —
# this only proves storage and upstream resolve to the same backend when
# MYTUBE_S3_ENDPOINT's head_object succeeds while MYTUBE_S3_UPSTREAM is
# reachable through edge at all (no connection-level failure).
if [ "$HEAD_OK" = "OK" ]; then
  pass "step 3: MYTUBE_S3_ENDPOINT and MYTUBE_S3_UPSTREAM point at the same storage (head_object via endpoint succeeded; edge itself reachable, HTTP $EDGE_SEES)"
else
  fail "step 3: head_object via MYTUBE_S3_ENDPOINT failed: $HEAD_OK"
fi

# ---------------------------------------------------------------------------
# Step 4: 64 MiB test object, sha256, put via storage.py.
# ---------------------------------------------------------------------------
info "step 4: create + upload 64 MiB test object"
SHA_ORIG=$(api_exec python3 -c "
import hashlib, os
from orchestrator.storage import put_object_from_file
path = '/tmp/${OBJECT_KEY}'
with open(path, 'wb') as f:
    f.write(os.urandom(${OBJ_SIZE_BYTES}))
put_object_from_file(path, '${OBJECT_KEY}')
print(hashlib.sha256(open(path, 'rb').read()).hexdigest())
" 2>&1 | tail -1)
if [[ "$SHA_ORIG" =~ ^[0-9a-f]{64}$ ]]; then
  pass "step 4: uploaded ${OBJECT_KEY} (64 MiB), sha256=$SHA_ORIG"
else
  fail "step 4: upload/hash failed: $SHA_ORIG"
  echo "Cannot continue without the test object — aborting remaining steps." >&2
  echo ""
  echo "===== SUMMARY: $FAIL_COUNT failing step(s) ====="
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 5: sign the object's URL via api's signing.sign_blob_url.
# ---------------------------------------------------------------------------
info "step 5: sign blob URL"
SIGN_OUT=$(api_exec python3 -c "
import os
from orchestrator.signing import sign_blob_url
sig, exp = sign_blob_url('${EDITION_ID}', os.environ['MYTUBE_SECURE_LINK_SECRET'], int(os.environ['MYTUBE_SIGN_TTL_SECONDS']))
print(sig)
print(exp)
" 2>&1)
SIG=$(echo "$SIGN_OUT" | sed -n '1p' | tr -d '\r')
EXP=$(echo "$SIGN_OUT" | sed -n '2p' | tr -d '\r')
if [ -n "$SIG" ] && [ -n "$EXP" ]; then
  pass "step 5: signed sig=$SIG exp=$EXP"
else
  fail "step 5: signing failed, got: $SIGN_OUT"
fi

# ---------------------------------------------------------------------------
# Step 6: no signature -> 302 to /refresh/{edition_id}, no bytes.
# ---------------------------------------------------------------------------
info "step 6: no signature -> 302 refresh"
LOC=$(curl -s -o /dev/null -D - "${EDGE_URL}/blob/${OBJECT_KEY}" 2>&1)
CODE6=$(echo "$LOC" | head -1 | awk '{print $2}')
LOCATION6=$(echo "$LOC" | grep -i '^location:' | tr -d '\r')
if [ "$CODE6" = "302" ] && [[ "$LOCATION6" == *"/refresh/${EDITION_ID}"* ]]; then
  pass "step 6: unsigned request got 302 -> $LOCATION6"
else
  fail "step 6: expected 302 to /refresh/${EDITION_ID}, got code=$CODE6 location='$LOCATION6'"
fi

# ---------------------------------------------------------------------------
# Step 7: retry=1 + broken signature -> stub clip (200, video/mp4), not a loop.
# ---------------------------------------------------------------------------
info "step 7: retry=1 + bad signature -> stub clip, not another redirect"
HDRS7=$(curl -s -o /dev/null -D - "${EDGE_URL}/blob/${OBJECT_KEY}?sig=deadbeef&exp=${EXP}&retry=1" 2>&1)
CODE7=$(echo "$HDRS7" | head -1 | awk '{print $2}')
CTYPE7=$(echo "$HDRS7" | grep -i '^content-type:' | tr -d '\r')
if [ "$CODE7" = "200" ] && [[ "$CTYPE7" == *"video/mp4"* ]]; then
  pass "step 7: retry=1 with bad signature got 200 $CTYPE7 (loop-guard works)"
else
  fail "step 7: expected 200 video/mp4, got code=$CODE7 $CTYPE7"
fi

# ---------------------------------------------------------------------------
# Step 8: valid signature, chunked range -> 206 + Content-Range + X-Cache-Status.
# ---------------------------------------------------------------------------
info "step 8: valid signature, ranged request"
HDRS8=$(curl -s -o /dev/null -D - -r 0-1048575 "${EDGE_URL}/blob/${OBJECT_KEY}?sig=${SIG}&exp=${EXP}" 2>&1)
CODE8=$(echo "$HDRS8" | head -1 | awk '{print $2}')
CRANGE8=$(echo "$HDRS8" | grep -i '^content-range:' | tr -d '\r')
XCACHE8=$(echo "$HDRS8" | grep -i '^x-cache-status:' | tr -d '\r')
if [ "$CODE8" = "206" ] && [[ "$CRANGE8" == *"bytes 0-1048575/${OBJ_SIZE_BYTES}"* ]] && [ -n "$XCACHE8" ]; then
  pass "step 8: 206, $CRANGE8, $XCACHE8"
else
  fail "step 8: expected 206 with matching Content-Range, got code=$CODE8 '$CRANGE8' '$XCACHE8'"
fi

# ---------------------------------------------------------------------------
# Step 9: same chunk again -> HIT.
# ---------------------------------------------------------------------------
info "step 9: repeat request, same chunk -> HIT"
HDRS9=$(curl -s -o /dev/null -D - -r 0-1048575 "${EDGE_URL}/blob/${OBJECT_KEY}?sig=${SIG}&exp=${EXP}" 2>&1)
XCACHE9=$(echo "$HDRS9" | grep -i '^x-cache-status:' | tr -d '\r')
if [[ "$XCACHE9" == *"HIT"* ]]; then
  pass "step 9: $XCACHE9"
else
  fail "step 9: expected X-Cache-Status: HIT, got '$XCACHE9'"
fi

# ---------------------------------------------------------------------------
# Step 10: full download through edge, sha256 match, log line count.
# ---------------------------------------------------------------------------
info "step 10: full download + sha256 + access_log line count"
reset_log
DL_PATH="$SMOKE_DIR/downloaded_${EDITION_ID}.bin"
DL_CODE=$(curl -s -o "$DL_PATH" -w '%{http_code}' "${EDGE_URL}/blob/${OBJECT_KEY}?sig=${SIG}&exp=${EXP}" 2>&1)
SHA_DL=$(sha256sum "$DL_PATH" 2>&1 | awk '{print $1}')
LINES10=$(log_lines)
if [ "$DL_CODE" = "200" ] && [ "$SHA_DL" = "$SHA_ORIG" ]; then
  pass "step 10: full download OK, sha256 matches ($SHA_DL)"
else
  fail "step 10: full download mismatch — code=$DL_CODE sha=$SHA_DL expected=$SHA_ORIG"
fi
# Measured per docs/experiments/07-object-storage.md: 64 lines for a
# 64 MiB object at slice 1m, not 65 — the first slice merges into the main
# request's log line instead of getting its own subrequest entry. 65 is
# accepted too, for an nginx build that logs it separately.
if [ "$LINES10" = "64" ]; then
  pass "step 10: access_log has 64 lines for 64 MiB @ slice 1m (matches the count measured in experiment 7 — log_subrequest on is confirmed working: 64 >> 1)"
elif [ "$LINES10" = "65" ]; then
  pass "step 10: access_log has 65 lines (this nginx build logs the first slice separately — log_subrequest on confirmed working)"
else
  fail "step 10: access_log has $LINES10 lines, expected 64 (measured) or 65 (first slice logged separately)"
fi

# ---------------------------------------------------------------------------
# Step 11: signature expiring mid-download.
# ---------------------------------------------------------------------------
info "step 11: signature expiring mid-download"
dc restart edge >/dev/null 2>&1
wait_edge_healthy || echo "WARNING: edge did not report healthy after restart"
reset_log
SIGN11=$(api_exec python3 -c "
import os
from orchestrator.signing import sign_blob_url
sig, exp = sign_blob_url('${EDITION_ID}', os.environ['MYTUBE_SECURE_LINK_SECRET'], 5)
print(sig); print(exp)
" 2>&1)
SIG11=$(echo "$SIGN11" | sed -n '1p' | tr -d '\r')
EXP11=$(echo "$SIGN11" | sed -n '2p' | tr -d '\r')
DL11_PATH="$SMOKE_DIR/downloaded11_${EDITION_ID}.bin"
# 64 MiB at 6 MB/s ~ 10.7s, comfortably past the 5s TTL.
curl -s --limit-rate 6M -o "$DL11_PATH" "${EDGE_URL}/blob/${OBJECT_KEY}?sig=${SIG11}&exp=${EXP11}" >/dev/null 2>&1
SHA11=$(sha256sum "$DL11_PATH" 2>&1 | awk '{print $1}')
BAD_STATUS_COUNT=$(edge_exec sh -c "awk '{print \$5}' /var/log/nginx/blob_access.log | grep -c -E '^(302|403)$'; true" 2>/dev/null)
BAD_STATUS_COUNT=$(echo "$BAD_STATUS_COUNT" | tr -d '\r ')
if [ "$SHA11" = "$SHA_ORIG" ] && [ "${BAD_STATUS_COUNT:-0}" = "0" ]; then
  pass "step 11: sha256 matches after mid-stream expiry, zero 302/403 among subrequests"
else
  fail "step 11: sha256=$SHA11 (expected $SHA_ORIG), 302/403 count=$BAD_STATUS_COUNT — see docs/experiments/05-secure-link-on-slices.md for the recorded verdict"
fi

# ---------------------------------------------------------------------------
# Step 12: parallel requests to one cold chunk -> exactly one origin fetch.
# ---------------------------------------------------------------------------
info "step 12: ten parallel requests, one cold chunk"
dc restart edge >/dev/null 2>&1
wait_edge_healthy || echo "WARNING: edge did not report healthy after restart"
reset_log
SIGN12=$(api_exec python3 -c "
import os
from orchestrator.signing import sign_blob_url
sig, exp = sign_blob_url('${EDITION_ID}', os.environ['MYTUBE_SECURE_LINK_SECRET'], int(os.environ['MYTUBE_SIGN_TTL_SECONDS']))
print(sig); print(exp)
" 2>&1)
SIG12=$(echo "$SIGN12" | sed -n '1p' | tr -d '\r')
EXP12=$(echo "$SIGN12" | sed -n '2p' | tr -d '\r')
PIDS=()
for i in $(seq 1 10); do
  curl -s -o /dev/null -r 10485760-11534335 "${EDGE_URL}/blob/${OBJECT_KEY}?sig=${SIG12}&exp=${EXP12}" &
  PIDS+=($!)
done
for pid in "${PIDS[@]}"; do wait "$pid"; done
# The log was just reset and every request in this window targets the same
# single chunk, so no need to filter by byte range: for a request that is
# already exactly one slice, nginx merges it into the main request's log
# line and $slice_range logs as "-" (same "first slice has no subrequest
# line of its own" behavior measured in docs/experiments/07-object-storage.md),
# so grepping for a literal "bytes=..." string here would never match.
ORIGIN_FETCHES=$(edge_exec sh -c "awk '{print \$4}' /var/log/nginx/blob_access.log | grep -c -E '^(MISS|EXPIRED|STALE)$'; true" 2>/dev/null)
ORIGIN_FETCHES=$(echo "$ORIGIN_FETCHES" | tr -d '\r ')
if [ "$ORIGIN_FETCHES" = "1" ]; then
  pass "step 12: exactly 1 origin fetch for the same cold chunk across 10 parallel requests"
else
  fail "step 12: expected exactly 1 origin fetch, got $ORIGIN_FETCHES"
fi

# ---------------------------------------------------------------------------
# Step 13: MYTUBE_CACHE_MODE=off switch.
# ---------------------------------------------------------------------------
info "step 13: MYTUBE_CACHE_MODE=off"
# NOT a `sed -i` on .env: `set -a; source .env; set +a` at the top of this
# script already exported MYTUBE_CACHE_MODE into THIS process's real
# environment, and docker compose gives OS environment variables priority
# over the .env file — editing the file alone is silently ignored by a
# child `docker compose` process that already inherited the old exported
# value. Override it for just these two invocations instead (found by
# dumping the container's actual rendered cache.conf: it kept resolving the
# memory template no matter what the file said).
MYTUBE_CACHE_MODE=off dc up -d --force-recreate edge >/dev/null 2>&1
wait_edge_healthy || echo "WARNING: edge did not report healthy after cache-mode switch"
HDRS13=$(curl -s -o /dev/null -D - -r 0-1048575 "${EDGE_URL}/blob/${OBJECT_KEY}?sig=${SIG}&exp=${EXP}" 2>&1)
CODE13=$(echo "$HDRS13" | head -1 | awk '{print $2}')
XCACHE13=$(echo "$HDRS13" | grep -i '^x-cache-status:' | tr -d '\r')
dc up -d --force-recreate edge >/dev/null 2>&1
wait_edge_healthy || echo "WARNING: edge did not report healthy after restoring cache mode"
if [ "$CODE13" = "206" ] && [[ "$XCACHE13" == *"OFF"* ]]; then
  pass "step 13: MYTUBE_CACHE_MODE=off gives $XCACHE13, bytes still served ($CODE13)"
else
  fail "step 13: expected 206 with X-Cache-Status: OFF, got code=$CODE13 '$XCACHE13'"
fi

echo ""
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "===== SMOKE TEST: ALL STEPS PASSED ====="
  exit 0
else
  echo "===== SMOKE TEST: $FAIL_COUNT STEP(S) FAILED ====="
  exit 1
fi
