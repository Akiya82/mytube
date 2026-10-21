#!/usr/bin/env bash
# Experiment 7: Garage vs SeaweedFS through the same nginx-with-slice setup.
#
# Runs both object-storage candidates through the same scenario: bring-up ->
# anonymous read from inside the network -> Range with and without a
# signature -> download through nginx slice + sha256 -> access_log format ->
# TTFB -> CreateBucket through the S3 API. Prints a comparison table and
# picks a candidate.
#
# Every object this stand creates uses a fixed name, torn down at the end
# through `trap ... EXIT` regardless of outcome. The owner's real MinIO is
# never addressed at all.
#
# Requires Docker (network mytube-exp07, temporary containers/volumes).
# Installs no Python package on the host: all S3 access goes through the
# amazon/aws-cli container.

set -euo pipefail

# Windows Git Bash reinterprets arguments shaped like "/garage" as a Windows
# path before passing them to docker.exe (a non-MSYS program). Disable that
# for the whole script: the only program invoked this way here is docker.
export MSYS_NO_PATHCONV=1

# ---------------------------------------------------------------------------
# Fixed names for this stand: only these are created and torn down.
# ---------------------------------------------------------------------------
NETWORK=mytube-exp07
C_GARAGE=exp07-garage
C_SEAWEED=exp07-seaweedfs
C_NGINX=exp07-nginx
V_META=exp07-meta
V_DATA=exp07-data

IMG_GARAGE=dxflrs/garage:v2.3.0
IMG_SEAWEED=chrislusf/seaweedfs:3.97
IMG_AWSCLI=amazon/aws-cli:2.27.50
IMG_NGINX=nginx:1.27-alpine
IMG_CURL=curlimages/curl:8.11.0
IMG_ALPINE=alpine:3.20

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKDIR="$SRC_ROOT/build/exp07"

REGION=mytube
BUCKET=mytube
OBJECT=probe.bin
OBJ_SIZE_MB=64
OBJ_SIZE_BYTES=$((OBJ_SIZE_MB * 1024 * 1024))
SLICE_COUNT=$((OBJ_SIZE_BYTES / 1048576))

SW_KEY=exp07key
SW_SECRET=exp07secretexp07secret

log() { echo "[exp07] $*" >&2; }

# ---------------------------------------------------------------------------
# Preflight: docker on PATH and a live daemon. A dead daemon is a stand
# error, not a "candidate failed" measurement.
# ---------------------------------------------------------------------------
preflight() {
  if ! command -v docker >/dev/null 2>&1; then
    local dd_bin="/c/Users/misha/AppData/Local/Programs/DockerDesktop/resources/bin"
    if [ -x "$dd_bin/docker" ]; then
      export PATH="$dd_bin:$PATH"
      log "docker was not on PATH, added $dd_bin"
    fi
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found on PATH. Install Docker Desktop or add its bin directory to PATH." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon unreachable (docker info failed). Start Docker Desktop and retry." >&2
    echo "This is a stand environment error, not a candidate comparison result." >&2
    exit 1
  fi
  for c in openssl sha256sum curl; do
    command -v "$c" >/dev/null 2>&1 || { echo "ERROR: $c not found on PATH" >&2; exit 1; }
  done
}

# ---------------------------------------------------------------------------
# Cleanup: stops and removes exactly the objects named above, by name.
# ---------------------------------------------------------------------------
cleanup() {
  local ec=$?
  set +e
  docker rm -f "$C_NGINX" "$C_GARAGE" "$C_SEAWEED" >/dev/null 2>&1
  docker volume rm "$V_META" "$V_DATA" >/dev/null 2>&1
  docker network rm "$NETWORK" >/dev/null 2>&1
  rm -rf "$WORKDIR"
  exit $ec
}

setup_workdir() {
  rm -rf "$WORKDIR"
  mkdir -p "$WORKDIR/garage" "$WORKDIR/seaweed" "$WORKDIR/aws-config" "$WORKDIR/nginx"
  cat > "$WORKDIR/aws-config/config" <<'CFG'
[default]
s3 =
    addressing_style = path
CFG
  cat > "$WORKDIR/policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::${BUCKET}/*"}
  ]
}
EOF
  head -c "$OBJ_SIZE_BYTES" /dev/urandom > "$WORKDIR/$OBJECT"
  SHA_ORIG=$(sha256sum "$WORKDIR/$OBJECT" | awk '{print $1}')
  log "test object $OBJECT: ${OBJ_SIZE_MB} MiB, sha256=$SHA_ORIG"
}

reset_volumes() {
  docker rm -f "$C_GARAGE" "$C_SEAWEED" >/dev/null 2>&1 || true
  docker volume rm "$V_META" "$V_DATA" >/dev/null 2>&1 || true
  docker volume create "$V_META" >/dev/null
  docker volume create "$V_DATA" >/dev/null
}

# aws_signed <endpoint> <key> <secret> <args...> -- S3 access through the aws-cli container
aws_signed() {
  local endpoint=$1 key=$2 secret=$3; shift 3
  docker run --rm --network "$NETWORK" \
    -e AWS_ACCESS_KEY_ID="$key" -e AWS_SECRET_ACCESS_KEY="$secret" -e AWS_DEFAULT_REGION="$REGION" \
    -v "$WORKDIR/aws-config/config:/root/.aws/config:ro" \
    -v "$WORKDIR:/data" \
    "$IMG_AWSCLI" --endpoint-url "$endpoint" "$@"
}

# aws_anon <endpoint> <args...> -- unsigned S3 call
aws_anon() {
  local endpoint=$1; shift 1
  docker run --rm --network "$NETWORK" \
    -v "$WORKDIR/aws-config/config:/root/.aws/config:ro" \
    -v "$WORKDIR:/data" \
    "$IMG_AWSCLI" --endpoint-url "$endpoint" --no-sign-request "$@"
}

median3() { printf '%s\n%s\n%s\n' "$1" "$2" "$3" | sort -n | sed -n '2p'; }

# ---------------------------------------------------------------------------
# Garage bring-up: layout assign/apply is required before bucket create
# (otherwise bucket create fails); anonymous read is attempted via
# PutBucketPolicy, then via the ordinary garage bucket subcommands this
# tag's own --help shows.
# ---------------------------------------------------------------------------
bring_up_garage() {
  local rpc_secret garage_toml node_line node_id key_out
  rpc_secret=$(openssl rand -hex 32)
  garage_toml="$WORKDIR/garage/garage.toml"
  cat > "$garage_toml" <<EOF
metadata_dir       = "/var/lib/garage/meta"
data_dir           = "/var/lib/garage/data"
db_engine          = "sqlite"
replication_factor = 1
rpc_bind_addr      = "[::]:3901"
rpc_public_addr    = "127.0.0.1:3901"
rpc_secret         = "$rpc_secret"

[s3_api]
s3_region     = "$REGION"
api_bind_addr = "[::]:3900"
root_domain   = ".s3.garage"
EOF

  docker run -d --name "$C_GARAGE" --network "$NETWORK" \
    -v "$garage_toml:/etc/garage.toml:ro" \
    -v "$V_META:/var/lib/garage/meta" \
    -v "$V_DATA:/var/lib/garage/data" \
    "$IMG_GARAGE" /garage server >/dev/null

  log "Garage: waiting for readiness (garage status)..."
  local ok=0
  for _ in $(seq 1 30); do
    if docker exec "$C_GARAGE" /garage status >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  [ "$ok" = 1 ] || { echo "ERROR: Garage did not come up within 60s" >&2; return 1; }

  node_line=$(docker exec "$C_GARAGE" /garage node id -q)
  node_id=${node_line%%@*}
  docker exec "$C_GARAGE" /garage layout assign -z dc1 -c 1G "$node_id" >/dev/null
  docker exec "$C_GARAGE" /garage layout apply --version 1 >/dev/null
  docker exec "$C_GARAGE" /garage bucket create "$BUCKET" >/dev/null

  key_out=$(docker exec "$C_GARAGE" /garage key create exp07-key)
  GARAGE_KEY_ID=$(echo "$key_out" | grep 'Key ID:' | awk '{print $NF}')
  GARAGE_SECRET=$(echo "$key_out" | grep 'Secret key:' | awk '{print $NF}')
  docker exec "$C_GARAGE" /garage bucket allow --read --write --owner "$BUCKET" --key exp07-key >/dev/null
  # Without this, CreateBucket through the S3 API returns AccessDenied
  # ("this key is not allowed to create buckets") -- that permission is off
  # by default and has to be granted separately.
  docker exec "$C_GARAGE" /garage key allow --create-bucket exp07-key >/dev/null

  CANDIDATE_ENDPOINT="http://$C_GARAGE:3900"
  CANDIDATE_KEY="$GARAGE_KEY_ID"
  CANDIDATE_SECRET="$GARAGE_SECRET"
  CANDIDATE_CONTAINER="$C_GARAGE"
  CANDIDATE_PORT=3900

  # Step 3: anonymous read from inside the network. Order:
  # PutBucketPolicy -> the ordinary garage bucket subcommands shown by --help.
  set +e
  local policy_out
  policy_out=$(aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api put-bucket-policy --bucket "$BUCKET" --policy file:///data/policy.json 2>&1)
  set -e
  log "Garage PutBucketPolicy: $policy_out"
  # `garage bucket --help`/`garage bucket website --help` on tag v2.3.0 have
  # no subcommand that grants an anonymous request s3:GetObject on the
  # bucket: `allow`/`deny` only work with named keys (not anonymous), and
  # `website` is a separate HTTP server with Host-based routing and
  # index-document semantics, not the GetObject S3 API path slice uses.
  # The actual outcome is recorded below from step 6's (curl) real result,
  # not from this assumption.
}

# ---------------------------------------------------------------------------
# SeaweedFS bring-up: identities.json with an "anonymous" identity
# (action Read) is the standard mechanism for anonymous read (see the
# SeaweedFS wiki).
# ---------------------------------------------------------------------------
bring_up_seaweed() {
  local s3json="$WORKDIR/seaweed/s3.json"
  cat > "$s3json" <<EOF
{"identities":[
  {"name":"exp07","credentials":[{"accessKey":"${SW_KEY}","secretKey":"${SW_SECRET}"}],
   "actions":["Admin","Read","Write","List","Tagging"]},
  {"name":"anonymous","actions":["Read"]}
]}
EOF

  docker run -d --name "$C_SEAWEED" --network "$NETWORK" \
    -v "$WORKDIR/seaweed:/etc/seaweedfs:ro" \
    -v "$V_DATA:/data" \
    "$IMG_SEAWEED" server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json -master.volumeSizeLimitMB=1024 >/dev/null

  CANDIDATE_ENDPOINT="http://$C_SEAWEED:8333"
  CANDIDATE_KEY="$SW_KEY"
  CANDIDATE_SECRET="$SW_SECRET"
  CANDIDATE_CONTAINER="$C_SEAWEED"
  CANDIDATE_PORT=8333

  log "SeaweedFS: waiting for the S3 port to become ready..."
  local ok=0
  for _ in $(seq 1 30); do
    if aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" s3api list-buckets >/dev/null 2>&1; then
      ok=1; break
    fi
    sleep 2
  done
  [ "$ok" = 1 ] || { echo "ERROR: SeaweedFS did not come up within 60s" >&2; return 1; }

  # The bucket is created the candidate's own ordinary way -- a signed
  # create-bucket call (anonymous CreateBucket is correctly refused: the
  # "anonymous" identity only has Read, no Write/Admin -- a valid
  # measurement of step 11 in its own right).
  aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api create-bucket --bucket "$BUCKET" >/dev/null
}

# ---------------------------------------------------------------------------
# The shared sequence for both candidates (steps 4-11 of the plan).
# Writes results into global variables RES_<candidate>_<metric>.
# ---------------------------------------------------------------------------
run_common_checks() {
  local name=$1
  local prefix="RES_${name}"

  # Step 4: the test object (already generated in setup_workdir)
  aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api put-object --bucket "$BUCKET" --key "$OBJECT" --body "/data/$OBJECT" >/dev/null

  # Step 5: unreachable from outside
  local ip host_curl_code bridge_dns
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CANDIDATE_CONTAINER")
  set +e
  curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$ip:$CANDIDATE_PORT/" >"$WORKDIR/host_curl.out" 2>/dev/null
  host_curl_exit=$?
  set -e
  host_curl_code=$(cat "$WORKDIR/host_curl.out" 2>/dev/null || echo "000")
  if [ "$host_curl_exit" != 0 ]; then
    printf -v "${prefix}_UNREACHABLE" '%s' "PASS (curl from the host: exit=$host_curl_exit, connection was not established)"
  else
    printf -v "${prefix}_UNREACHABLE" '%s' "FAIL (curl from the host got HTTP $host_curl_code -- the port is reachable)"
  fi

  set +e
  bridge_dns=$(docker run --rm --network bridge "$IMG_ALPINE" sh -c "getent hosts $CANDIDATE_CONTAINER" 2>&1)
  bridge_dns_exit=$?
  set -e
  if [ "$bridge_dns_exit" != 0 ]; then
    log "$name: name $CANDIDATE_CONTAINER does not resolve from the bridge network (expected)"
  else
    log "$name: WARNING name $CANDIDATE_CONTAINER DOES RESOLVE from the bridge network: $bridge_dns"
  fi

  # Step 6: anonymous unsigned Range-GET from inside the network -- the
  # mandatory criterion. Two checks: mid-file and a tail range.
  local anon1 anon2
  set +e
  anon1=$(docker run --rm --network "$NETWORK" "$IMG_CURL" -sD- -o /dev/null -r 0-1048575 \
    "$CANDIDATE_ENDPOINT/$BUCKET/$OBJECT" 2>&1)
  anon1_code=$(echo "$anon1" | head -1 | awk '{print $2}')
  local tail_start=$((OBJ_SIZE_BYTES - 1048576)) tail_end=$((OBJ_SIZE_BYTES - 1))
  anon2=$(docker run --rm --network "$NETWORK" "$IMG_CURL" -sD- -o /dev/null -r "${tail_start}-${tail_end}" \
    "$CANDIDATE_ENDPOINT/$BUCKET/$OBJECT" 2>&1)
  anon2_code=$(echo "$anon2" | head -1 | awk '{print $2}')
  set -e
  if [ "$anon1_code" = "206" ] && [ "$anon2_code" = "206" ]; then
    printf -v "${prefix}_ANON_RANGE" '%s' "PASS"
    ANON_RANGE_OK_FOR_NGINX_TEST=1
  else
    printf -v "${prefix}_ANON_RANGE" '%s' "FAIL (the 0- range got HTTP $anon1_code, the tail range got HTTP $anon2_code -- not 206)"
    ANON_RANGE_OK_FOR_NGINX_TEST=0
  fi

  # Step 7: a signed Range -- a control check (not about anonymity, about Range itself)
  set +e
  aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api get-object --bucket "$BUCKET" --key "$OBJECT" --range bytes=0-1048575 \
    /data/signed_range.bin >"$WORKDIR/signed_range.json" 2>&1
  signed_range_exit=$?
  set -e
  if [ "$signed_range_exit" = 0 ] && grep -q 'ContentRange' "$WORKDIR/signed_range.json"; then
    printf -v "${prefix}_SIGNED_RANGE" '%s' "PASS"
  else
    sr_detail=$(tr -d '\n' < "$WORKDIR/signed_range.json" | head -c 200)
    printf -v "${prefix}_SIGNED_RANGE" '%s' "FAIL ($sr_detail)"
  fi

  # Step 8: download through nginx with slice, verify sha256
  cat > "$WORKDIR/nginx/nginx-${name}.conf" <<EOF
worker_processes 1;
events { worker_connections 1024; }
http {
    log_subrequest on;
    log_format cache_audit '\$time_iso8601 \$uri \$slice_range \$upstream_cache_status \$status \$body_bytes_sent';
    access_log /var/log/nginx/blob_access.log cache_audit;
    proxy_cache_path /dev/shm/exp07vid levels=1:2 keys_zone=exp07vid:16m max_size=256m inactive=120s use_temp_path=off;

    upstream storage_upstream {
        server ${CANDIDATE_CONTAINER}:${CANDIDATE_PORT};
        keepalive 32;
    }

    server {
        listen 80;
        location /blob/ {
            slice 1m;
            proxy_set_header Range \$slice_range;
            proxy_cache exp07vid;
            proxy_cache_key "\$uri\$slice_range";
            proxy_cache_valid 200 206 1h;
            proxy_cache_lock on;
            proxy_cache_lock_timeout 10s;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_pass http://storage_upstream/${BUCKET}/;
            add_header X-Cache-Status \$upstream_cache_status;
        }
    }
}
EOF
  docker rm -f "$C_NGINX" >/dev/null 2>&1 || true
  docker run -d --name "$C_NGINX" --network "$NETWORK" --shm-size=200m \
    -v "$WORKDIR/nginx/nginx-${name}.conf:/etc/nginx/nginx.conf:ro" \
    "$IMG_NGINX" >/dev/null
  sleep 1

  local dl_code sha_dl cache_status_1 cache_status_2
  dl_code=$(docker run --rm --network "$NETWORK" -v "$WORKDIR:/out" "$IMG_CURL" \
    -s -D "/out/dl1_headers.txt" -o "/out/downloaded_${name}.bin" -w '%{http_code}' \
    "http://$C_NGINX/blob/$OBJECT")
  cache_status_1=$(grep -i '^X-Cache-Status:' "$WORKDIR/dl1_headers.txt" | tr -d '\r' | awk '{print $2}') || true
  if [ -f "$WORKDIR/downloaded_${name}.bin" ]; then
    sha_dl=$(sha256sum "$WORKDIR/downloaded_${name}.bin" | awk '{print $1}')
  else
    sha_dl="-"
  fi

  printf -v "${prefix}_DL_HTTP_CODE" '%s' "$dl_code"
  printf -v "${prefix}_CACHE_STATUS_1" '%s' "$cache_status_1"
  if [ "$dl_code" = "200" ] && [ "$sha_dl" = "$SHA_ORIG" ]; then
    printf -v "${prefix}_SHA_OK" '%s' "PASS"
  else
    printf -v "${prefix}_SHA_OK" '%s' "FAIL (HTTP $dl_code, downloaded sha256=$sha_dl, expected $SHA_ORIG)"
  fi

  # Step 9: the access_log format, captured live -- taken right after the
  # FIRST full download, before the second (cache-verification) request, so
  # the line count reflects exactly one pass over 64 MiB.
  local log_total log_sample
  log_total=$(docker exec "$C_NGINX" sh -c 'wc -l < /var/log/nginx/blob_access.log' 2>/dev/null || echo 0)
  log_sample=$(docker exec "$C_NGINX" sh -c 'head -3 /var/log/nginx/blob_access.log' 2>/dev/null || echo "")
  printf -v "${prefix}_LOG_LINES" '%s' "$log_total"
  printf '%s\n' "$log_sample" > "$WORKDIR/log_sample_${name}.txt"

  # A second pass (same address) -- confirms the second time is served from
  # the cache (X-Cache-Status). Taken after capturing the log format above,
  # so it does not double the line count from step 9.
  docker run --rm --network "$NETWORK" -v "$WORKDIR:/out" "$IMG_CURL" \
    -s -D "/out/dl2_headers.txt" -o "/out/downloaded2_${name}.bin" \
    "http://$C_NGINX/blob/$OBJECT" >/dev/null
  cache_status_2=$(grep -i '^X-Cache-Status:' "$WORKDIR/dl2_headers.txt" | tr -d '\r' | awk '{print $2}') || true
  printf -v "${prefix}_CACHE_STATUS_2" '%s' "$cache_status_2"

  # Step 10: TTFB -- three cold chunks (first touch) and the same chunks
  # warm (second touch). Needs a fresh cache, so chunks not yet touched by
  # the full download above are used -- restart nginx with the same config
  # so the cache is empty again, and measure on specific ranges.
  docker rm -f "$C_NGINX" >/dev/null 2>&1 || true
  docker run -d --name "$C_NGINX" --network "$NETWORK" --shm-size=200m \
    -v "$WORKDIR/nginx/nginx-${name}.conf:/etc/nginx/nginx.conf:ro" \
    "$IMG_NGINX" >/dev/null
  sleep 1
  local offsets=("5242880-6291455" "25165824-26214399" "45088768-46137343")
  local cold=() warm=()
  for r in "${offsets[@]}"; do
    local t
    t=$(docker run --rm --network "$NETWORK" "$IMG_CURL" -s -o /dev/null -r "$r" \
      -w '%{time_starttransfer}' "http://$C_NGINX/blob/$OBJECT")
    cold+=("$t")
  done
  for r in "${offsets[@]}"; do
    local t
    t=$(docker run --rm --network "$NETWORK" "$IMG_CURL" -s -o /dev/null -r "$r" \
      -w '%{time_starttransfer}' "http://$C_NGINX/blob/$OBJECT")
    warm+=("$t")
  done
  printf -v "${prefix}_TTFB_COLD" '%s' "$(median3 "${cold[0]}" "${cold[1]}" "${cold[2]}")"
  printf -v "${prefix}_TTFB_WARM" '%s' "$(median3 "${warm[0]}" "${warm[1]}" "${warm[2]}")"

  # Step 11: CreateBucket through the S3 API (a signed request -- testing
  # the operation itself, not anonymity) on a separate probe-create bucket
  set +e
  aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api create-bucket --bucket probe-create >"$WORKDIR/createbucket_${name}.out" 2>&1
  cb_exit=$?
  set -e
  if [ "$cb_exit" = 0 ]; then
    printf -v "${prefix}_CREATE_BUCKET" '%s' "PASS"
  else
    cb_detail=$(tr -d '\n' < "$WORKDIR/createbucket_${name}.out" | head -c 150)
    printf -v "${prefix}_CREATE_BUCKET" '%s' "FAIL ($cb_detail)"
  fi

  # Image size
  local img_var
  if [ "$name" = garage ]; then img_var="$IMG_GARAGE"; else img_var="$IMG_SEAWEED"; fi
  local img_size
  img_size=$(docker image inspect "$img_var" --format '{{.Size}}')
  printf -v "${prefix}_IMG_SIZE_MB" '%s' "$(awk "BEGIN{printf \"%.1f\", $img_size/1024/1024}")"

  docker rm -f "$C_NGINX" >/dev/null 2>&1 || true
}

print_result() {
  local prefix="RES_$1" v c1 c2 tc tw
  v="${prefix}_UNREACHABLE";   echo "  unreachable from outside:          ${!v}"
  v="${prefix}_ANON_RANGE";    echo "  anonymous Range-GET from inside:   ${!v}"
  v="${prefix}_SIGNED_RANGE";  echo "  signed Range:                      ${!v}"
  v="${prefix}_DL_HTTP_CODE";  echo "  download through slice (HTTP):     ${!v}"
  v="${prefix}_CACHE_STATUS_1"; c1="${!v}"
  v="${prefix}_CACHE_STATUS_2"; c2="${!v}"
  echo "  X-Cache-Status 1st/2nd pass:       $c1 / $c2"
  v="${prefix}_SHA_OK";        echo "  sha256 through slice matched:      ${!v}"
  v="${prefix}_LOG_LINES";     echo "  access_log lines (64 MiB, 1m):     ${!v}"
  v="${prefix}_TTFB_COLD"; tc="${!v}"
  v="${prefix}_TTFB_WARM"; tw="${!v}"
  echo "  TTFB cold / warm (s):              $tc / $tw"
  v="${prefix}_CREATE_BUCKET"; echo "  CreateBucket through the S3 API:   ${!v}"
  v="${prefix}_IMG_SIZE_MB";   echo "  image size (MiB):                  ${!v}"
}

main() {
  preflight
  trap cleanup EXIT
  setup_workdir
  docker network create "$NETWORK" >/dev/null

  echo "=== Candidate: Garage ($IMG_GARAGE) ===" >&2
  reset_volumes
  bring_up_garage
  run_common_checks garage

  echo "=== Candidate: SeaweedFS ($IMG_SEAWEED) ===" >&2
  reset_volumes
  bring_up_seaweed
  run_common_checks seaweedfs

  echo
  echo "===================== Comparison table (experiment 7) ====================="
  echo
  echo "--- Garage ($IMG_GARAGE) ---"
  print_result garage
  echo
  echo "--- SeaweedFS ($IMG_SEAWEED) ---"
  print_result seaweedfs
  echo
  echo "Expected for a 64 MiB slot / slice 1m: $SLICE_COUNT slices + 1 master request."
  echo "=============================================================================="

  # Mandatory criteria: unreachable from outside + anonymous Range-GET from
  # inside + sha256 through slice matched.
  local pass=0
  for name in garage seaweedfs; do
    local prefix="RES_${name}"
    local v unreach anon sha
    v="${prefix}_UNREACHABLE"; unreach="${!v}"
    v="${prefix}_ANON_RANGE"; anon="${!v}"
    v="${prefix}_SHA_OK"; sha="${!v}"
    if [[ "$unreach" == PASS* ]] && [[ "$anon" == PASS* ]] && [[ "$sha" == PASS* ]]; then
      pass=1
      log "Candidate $name passed all three mandatory criteria"
    fi
  done

  if [ "$pass" = 1 ]; then
    exit 0
  else
    echo "No candidate passed all mandatory criteria -- see the table above." >&2
    exit 1
  fi
}

main "$@"
