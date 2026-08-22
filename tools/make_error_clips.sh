#!/usr/bin/env bash
# Собирает три коротких MP4-ролика вместо кодов ошибок (D-31): "видео удалено",
# "такой ссылки нет", "сервис недоступен". Каждый — 6с, 1280x720, H.264 High,
# yuv420p, +faststart (технические грабли проекта 1 и 2).
#
# Usage: make_error_clips.sh <fontfile> [output_dir]
#   fontfile     обязательный аргумент, без "умного" дефолта (вердикт M10):
#                DejaVuSans на Windows-хосте владельца не существует, и скрипт
#                молча дал бы чёрный экран — ровно то, что эти ролики
#                предотвращают. На Windows/Git Bash передавайте POSIX-путь,
#                например /c/Windows/Fonts/arial.ttf
#   output_dir   default: ./edge/errors
set -euo pipefail

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [ "${1:-}" = "" ]; then
  fail "отсутствует обязательный аргумент: путь к файлу шрифта (fontfile). Usage: make_error_clips.sh <fontfile> [output_dir]"
fi
FONTFILE="$1"
OUTPUT_DIR="${2:-./edge/errors}"

check_ffmpeg_family() {
  local bin="$1" var_name="$2"
  if ! "$bin" -version >/dev/null 2>&1; then
    fail "инструмент '$bin' не найден или не запускается ($var_name). Решение: поставить $bin локально в PATH, либо подставить переменную окружения ${var_name}=<путь>."
  fi
}

check_drawtext() {
  if ! "$FFMPEG" -hide_banner -filters 2>/dev/null | grep -q 'drawtext'; then
    fail "сборка $FFMPEG не содержит фильтр drawtext — без него ролики останутся чёрным экраном без текста."
  fi
}

check_fontfile() {
  [ -f "$FONTFILE" ] || fail "файл шрифта не найден: $FONTFILE"
}

preflight() {
  check_ffmpeg_family "$FFMPEG" FFMPEG
  check_ffmpeg_family "$FFPROBE" FFPROBE
  check_drawtext
  check_fontfile
}

preflight

mkdir -p "$OUTPUT_DIR"

# Двоеточие и обратный слэш — разделители внутри выражения фильтра drawtext,
# путь к шрифту обязан быть экранирован перед подстановкой в fontfile=.
escape_fontfile() {
  printf '%s' "$1" | sed -e 's/\\/\\\\\\\\/g' -e 's/:/\\\\:/g'
}
FONTFILE_ESCAPED=$(escape_fontfile "$FONTFILE")

build_clip() {
  local text="$1" output="$2"
  "$FFMPEG" -y \
    -f lavfi -i "color=c=black:size=1280x720:rate=24:duration=6" \
    -vf "drawtext=fontfile='${FONTFILE_ESCAPED}':text='${text}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p \
    -movflags +faststart \
    "$output"

  # Самопроверка: параметры видеопотока (технические грабли проекта 1 и 2)
  local probe_json actual
  probe_json=$("$FFPROBE" -v error -select_streams v:0 -show_entries stream=codec_name,profile,level,pix_fmt -of json "$output")
  actual=$(printf '%s' "$probe_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
s = data['streams'][0]
print(s.get('codec_name'), s.get('profile'), s.get('level'), s.get('pix_fmt'))
")
  case "$actual" in
    "h264 High 41 yuv420p") : ;;
    *) fail "проверка ffprobe провалена для $output: получено '$actual', ожидалось 'h264 High 41 yuv420p'." ;;
  esac

  python3 "$(dirname "$0")/check_faststart.py" "$output"
}

build_clip "this video was deleted" "$OUTPUT_DIR/deleted.mp4"
build_clip "no such link" "$OUTPUT_DIR/not-found.mp4"
build_clip "service unavailable" "$OUTPUT_DIR/unavailable.mp4"

echo "OK: три ролика собраны и проверены в $OUTPUT_DIR"
