#!/usr/bin/env bash
# Собирает стерильный контрольный клип для эксперимента 1 (D-34): H.264 High 4.1,
# yuv420p, +faststart, 1080p ~6 Мбит/с, 120 секунд. Проверяет себя же машинно.
#
# Usage: make_control_clip.sh [output_path] [source_path] [--skip-docker-check]
#   output_path   default: ./build/control.mp4
#   source_path   optional; без него собирается синтетический источник (testsrc2 + sine)
set -euo pipefail

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"

SKIP_DOCKER_CHECK=0
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --skip-docker-check) SKIP_DOCKER_CHECK=1 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

OUTPUT_PATH="${POSITIONAL[0]:-./build/control.mp4}"
SOURCE_PATH="${POSITIONAL[1]:-}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

check_ffmpeg_family() {
  local bin="$1" var_name="$2"
  if ! "$bin" -version >/dev/null 2>&1; then
    fail "инструмент '$bin' не найден или не запускается ($var_name). Решение: поставить $bin локально в PATH, либо подставить переменную окружения ${var_name}=<путь к обёртке, гоняющей его в контейнере>."
  fi
}

check_python() {
  if ! python3 --version >/dev/null 2>&1; then
    fail "инструмент 'python3' не найден. Поставьте Python 3 локально в PATH."
  fi
}

check_docker() {
  if [ "$SKIP_DOCKER_CHECK" -eq 1 ]; then
    echo "docker compose / hello-world: пропущено (--skip-docker-check)"
    return 0
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fail "инструмент 'docker compose' не найден или не запускается. Поставьте Docker Desktop (Windows) либо Docker Engine + compose plugin, либо запустите со флагом --skip-docker-check если контрольный клип собирается без Docker."
  fi
  if ! docker run --rm hello-world >/dev/null 2>&1; then
    fail "'docker run --rm hello-world' не прошёл — Docker установлен, но не запущен или недоступен текущему пользователю. Запустите Docker Desktop, либо используйте --skip-docker-check."
  fi
}

preflight() {
  # Проверки (M10, m8): ffmpeg -version, ffprobe -version, python3 --version,
  # docker compose version, docker run --rm hello-world.
  check_ffmpeg_family "$FFMPEG" FFMPEG
  check_ffmpeg_family "$FFPROBE" FFPROBE
  check_python
  check_docker
}

preflight

mkdir -p "$(dirname "$OUTPUT_PATH")"

if [ -n "$SOURCE_PATH" ]; then
  "$FFMPEG" -y -i "$SOURCE_PATH" \
    -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p \
    -b:v 6M -maxrate 8M -bufsize 12M -g 48 -keyint_min 48 \
    -c:a aac -b:a 160k -ac 2 \
    -movflags +faststart \
    "$OUTPUT_PATH"
else
  "$FFMPEG" -y \
    -f lavfi -i "testsrc2=size=1920x1080:rate=24" \
    -f lavfi -i "sine=frequency=440:sample_rate=48000" \
    -t 120 \
    -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p \
    -b:v 6M -maxrate 8M -bufsize 12M -g 48 -keyint_min 48 \
    -c:a aac -b:a 160k -ac 2 \
    -movflags +faststart \
    "$OUTPUT_PATH"
fi

# Самопроверка 1: параметры видеопотока (D-34, техническая грабля 2)
PROBE_JSON=$("$FFPROBE" -v error -select_streams v:0 \
  -show_entries stream=codec_name,profile,level,pix_fmt -of json "$OUTPUT_PATH")

check_field() {
  local field="$1" expected="$2"
  local actual
  actual=$(printf '%s' "$PROBE_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(data['streams'][0].get('$field', ''))
")
  if [ "$actual" != "$expected" ]; then
    fail "проверка ffprobe провалена: поле '$field' = '$actual', ожидалось '$expected'. Собранный файл не соответствует D-34 и даст чёрный экран в AVPro."
  fi
}

check_field codec_name h264
check_field profile High
check_field level 41
check_field pix_fmt yuv420p

# Самопроверка 2: порядок top-level атомов (техническая грабля 1, +faststart)
python3 "$(dirname "$0")/check_faststart.py" "$OUTPUT_PATH"

echo "OK: $OUTPUT_PATH собран и проверен (H.264 High 4.1, yuv420p, moov раньше mdat)"
