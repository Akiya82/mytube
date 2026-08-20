#!/usr/bin/env bash
# Experiment 7 (DIST-02): Garage vs SeaweedFS через одинаковый nginx со slice.
#
# Прогоняет обоих кандидатов объектного хранилища через один и тот же сценарий:
# bring-up -> анонимное чтение изнутри сети (D-38) -> Range с подписью и без ->
# скачивание через nginx slice + sha256 -> формат access_log (B2) -> TTFB ->
# CreateBucket через S3 API. Печатает сравнительную таблицу и выбирает кандидата.
#
# Все объекты стенда используют фиксированные имена (вердикт M3), убираются
# в конце через `trap ... EXIT` независимо от исхода. Боевое MinIO владельца
# не адресуется вообще (D-18).
#
# Требует Docker (сеть mytube-exp07, временные контейнеры/тома). Ни одного
# пакета Python на хост не ставит: весь S3-доступ идёт контейнером amazon/aws-cli (M2).

set -euo pipefail

# Windows Git Bash переводит аргументы вида "/garage" в оценку как Windows-путь
# перед передачей их в docker.exe (не-MSYS-программу). Отключаем это для всего
# скрипта: единственная программа, которую мы дёргаем таким образом, — docker.
export MSYS_NO_PATHCONV=1

# ---------------------------------------------------------------------------
# Фиксированные имена стенда (вердикт M3): только они создаются и убираются.
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
# Preflight: docker в PATH и живой демон. Мёртвый демон — это ошибка стенда,
# а не результат замера "кандидат не прошёл".
# ---------------------------------------------------------------------------
preflight() {
  if ! command -v docker >/dev/null 2>&1; then
    local dd_bin="/c/Users/misha/AppData/Local/Programs/DockerDesktop/resources/bin"
    if [ -x "$dd_bin/docker" ]; then
      export PATH="$dd_bin:$PATH"
      log "docker не был в PATH, добавлен $dd_bin"
    fi
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker не найден в PATH. Установите Docker Desktop или добавьте его bin-каталог в PATH." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon недоступен (docker info упал). Запустите Docker Desktop и повторите." >&2
    echo "Это ошибка окружения стенда, а не результат сравнения кандидатов." >&2
    exit 1
  fi
  for c in openssl sha256sum curl; do
    command -v "$c" >/dev/null 2>&1 || { echo "ERROR: $c не найден в PATH" >&2; exit 1; }
  done
}

# ---------------------------------------------------------------------------
# Уборка (шаг 12): гасит и удаляет ровно перечисленные по именам объекты.
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
  log "тестовый объект $OBJECT: ${OBJ_SIZE_MB} МиБ, sha256=$SHA_ORIG"
}

reset_volumes() {
  docker rm -f "$C_GARAGE" "$C_SEAWEED" >/dev/null 2>&1 || true
  docker volume rm "$V_META" "$V_DATA" >/dev/null 2>&1 || true
  docker volume create "$V_META" >/dev/null
  docker volume create "$V_DATA" >/dev/null
}

# aws_signed <endpoint> <key> <secret> <args...> — S3-доступ контейнером aws-cli (M2)
aws_signed() {
  local endpoint=$1 key=$2 secret=$3; shift 3
  docker run --rm --network "$NETWORK" \
    -e AWS_ACCESS_KEY_ID="$key" -e AWS_SECRET_ACCESS_KEY="$secret" -e AWS_DEFAULT_REGION="$REGION" \
    -v "$WORKDIR/aws-config/config:/root/.aws/config:ro" \
    -v "$WORKDIR:/data" \
    "$IMG_AWSCLI" --endpoint-url "$endpoint" "$@"
}

# aws_anon <endpoint> <args...> — неподписанный S3-вызов
aws_anon() {
  local endpoint=$1; shift 1
  docker run --rm --network "$NETWORK" \
    -v "$WORKDIR/aws-config/config:/root/.aws/config:ro" \
    -v "$WORKDIR:/data" \
    "$IMG_AWSCLI" --endpoint-url "$endpoint" --no-sign-request "$@"
}

median3() { printf '%s\n%s\n%s\n' "$1" "$2" "$3" | sort -n | sed -n '2p'; }

# ---------------------------------------------------------------------------
# Bring-up Garage: layout assign/apply обязателен до bucket create (иначе
# bucket create падает); anonymous read пробуется PutBucketPolicy, затем
# штатными подкомандами garage bucket, показанными --help этого тега.
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

  log "Garage: жду готовности (garage status)..."
  local ok=0
  for _ in $(seq 1 30); do
    if docker exec "$C_GARAGE" /garage status >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  [ "$ok" = 1 ] || { echo "ERROR: Garage не поднялся за 60с" >&2; return 1; }

  node_line=$(docker exec "$C_GARAGE" /garage node id -q)
  node_id=${node_line%%@*}
  docker exec "$C_GARAGE" /garage layout assign -z dc1 -c 1G "$node_id" >/dev/null
  docker exec "$C_GARAGE" /garage layout apply --version 1 >/dev/null
  docker exec "$C_GARAGE" /garage bucket create "$BUCKET" >/dev/null

  key_out=$(docker exec "$C_GARAGE" /garage key create exp07-key)
  GARAGE_KEY_ID=$(echo "$key_out" | grep 'Key ID:' | awk '{print $NF}')
  GARAGE_SECRET=$(echo "$key_out" | grep 'Secret key:' | awk '{print $NF}')
  docker exec "$C_GARAGE" /garage bucket allow --read --write --owner "$BUCKET" --key exp07-key >/dev/null
  # без этого CreateBucket через S3 API отдаёт AccessDenied ("не разрешено
  # создавать бакеты этим ключом") — отдельное разрешение по умолчанию выключено
  docker exec "$C_GARAGE" /garage key allow --create-bucket exp07-key >/dev/null

  CANDIDATE_ENDPOINT="http://$C_GARAGE:3900"
  CANDIDATE_KEY="$GARAGE_KEY_ID"
  CANDIDATE_SECRET="$GARAGE_SECRET"
  CANDIDATE_CONTAINER="$C_GARAGE"
  CANDIDATE_PORT=3900

  # Шаг 3: анонимное чтение изнутри сети (D-38). Порядок по плану:
  # PutBucketPolicy -> штатные подкоманды garage bucket, показанные --help.
  set +e
  local policy_out
  policy_out=$(aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api put-bucket-policy --bucket "$BUCKET" --policy file:///data/policy.json 2>&1)
  set -e
  log "Garage PutBucketPolicy: $policy_out"
  # `garage bucket --help`/`garage bucket website --help` в теге v2.3.0 не
  # содержат подкоманды, дающей анонимному запросу s3:GetObject на бакет:
  # `allow`/`deny` работают только с именованными ключами (не с anonymous),
  # а `website` — отдельный HTTP-сервер с Host-based роутингом и
  # index-document семантикой, не путь GetObject S3 API, которым ходит slice.
  # Итог зафиксирован ниже по фактическому результату шага 6 (curl), а не
  # предположением.
}

# ---------------------------------------------------------------------------
# Bring-up SeaweedFS: identities.json с identity "anonymous" (action Read) —
# штатный механизм анонимного чтения (см. read_first плана и SeaweedFS wiki).
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

  log "SeaweedFS: жду готовности S3-порта..."
  local ok=0
  for _ in $(seq 1 30); do
    if aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" s3api list-buckets >/dev/null 2>&1; then
      ok=1; break
    fi
    sleep 2
  done
  [ "$ok" = 1 ] || { echo "ERROR: SeaweedFS не поднялся за 60с" >&2; return 1; }

  # Бакет заводится штатным путём кандидата — подписанным create-bucket
  # (анонимное CreateBucket корректно отклоняется: у identity "anonymous"
  # только Read, без Write/Admin — сам по себе валидный замер шага 11).
  aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api create-bucket --bucket "$BUCKET" >/dev/null
}

# ---------------------------------------------------------------------------
# Общая последовательность для обоих кандидатов (шаги 4-11 плана).
# Пишет результат в глобальные переменные RES_<candidate>_<metric>.
# ---------------------------------------------------------------------------
run_common_checks() {
  local name=$1
  local prefix="RES_${name}"

  # Шаг 4: тестовый объект (уже сгенерирован в setup_workdir)
  aws_signed "$CANDIDATE_ENDPOINT" "$CANDIDATE_KEY" "$CANDIDATE_SECRET" \
    s3api put-object --bucket "$BUCKET" --key "$OBJECT" --body "/data/$OBJECT" >/dev/null

  # Шаг 5: недостижимость снаружи (D-38, B1)
  local ip host_curl_code bridge_dns
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CANDIDATE_CONTAINER")
  set +e
  curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$ip:$CANDIDATE_PORT/" >"$WORKDIR/host_curl.out" 2>/dev/null
  host_curl_exit=$?
  set -e
  host_curl_code=$(cat "$WORKDIR/host_curl.out" 2>/dev/null || echo "000")
  if [ "$host_curl_exit" != 0 ]; then
    printf -v "${prefix}_UNREACHABLE" '%s' "PASS (curl с хоста: exit=$host_curl_exit, соединение не установлено)"
  else
    printf -v "${prefix}_UNREACHABLE" '%s' "FAIL (curl с хоста получил HTTP $host_curl_code — порт достижим)"
  fi

  set +e
  bridge_dns=$(docker run --rm --network bridge "$IMG_ALPINE" sh -c "getent hosts $CANDIDATE_CONTAINER" 2>&1)
  bridge_dns_exit=$?
  set -e
  if [ "$bridge_dns_exit" != 0 ]; then
    log "$name: имя $CANDIDATE_CONTAINER не резолвится из сети bridge (ожидаемо, D-38)"
  else
    log "$name: WARNING имя $CANDIDATE_CONTAINER РЕЗОЛВИТСЯ из сети bridge: $bridge_dns"
  fi

  # Шаг 6: анонимный неподписанный Range-GET изнутри сети (D-38, B1) —
  # обязательный критерий. Две проверки: середина файла и хвостовой диапазон.
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
    printf -v "${prefix}_ANON_RANGE" '%s' "FAIL (диапазон 0- дал HTTP $anon1_code, хвостовой дал HTTP $anon2_code — не 206)"
    ANON_RANGE_OK_FOR_NGINX_TEST=0
  fi

  # Шаг 7: Range с подписью — контрольная проверка (не D-38, сам Range)
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

  # Шаг 8: скачивание через nginx со slice, сверка sha256
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
    sha_dl="—"
  fi

  printf -v "${prefix}_DL_HTTP_CODE" '%s' "$dl_code"
  printf -v "${prefix}_CACHE_STATUS_1" '%s' "$cache_status_1"
  if [ "$dl_code" = "200" ] && [ "$sha_dl" = "$SHA_ORIG" ]; then
    printf -v "${prefix}_SHA_OK" '%s' "PASS"
  else
    printf -v "${prefix}_SHA_OK" '%s' "FAIL (HTTP $dl_code, sha256 скачанного=$sha_dl, ожидался $SHA_ORIG)"
  fi

  # Шаг 9: формат access_log живьём (B2) — снимается сразу после ПЕРВОГО
  # полного скачивания, до второго (кэш-проверочного) запроса, чтобы число
  # строк отражало ровно один прогон по 64 МиБ (как того требует Task 1).
  local log_total log_sample
  log_total=$(docker exec "$C_NGINX" sh -c 'wc -l < /var/log/nginx/blob_access.log' 2>/dev/null || echo 0)
  log_sample=$(docker exec "$C_NGINX" sh -c 'head -3 /var/log/nginx/blob_access.log' 2>/dev/null || echo "")
  printf -v "${prefix}_LOG_LINES" '%s' "$log_total"
  printf '%s\n' "$log_sample" > "$WORKDIR/log_sample_${name}.txt"

  # Второй прогон (та же ссылка) — подтверждение, что второй раз отдаётся
  # из кэша (X-Cache-Status). Снимается после захвата формата лога, чтобы
  # не задваивать число строк из шага 9.
  docker run --rm --network "$NETWORK" -v "$WORKDIR:/out" "$IMG_CURL" \
    -s -D "/out/dl2_headers.txt" -o "/out/downloaded2_${name}.bin" \
    "http://$C_NGINX/blob/$OBJECT" >/dev/null
  cache_status_2=$(grep -i '^X-Cache-Status:' "$WORKDIR/dl2_headers.txt" | tr -d '\r' | awk '{print $2}') || true
  printf -v "${prefix}_CACHE_STATUS_2" '%s' "$cache_status_2"

  # Шаг 10: TTFB — три холодных куска (первое обращение) и они же прогретые
  # (повторное обращение). Требует свежего кэша, поэтому используем куски,
  # ещё не тронутые полным скачиванием выше, — рестартуем nginx с тем же
  # конфигом, чтобы кэш был снова пуст, и меряем на конкретных диапазонах.
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

  # Шаг 11: CreateBucket через S3 API (подписанный запрос — тест самой
  # операции, а не анонимности) на отдельном бакете probe-create
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

  # Размер образа
  local img_var
  if [ "$name" = garage ]; then img_var="$IMG_GARAGE"; else img_var="$IMG_SEAWEED"; fi
  local img_size
  img_size=$(docker image inspect "$img_var" --format '{{.Size}}')
  printf -v "${prefix}_IMG_SIZE_MB" '%s' "$(awk "BEGIN{printf \"%.1f\", $img_size/1024/1024}")"

  docker rm -f "$C_NGINX" >/dev/null 2>&1 || true
}

print_result() {
  local prefix="RES_$1" v c1 c2 tc tw
  v="${prefix}_UNREACHABLE";   echo "  недостижим снаружи:                ${!v}"
  v="${prefix}_ANON_RANGE";    echo "  анонимный Range-GET изнутри:       ${!v}"
  v="${prefix}_SIGNED_RANGE";  echo "  Range с подписью:                  ${!v}"
  v="${prefix}_DL_HTTP_CODE";  echo "  скачивание через slice (HTTP):     ${!v}"
  v="${prefix}_CACHE_STATUS_1"; c1="${!v}"
  v="${prefix}_CACHE_STATUS_2"; c2="${!v}"
  echo "  X-Cache-Status 1-й/2-й прогон:     $c1 / $c2"
  v="${prefix}_SHA_OK";        echo "  sha256 через slice совпал:         ${!v}"
  v="${prefix}_LOG_LINES";     echo "  строк в access_log (64 МиБ, 1m):   ${!v}"
  v="${prefix}_TTFB_COLD"; tc="${!v}"
  v="${prefix}_TTFB_WARM"; tw="${!v}"
  echo "  TTFB холодный / прогретый (с):     $tc / $tw"
  v="${prefix}_CREATE_BUCKET"; echo "  CreateBucket через S3 API:         ${!v}"
  v="${prefix}_IMG_SIZE_MB";   echo "  размер образа (МиБ):               ${!v}"
}

main() {
  preflight
  trap cleanup EXIT
  setup_workdir
  docker network create "$NETWORK" >/dev/null

  echo "=== Кандидат: Garage ($IMG_GARAGE) ===" >&2
  reset_volumes
  bring_up_garage
  run_common_checks garage

  echo "=== Кандидат: SeaweedFS ($IMG_SEAWEED) ===" >&2
  reset_volumes
  bring_up_seaweed
  run_common_checks seaweedfs

  echo
  echo "===================== Сравнительная таблица (эксперимент 7) ====================="
  echo
  echo "--- Garage ($IMG_GARAGE) ---"
  print_result garage
  echo
  echo "--- SeaweedFS ($IMG_SEAWEED) ---"
  print_result seaweedfs
  echo
  echo "Ожидание по слоту в 64 МиБ / slice 1m: $SLICE_COUNT срезов + 1 основной запрос."
  echo "==================================================================================="

  # Обязательные критерии (per Task 1): недостижим снаружи + анонимный
  # Range-GET изнутри + sha256 через slice совпал.
  local pass=0
  for name in garage seaweedfs; do
    local prefix="RES_${name}"
    local v unreach anon sha
    v="${prefix}_UNREACHABLE"; unreach="${!v}"
    v="${prefix}_ANON_RANGE"; anon="${!v}"
    v="${prefix}_SHA_OK"; sha="${!v}"
    if [[ "$unreach" == PASS* ]] && [[ "$anon" == PASS* ]] && [[ "$sha" == PASS* ]]; then
      pass=1
      log "Кандидат $name прошёл все три обязательных критерия"
    fi
  done

  if [ "$pass" = 1 ]; then
    exit 0
  else
    echo "Ни один кандидат не прошёл все обязательные критерии — см. таблицу выше." >&2
    exit 1
  fi
}

main "$@"
