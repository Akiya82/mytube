#!/usr/bin/env bash
# End-to-end smoke test for the whole playlist path: create it, fill it from
# the library as it already stands, order it, build it, play it, rebuild it
# without the link changing, watch a deleted clip drop out, delete it and get
# the space back. Twelve numbered steps, one pass/fail line each. Mirrors
# tools/smoke_blob.sh's scaffold.
#
# TWO CHECKS THIS SCRIPT CANNOT MAKE, named here rather than quietly skipped:
#
#   1. What the join between two clips looks and sounds like inside a world's
#      video player -- whether the next clip starts, whether the picture goes
#      black at the join, whether the sound walks away from the picture.
#   2. Whether that player repeats a finished list, and what it does when it
#      does.
#
# Neither is automatable: no automated environment has a VRChat client, and
# there is no way to record playback from inside one. Both were answered by a
# person watching, on PC, and what they saw is written down in
# docs/experiments/04-avpro-discontinuity.md.
#
# Everything below is driven from inside the compose network rather than from
# a shell on the host. That is the general form -- it works wherever the stack
# runs, including where the edge's published port is not reachable from the
# host it runs on.
#
# It resets the operator's password, the way tools/smoke_auth.sh does and for
# the same reason: a session is the only way in, and there is no other way to
# obtain one. It prints the credential it set.
#
# It leaves the library exactly as it found it: it adds no clip, deletes no
# clip, and purges the one playlist it creates.
#
# Usage: cd src && bash tools/smoke_playlist.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SRC_ROOT"

DD_BIN="/c/Users/misha/AppData/Local/Programs/DockerDesktop/resources/bin"
[ -d "$DD_BIN" ] && export PATH="$DD_BIN:$PATH"
# MSYS_NO_PATHCONV is scoped to the docker wrappers only, NOT exported
# globally -- see tools/smoke_blob.sh's own comment on why.
dk() { MSYS_NO_PATHCONV=1 docker "$@"; }
dc() { MSYS_NO_PATHCONV=1 docker compose "$@"; }

FAIL_COUNT=0
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
info() { echo "[....] $*" >&2; }

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found in PATH" >&2; exit 1; }
dk info >/dev/null 2>&1 || { echo "ERROR: docker daemon unreachable (docker info failed) — this is an environment failure, not a smoke-test result." >&2; exit 1; }
[ -f .env ] || { echo "ERROR: src/.env missing — copy .env.example and fill in real values first." >&2; exit 1; }

set -a
source .env
set +a

: "${MYTUBE_ADMIN_FQDN:?MYTUBE_ADMIN_FQDN is not set in .env — the interface has no address to answer on}"

ADMIN_HOST="$MYTUBE_ADMIN_FQDN"
BASE="http://127.0.0.1"

api_exec() { dc exec -T api "$@"; }
edge_exec() { dc exec -T edge "$@"; }

# Every request runs inside the edge container, against the edge's own
# listener. A request carrying no admin hostname lands on the default server,
# which is the delivery half -- that is how the public addresses below are
# reached, and it is deliberate that they need no header to be reached.
pub_curl() { edge_exec curl -s "$@"; }
adm_curl() { edge_exec curl -s -H "Host: ${ADMIN_HOST}" "$@"; }
# A browser attaches Origin to every same-site form submission and the
# interface refuses a write without it; curl does not, unless told to.
adm_post() {
  edge_exec curl -s -H "Host: ${ADMIN_HOST}" -H "Origin: https://${ADMIN_HOST}" -X POST "$@"
}

STAMP=$(date +%s)
SLUG="smoke-playlist-${STAMP}"
NAME="Smoke Playlist ${STAMP}"
RUN_USERNAME="smoke-op-${STAMP}"
RUN_PASSWORD="smoke-password-${STAMP}-not-a-real-secret"

# What the address plays, as one number: every clip downloaded in play order
# and run through one digest.
#
# Deliberately the bytes rather than the document that lists them. The
# document is the wrong thing to measure twice over -- it carries a fresh
# signature per clip on every request, so two fetches of an unchanged list
# read as different; and a rebuild mints new keys for the clips it produces,
# so re-assembling the very same list reads as different too. Either would
# make the check below pass on a rebuild that changed nothing, which is the
# one failure it exists to catch. This was measured, not reasoned about: a
# document digest passed a deliberately unchanged rebuild during this
# script's own bring-up.
plays_fingerprint() {
  api_exec python3 -c "
import urllib.request, hashlib, sys

document = urllib.request.urlopen('http://edge/p/${SLUG}', timeout=15).read().decode()
addresses = [line.replace('http://127.0.0.1/', 'http://edge/')
             for line in document.splitlines() if line.startswith('http')]
if not addresses:
    sys.exit(1)

digest = hashlib.sha256()
total = 0
for address in addresses:
    clip = urllib.request.urlopen(address, timeout=120).read()
    digest.update(clip)
    total += len(clip)
print(digest.hexdigest()[:12], total, 'bytes')
" 2>/dev/null | tail -1
}
segment_keys() { grep -o '/blob/[^?]*' | sed 's#/blob/##'; }
segment_count() { grep -c '^http' || true; }

# The count of objects in the bucket that nothing in the catalog references.
# Read as a delta against whatever it was before this run, never against
# zero: a bucket may already carry unreferenced objects from earlier work,
# and this script is answerable for its own leavings, not for those.
audit_unreferenced() {
  api_exec python -m orchestrator.cli storage-audit 2>/dev/null \
    | grep -cE '^  [^ ]+  [0-9]+ bytes$'
}

# One library row, whole: from the element that opens it to the one that
# closes it. A window anchored on the row's address and running forward
# misses the chip, which renders above it.
row_for() {
  awk -v needle="$1" '
    /<li class="library-row"/ { block = ""; found = 0 }
    { block = block $0 ORS }
    index($0, needle) { found = 1 }
    /<\/li>/ { if (found) { printf "%s", block; exit } }
  '
}

# A row as a person reads it, with the markup taken out -- what to print when
# a step has to report what the interface actually said.
row_text() { sed 's/<[^>]*>//g' | tr -s '[:space:]' ' '; }

wait_edge_healthy() {
  local id
  id=$(dc ps -q edge)
  for _ in $(seq 1 30); do
    STATUS=$(dk inspect "$id" --format '{{.State.Health.Status}}' 2>/dev/null)
    [ "$STATUS" = "healthy" ] && { sleep 1; return 0; }
    sleep 2
  done
  return 1
}

# ---------------------------------------------------------------------------
# Step 1: the stack is up and healthy, with the edge recreated rather than
# only started. Its configuration is rendered at container start, so a plain
# up after a change under edge/ leaves the previous render in place and the
# whole run measures the old configuration.
# ---------------------------------------------------------------------------
info "step 1: bring the stack up, recreate the edge"
if dc up -d --wait >/dev/null 2>&1 && dc up -d --force-recreate edge >/dev/null 2>&1 && wait_edge_healthy; then
  pass "step 1: stack is up, edge recreated and healthy"
else
  fail "step 1: the stack did not converge"
  dc ps
  exit 1
fi

BASELINE_UNREFERENCED=$(audit_unreferenced)
info "unreferenced objects already in the bucket before this run: ${BASELINE_UNREFERENCED}"

# ---------------------------------------------------------------------------
# Step 2: sign in and keep the session.
# ---------------------------------------------------------------------------
info "step 2: sign in"
CREATE_OUT=$(api_exec sh -c "echo '${RUN_PASSWORD}
${RUN_PASSWORD}' | python -m orchestrator.cli create-operator --username '${RUN_USERNAME}'" 2>&1)
if [ $? -eq 0 ]; then
  LOGIN_USERNAME="$RUN_USERNAME"
else
  # An account already exists. Its username is not knowable here, so this
  # resets without naming one: the command resolves the single row itself
  # and prints back which account it changed.
  RESET_OUT=$(api_exec sh -c "echo '${RUN_PASSWORD}
${RUN_PASSWORD}' | python -m orchestrator.cli reset-password" 2>&1)
  LOGIN_USERNAME=$(echo "$RESET_OUT" | sed -n "s/.*Password for '\([^']*\)'.*/\1/p")
fi

if [ -z "$LOGIN_USERNAME" ]; then
  fail "step 2: could not obtain an operator credential to sign in with"
  exit 1
fi

LOGIN_HDRS=$(adm_post -o /dev/null -D - -d "username=${LOGIN_USERNAME}&password=${RUN_PASSWORD}" "${BASE}/login")
COOKIE=$(echo "$LOGIN_HDRS" | grep -i '^set-cookie:' | sed -E 's/^[Ss]et-[Cc]ookie: *([^;]+);.*/\1/' | tr -d '\r')
if [ -n "$COOKIE" ]; then
  pass "step 2: signed in as ${LOGIN_USERNAME} (its password was set by this run)"
else
  fail "step 2: no session cookie came back for ${LOGIN_USERNAME}"
  exit 1
fi

adm_get() { adm_curl -b "$COOKIE" "$@"; }
adm_write() { adm_post -b "$COOKIE" "$@"; }

# ---------------------------------------------------------------------------
# Step 3: create a playlist, and find its row in the library carrying the
# chip that says it holds nothing yet.
# ---------------------------------------------------------------------------
info "step 3: create the playlist and find its library row"
adm_write -o /dev/null --data-urlencode "name=${NAME}" -d "slug=${SLUG}" "${BASE}/playlists" >/dev/null
LIBRARY=$(adm_get "${BASE}/library")
ROW=$(echo "$LIBRARY" | row_for "/p/${SLUG}")
PLAYLIST_ID=$(echo "$ROW" | grep -o '/playlists/[0-9a-f-]\{36\}/edit' | head -1 | sed -E 's#/playlists/(.*)/edit#\1#')
if [ -n "$PLAYLIST_ID" ] && echo "$ROW" | grep -q '>Empty<'; then
  pass "step 3: created, its row carries the empty chip (${PLAYLIST_ID})"
else
  fail "step 3: no library row with an empty chip for /p/${SLUG} (id='${PLAYLIST_ID}')"
  exit 1
fi

EDIT_URL="${BASE}/playlists/${PLAYLIST_ID}/edit"
ITEMS_URL="${BASE}/playlists/${PLAYLIST_ID}/items"
PUBLIC_URL="${BASE}/p/${SLUG}"

# ---------------------------------------------------------------------------
# Step 4: the address answers before there is anything to play. A member of
# the audience gets a clip that tells them so, not an error page, and it is
# the very file this repository ships for the purpose.
# ---------------------------------------------------------------------------
info "step 4: the address before the playlist has members"
NOT_READY_HDRS=$(pub_curl -o /dev/null -D - "$PUBLIC_URL")
NOT_READY_CODE=$(echo "$NOT_READY_HDRS" | head -1 | awk '{print $2}')
NOT_READY_TYPE=$(echo "$NOT_READY_HDRS" | grep -i '^content-type:' | tr -d '\r')
DIGESTS=$(edge_exec sh -c "curl -s -o /tmp/smoke_answer.bin '${PUBLIC_URL}' && sha256sum /tmp/smoke_answer.bin /etc/nginx/errors/playlist-not-ready.mp4 | cut -d' ' -f1")
SERVED_DIGEST=$(echo "$DIGESTS" | head -1)
STUB_DIGEST=$(echo "$DIGESTS" | tail -1)
if [ "$NOT_READY_CODE" = "200" ] && [[ "$NOT_READY_TYPE" == *"video/mp4"* ]] \
  && [ -n "$SERVED_DIGEST" ] && [ "$SERVED_DIGEST" = "$STUB_DIGEST" ]; then
  pass "step 4: 200 video/mp4, byte-identical to the clip that says it is not ready"
else
  fail "step 4: expected the not-ready clip, got code=${NOT_READY_CODE} '${NOT_READY_TYPE}' served=${SERVED_DIGEST} stub=${STUB_DIGEST}"
fi

# ---------------------------------------------------------------------------
# Step 5: fill it from the library as it already stands.
#
# Deliberately from what is already here, and nothing is added for the
# occasion. This is the step that catches a feature which only works for
# material added after the change that introduced it: a clip added before the
# catalog recorded the parameters the build reads has to have been measured
# since, and if it was not, step 7 refuses this playlist and the whole point
# of it is false on the one stack it ships to.
# ---------------------------------------------------------------------------
info "step 5: add two clips from the library"
FORM=$(adm_get "$EDIT_URL")
EDITIONS=$(echo "$FORM" | grep -o 'name="edition_id" value="[0-9a-f-]\{36\}"' | sed -E 's/.*value="(.*)"/\1/')
EDITION_COUNT=$(echo "$EDITIONS" | grep -c .)
FIRST_EDITION=$(echo "$EDITIONS" | sed -n 1p)
SECOND_EDITION=$(echo "$EDITIONS" | sed -n 2p)
if [ "$EDITION_COUNT" -lt 2 ]; then
  fail "step 5: the library holds ${EDITION_COUNT} clip(s); this check needs two that are already here and adds none"
  exit 1
fi

adm_write -o /dev/null -d "edition_id=${FIRST_EDITION}" "$ITEMS_URL" >/dev/null
adm_write -o /dev/null -d "edition_id=${SECOND_EDITION}" "$ITEMS_URL" >/dev/null
MEMBERS=$(adm_get "$ITEMS_URL")
MEMBER_COUNT=$(echo "$MEMBERS" | grep -c 'data-item-id=')
if [ "$MEMBER_COUNT" -eq 2 ]; then
  pass "step 5: two clips from the standing library are in the list"
else
  fail "step 5: expected two members, got ${MEMBER_COUNT}"
  exit 1
fi

item_ids() { grep -o 'data-item-id="[0-9a-f-]\{36\}"' | sed -E 's/.*"(.*)"/\1/' | tr '\n' ' '; }

# ---------------------------------------------------------------------------
# Step 6: reorder, and confirm the new order is what the server holds rather
# than what one response happened to render.
# ---------------------------------------------------------------------------
info "step 6: reorder"
ORDER_BEFORE=$(echo "$MEMBERS" | item_ids)
FIRST_ITEM=$(echo "$ORDER_BEFORE" | awk '{print $1}')
adm_write -o /dev/null -d "direction=down" "${ITEMS_URL}/${FIRST_ITEM}/move" >/dev/null
ORDER_AFTER=$(adm_get "$ITEMS_URL" | item_ids)
ORDER_REFETCHED=$(adm_get "$ITEMS_URL" | item_ids)
if [ "$ORDER_AFTER" != "$ORDER_BEFORE" ] && [ "$ORDER_REFETCHED" = "$ORDER_AFTER" ]; then
  pass "step 6: the order changed and held on a re-read"
else
  fail "step 6: before='${ORDER_BEFORE}' after='${ORDER_AFTER}' refetched='${ORDER_REFETCHED}'"
fi

# ---------------------------------------------------------------------------
# Step 7: save, and wait for it to report itself built. A refusal is the
# result of this step, not a reason to keep waiting: it prints why and stops.
# ---------------------------------------------------------------------------
save_and_wait() {
  local expected_segments="$1"
  local deadline=$((SECONDS + 300))
  adm_write -o /dev/null --data-urlencode "name=${NAME}" -d "slug=${SLUG}" "${BASE}/playlists/${PLAYLIST_ID}" >/dev/null
  while [ "$SECONDS" -lt "$deadline" ]; do
    local row
    row=$(adm_get "${BASE}/library" | row_for "/p/${SLUG}")
    if echo "$row" | grep -q 'chip--refused'; then
      echo "REFUSED: $(echo "$row" | row_text)"
      return 1
    fi
    # The content type first, and only then the body. While the build is
    # still running the answer is a video file, and reading those bytes into
    # a shell variable is a warning per poll for nothing.
    local answer_type body
    answer_type=$(pub_curl -o /dev/null -w '%{content_type}' "$PUBLIC_URL")
    if [[ "$answer_type" == *"mpegurl"* ]]; then
      body=$(pub_curl "$PUBLIC_URL")
      if [ "$(echo "$body" | segment_count)" -eq "$expected_segments" ]; then
        return 0
      fi
    fi
    sleep 3
  done
  echo "TIMED OUT waiting for ${expected_segments} clip(s) to finish assembling"
  return 1
}

info "step 7: save and wait for the build"
BUILD_REPORT=$(save_and_wait 2)
if [ $? -eq 0 ]; then
  pass "step 7: built"
else
  fail "step 7: ${BUILD_REPORT}"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 8: the address plays the assembled result. Not "answers 200" -- the
# bytes behind it are range-requestable and a decoder can actually open them,
# which is the distinction this project learned the hard way once already
# (see tools/check_decodable.sh's own header).
# ---------------------------------------------------------------------------
info "step 8: the address plays"
DOCUMENT=$(pub_curl "$PUBLIC_URL")
DOC_JOINS=$(echo "$DOCUMENT" | grep -c 'EXT-X-DISCONTINUITY')
PLAYS_RAW=$(api_exec python3 -c "
import urllib.request, subprocess, tempfile, os, sys

document = urllib.request.urlopen('http://edge/p/${SLUG}', timeout=15).read().decode()
addresses = [line for line in document.splitlines() if line.startswith('http')]
if not addresses:
    print('no clip address in the document'); sys.exit(1)
address = addresses[0].replace('http://127.0.0.1/', 'http://edge/')

partial = urllib.request.urlopen(
    urllib.request.Request(address, headers={'Range': 'bytes=0-65535'}), timeout=30)
range_status = partial.status
content_range = partial.headers.get('Content-Range') or ''
if range_status != 206 or not content_range:
    print('a ranged request came back', range_status, content_range); sys.exit(1)

whole = urllib.request.urlopen(address, timeout=120).read()
path = os.path.join(tempfile.gettempdir(), 'smoke_clip.bin')
open(path, 'wb').write(whole)
probe = subprocess.run(
    ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
     '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', path],
    capture_output=True, text=True)
codec = (probe.stdout.split() or [''])[0]
if probe.returncode != 0 or not codec:
    print('the bytes behind the address do not decode:', probe.stderr.strip()); sys.exit(1)
print('206 with', content_range, 'and the whole clip decodes as', codec)
" 2>&1)
PLAYS_OK=$?
PLAYS=$(echo "$PLAYS_RAW" | tail -1)
if [ "$PLAYS_OK" -eq 0 ] && [[ "$DOCUMENT" == *"#EXT-X-ENDLIST"* ]] && [ "$DOC_JOINS" -eq 1 ]; then
  pass "step 8: two clips with one join between them; ${PLAYS}"
else
  fail "step 8: joins=${DOC_JOINS} report='${PLAYS}'"
fi

# ---------------------------------------------------------------------------
# Step 9: the link survives a rebuild, and what it plays does not.
#
# Both halves matter and the second is the one worth writing this script for.
# An unchanged address proves nothing on its own: a rebuild that silently did
# nothing leaves the address exactly as unchanged as a rebuild that worked.
# ---------------------------------------------------------------------------
info "step 9: rebuild, same link, different content"
ADDRESS_BEFORE=$(echo "$LIBRARY" | grep -o "https\?://[^\"]*/p/${SLUG}" | head -1)
FINGERPRINT_BEFORE=$(plays_fingerprint)
FINGERPRINT_AGAIN=$(plays_fingerprint)

adm_write -o /dev/null -d "edition_id=${FIRST_EDITION}" "$ITEMS_URL" >/dev/null
REBUILD_REPORT=$(save_and_wait 3)
REBUILD_OK=$?
DOCUMENT_AFTER=$(pub_curl "$PUBLIC_URL")
FINGERPRINT_AFTER=$(plays_fingerprint)
ADDRESS_AFTER=$(adm_get "${BASE}/library" | grep -o "https\?://[^\"]*/p/${SLUG}" | head -1)

if [ "$FINGERPRINT_AGAIN" != "$FINGERPRINT_BEFORE" ]; then
  fail "step 9: what the address plays reads as different between two requests that changed nothing — the fingerprint is measuring the signatures, not the content, and the rest of this step would pass on anything"
elif [ "$REBUILD_OK" -ne 0 ]; then
  fail "step 9: ${REBUILD_REPORT}"
elif [ -z "$ADDRESS_BEFORE" ] || [ "$ADDRESS_AFTER" != "$ADDRESS_BEFORE" ]; then
  fail "step 9: the address changed across a rebuild: '${ADDRESS_BEFORE}' became '${ADDRESS_AFTER}' — a link already pasted into a world cannot be corrected from inside it"
elif [ "$FINGERPRINT_AFTER" = "$FINGERPRINT_BEFORE" ]; then
  fail "step 9: the address is unchanged but so is what it plays (${FINGERPRINT_AFTER}) — the rebuild did not take"
else
  pass "step 9: ${ADDRESS_AFTER} unchanged, and what it plays went from ${FINGERPRINT_BEFORE} to ${FINGERPRINT_AFTER}"
fi

# ---------------------------------------------------------------------------
# Step 10: a clip deleted from the library drops out of the list on its own,
# and the rest keep playing. Nothing is rebuilt and nothing is marked for the
# operator to go and fix, which is what the recorded answer says should
# happen.
#
# This drives the state the delivery path reads, and puts it back afterwards,
# rather than running the operator's own delete gesture: that gesture destroys
# a clip out of the library for good, and this script has to leave the library
# exactly as it found it and has to be runnable twice in a row.
# ---------------------------------------------------------------------------
info "step 10: a deleted clip drops out"
set_title_status() {
  api_exec python3 -c "
import asyncio, asyncpg, sys
from orchestrator.config import get_settings

async def main():
    conn = await asyncpg.connect(get_settings().database_url)
    title_id = await conn.fetchval('''
        SELECT s.title_id
        FROM playlist_items pi
        JOIN editions e ON e.id = pi.edition_id
        JOIN episodes ep ON ep.id = e.episode_id
        JOIN seasons s ON s.id = ep.season_id
        WHERE pi.playlist_id = \$1::uuid
        ORDER BY pi.position
        LIMIT 1
    ''', '${PLAYLIST_ID}')
    await conn.execute('UPDATE titles SET status = \$2 WHERE id = \$1', title_id, '$1')
    await conn.close()
    print(title_id)

asyncio.run(main())
" 2>&1 | tail -1
}

SEGMENTS_BEFORE_DROP=$(echo "$DOCUMENT_AFTER" | segment_count)
DROPPED_TITLE=$(set_title_status deleted)
DOCUMENT_DROPPED=$(pub_curl "$PUBLIC_URL")
SEGMENTS_DROPPED=$(echo "$DOCUMENT_DROPPED" | segment_count)
STILL_PLAYS=$(pub_curl -o /dev/null -w '%{http_code}' -r 0-1023 "$(echo "$DOCUMENT_DROPPED" | grep '^http' | head -1)")
RESTORED_TITLE=$(set_title_status active)
SEGMENTS_RESTORED=$(pub_curl "$PUBLIC_URL" | segment_count)

if [ "$SEGMENTS_DROPPED" -lt "$SEGMENTS_BEFORE_DROP" ] \
  && [ "$SEGMENTS_DROPPED" -gt 0 ] \
  && [ "$STILL_PLAYS" = "206" ] \
  && [ "$SEGMENTS_RESTORED" -eq "$SEGMENTS_BEFORE_DROP" ]; then
  pass "step 10: the list went from ${SEGMENTS_BEFORE_DROP} clips to ${SEGMENTS_DROPPED} with nothing rebuilt, the rest still play, and the library is back as it was"
else
  fail "step 10: before=${SEGMENTS_BEFORE_DROP} dropped=${SEGMENTS_DROPPED} rest-plays=${STILL_PLAYS} restored=${SEGMENTS_RESTORED} (title ${DROPPED_TITLE}/${RESTORED_TITLE})"
fi

# ---------------------------------------------------------------------------
# Step 11: nothing this run built is sitting in the bucket unaccounted for.
# ---------------------------------------------------------------------------
info "step 11: the audit"
BUILT_UNREFERENCED=$(audit_unreferenced)
if [ "$BUILT_UNREFERENCED" -eq "$BASELINE_UNREFERENCED" ]; then
  pass "step 11: everything this run put in the bucket is accounted for (${BUILT_UNREFERENCED} unreferenced, unchanged)"
else
  fail "step 11: unreferenced objects went from ${BASELINE_UNREFERENCED} to ${BUILT_UNREFERENCED} — this run left something behind"
fi

# ---------------------------------------------------------------------------
# Step 12: delete it now, and get every byte of it back.
# ---------------------------------------------------------------------------
info "step 12: delete now, and reclaim"
BUILT_KEYS=$(pub_curl "$PUBLIC_URL" | segment_keys)
adm_write -o /dev/null "${BASE}/playlists/${PLAYLIST_ID}/delete" >/dev/null
adm_write -o /dev/null "${BASE}/playlists/${PLAYLIST_ID}/purge" >/dev/null

LEFTOVERS=$(api_exec python3 -c "
import sys
from orchestrator.storage import list_all_objects
held = set(list_all_objects())
wanted_gone = [k for k in '''${BUILT_KEYS}'''.split() if k]
if not wanted_gone:
    print('no keys to check'); sys.exit(1)
print(' '.join(sorted(held & set(wanted_gone))) or 'none')
" 2>&1 | tail -1)

GONE_HDRS=$(pub_curl -o /dev/null -D - "$PUBLIC_URL")
GONE_CODE=$(echo "$GONE_HDRS" | head -1 | awk '{print $2}')
GONE_TYPE=$(echo "$GONE_HDRS" | grep -i '^content-type:' | tr -d '\r')
GONE_DIGESTS=$(edge_exec sh -c "curl -s -o /tmp/smoke_gone.bin '${PUBLIC_URL}' && sha256sum /tmp/smoke_gone.bin /etc/nginx/errors/not-found.mp4 | cut -d' ' -f1")
GONE_SERVED=$(echo "$GONE_DIGESTS" | head -1)
GONE_STUB=$(echo "$GONE_DIGESTS" | tail -1)
FINAL_UNREFERENCED=$(audit_unreferenced)
BROKEN=$(api_exec python -m orchestrator.cli storage-audit 2>/dev/null | grep -c 'no broken addresses')

if [ "$LEFTOVERS" = "none" ] \
  && [ "$GONE_CODE" = "200" ] && [[ "$GONE_TYPE" == *"video/mp4"* ]] \
  && [ "$GONE_SERVED" = "$GONE_STUB" ] \
  && [ "$FINAL_UNREFERENCED" -eq "$BASELINE_UNREFERENCED" ] \
  && [ "$BROKEN" -eq 1 ]; then
  pass "step 12: every clip it assembled is gone from the bucket, the audit is back where it started, and the address answers the clip that says there is nothing there"
else
  fail "step 12: still in the bucket='${LEFTOVERS}' answer=${GONE_CODE} '${GONE_TYPE}' digest-match=$([ "$GONE_SERVED" = "$GONE_STUB" ] && echo yes || echo no) unreferenced=${FINAL_UNREFERENCED} (was ${BASELINE_UNREFERENCED}) broken-addresses-clean=${BROKEN}"
fi

echo ""
if [ "$FAIL_COUNT" -eq 0 ]; then
  echo "===== SMOKE TEST: ALL STEPS PASSED ====="
  exit 0
else
  echo "===== SMOKE TEST: $FAIL_COUNT STEP(S) FAILED ====="
  exit 1
fi
