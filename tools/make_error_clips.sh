#!/usr/bin/env bash
# Builds four short MP4 clips to stand in for error codes: "this video was
# deleted", "no such link", "service unavailable", "playlist not ready yet".
# Each is 6s, 1280x720, H.264 High, yuv420p, +faststart (this project's own
# encoding pitfalls).
#
# Usage: make_error_clips.sh <fontfile> [output_dir]
#   fontfile     required argument, with no "smart" default: DejaVuSans
#                does not exist on the owner's Windows host, and the script
#                would silently produce a black screen -- exactly what
#                these clips exist to prevent. On Windows/Git Bash, pass a
#                POSIX path, for example /c/Windows/Fonts/arial.ttf
#   output_dir   default: ./edge/errors
set -euo pipefail

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [ "${1:-}" = "" ]; then
  fail "missing required argument: font file path (fontfile). Usage: make_error_clips.sh <fontfile> [output_dir]"
fi
FONTFILE="$1"
OUTPUT_DIR="${2:-./edge/errors}"

check_ffmpeg_family() {
  local bin="$1" var_name="$2"
  if ! "$bin" -version >/dev/null 2>&1; then
    fail "tool '$bin' not found or does not run ($var_name). Fix: install $bin locally on PATH, or set the environment variable ${var_name}=<path>."
  fi
}

check_drawtext() {
  if ! "$FFMPEG" -hide_banner -filters 2>/dev/null | grep -q 'drawtext'; then
    fail "this $FFMPEG build has no drawtext filter -- without it the clips would stay a black screen with no text."
  fi
}

check_fontfile() {
  [ -f "$FONTFILE" ] || fail "font file not found: $FONTFILE"
}

preflight() {
  check_ffmpeg_family "$FFMPEG" FFMPEG
  check_ffmpeg_family "$FFPROBE" FFPROBE
  check_drawtext
  check_fontfile
}

preflight

mkdir -p "$OUTPUT_DIR"

# Colon and backslash are separators inside a drawtext filter expression;
# the font path must be escaped before it is substituted into fontfile=.
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

  # Self-check: video stream parameters (this project's own encoding pitfalls)
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
    *) fail "ffprobe check failed for $output: got '$actual', expected 'h264 High 41 yuv420p'." ;;
  esac

  python3 "$(dirname "$0")/check_faststart.py" "$output"
}

build_clip "this video was deleted" "$OUTPUT_DIR/deleted.mp4"
build_clip "no such link" "$OUTPUT_DIR/not-found.mp4"
build_clip "service unavailable" "$OUTPUT_DIR/unavailable.mp4"
build_clip "playlist not ready yet" "$OUTPUT_DIR/playlist-not-ready.mp4"

echo "OK: four clips built and verified in $OUTPUT_DIR"
