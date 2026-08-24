#!/usr/bin/env bash
# Builds a sterile control clip for experiment 1: H.264 High 4.1, yuv420p,
# +faststart, 1080p ~6 Mbit/s, 120 seconds. Verifies itself by machine.
#
# Usage: make_control_clip.sh [output_path] [source_path] [--skip-docker-check]
#   output_path   default: ./build/control.mp4
#   source_path   optional; without it a synthetic source is built (testsrc2 + sine)
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
    fail "tool '$bin' not found or does not run ($var_name). Fix: install $bin locally on PATH, or set the environment variable ${var_name}=<path to a wrapper that runs it in a container>."
  fi
}

check_python() {
  if ! python3 --version >/dev/null 2>&1; then
    fail "tool 'python3' not found. Install Python 3 locally on PATH."
  fi
}

check_docker() {
  if [ "$SKIP_DOCKER_CHECK" -eq 1 ]; then
    echo "docker compose / hello-world: skipped (--skip-docker-check)"
    return 0
  fi
  if ! docker compose version >/dev/null 2>&1; then
    fail "tool 'docker compose' not found or does not run. Install Docker Desktop (Windows) or Docker Engine + the compose plugin, or run with --skip-docker-check if the control clip is built without Docker."
  fi
  if ! docker run --rm hello-world >/dev/null 2>&1; then
    fail "'docker run --rm hello-world' failed -- Docker is installed but not running or not reachable by the current user. Start Docker Desktop, or use --skip-docker-check."
  fi
}

preflight() {
  # Checks: ffmpeg -version, ffprobe -version, python3 --version,
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

# Self-check 1: video stream parameters
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
    fail "ffprobe check failed: field '$field' = '$actual', expected '$expected'. The built file does not meet the required encoding and will show a black screen in AVPro."
  fi
}

check_field codec_name h264
check_field profile High
check_field level 41
check_field pix_fmt yuv420p

# Self-check 2: top-level atom order (+faststart)
python3 "$(dirname "$0")/check_faststart.py" "$OUTPUT_PATH"

echo "OK: $OUTPUT_PATH built and verified (H.264 High 4.1, yuv420p, moov before mdat)"
