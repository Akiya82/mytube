#!/usr/bin/env bash
# Builds six short MP4 fixture clips (three pairs, one stream parameter
# differing per pair), segments each into HLS, hand-assembles one combined
# manifest per pair with a discontinuity tag at the boundary, and serves the
# result from a throwaway container. Used to observe, inside VRChat, what a
# video player does at a boundary between clips whose stream parameters
# differ. This project's own known encoding pitfalls apply here exactly as
# they do everywhere else: +faststart in every branch, an explicit H.264
# High 4.1 / yuv420p profile so the encoder cannot silently emit a 10-bit
# stream.
#
# Usage: experiment_discontinuity.sh <fontfile> [output_dir] [port]
#   fontfile     required argument, with no "smart" default -- see
#                make_error_clips.sh for the reasoning. On Windows/Git Bash,
#                pass a POSIX path, for example /c/Windows/Fonts/arial.ttf
#   output_dir   default: ./build/experiment-discontinuity
#   port         default: 8099
#
# Usage: experiment_discontinuity.sh --stop
#   Removes the throwaway serving container and exits. No other argument
#   is read in this mode.
set -euo pipefail

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"
CONTAINER_NAME="mytube-experiment-discontinuity"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

stop_container() {
  if docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "OK: container $CONTAINER_NAME removed."
  else
    echo "OK: container $CONTAINER_NAME was not running."
  fi
}

if [ "${1:-}" = "--stop" ]; then
  stop_container
  exit 0
fi

if [ "${1:-}" = "" ]; then
  fail "missing required argument: font file path (fontfile). Usage: experiment_discontinuity.sh <fontfile> [output_dir] [port]"
fi
FONTFILE="$1"
OUTPUT_DIR="${2:-./build/experiment-discontinuity}"
PORT="${3:-8099}"

check_ffmpeg_family() {
  local bin="$1" var_name="$2"
  if ! "$bin" -version >/dev/null 2>&1; then
    fail "tool '$bin' not found or does not run ($var_name). Fix: install $bin locally on PATH, or set the environment variable ${var_name}=<path>."
  fi
}

check_drawtext() {
  if ! "$FFMPEG" -hide_banner -filters 2>/dev/null | grep -q 'drawtext'; then
    fail "this $FFMPEG build has no drawtext filter -- without it the clips would stay a plain color with no text."
  fi
}

check_fontfile() {
  [ -f "$FONTFILE" ] || fail "font file not found: $FONTFILE"
}

check_docker() {
  if ! docker info >/dev/null 2>&1; then
    fail "docker is not available or not running -- start Docker Desktop (or the docker daemon) before running this script."
  fi
}

preflight() {
  check_ffmpeg_family "$FFMPEG" FFMPEG
  check_ffmpeg_family "$FFPROBE" FFPROBE
  check_drawtext
  check_fontfile
  check_docker
}

preflight

# Colon and backslash are separators inside a drawtext filter expression;
# the font path must be escaped before it is substituted into fontfile=.
escape_fontfile() {
  printf '%s' "$1" | sed -e 's/\\/\\\\\\\\/g' -e 's/:/\\\\:/g'
}
FONTFILE_ESCAPED=$(escape_fontfile "$FONTFILE")

# Keep only code under version control; a generated fixture tree does not
# belong in the repository. build/ is already ignored project-wide, but
# this makes the script self-contained regardless.
ensure_gitignore_entry() {
  local gitignore="$REPO_ROOT/.gitignore"
  if ! grep -qxF 'build/' "$gitignore" 2>/dev/null; then
    printf '\nbuild/\n' >> "$gitignore"
  fi
}
ensure_gitignore_entry

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
SCRATCH_DIR="$OUTPUT_DIR/.scratch"
mkdir -p "$SCRATCH_DIR"

# Same ffprobe/faststart self-check make_error_clips.sh and
# make_control_clip.sh already run on every clip they produce.
check_encoding() {
  local output="$1" probe_json actual
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
  python3 "$REPO_ROOT/tools/check_faststart.py" "$output"
}

# Builds one 8-second clip: a solid color, burned-in label, and a distinct
# sine tone -- the two distinct tones per pair make an audio/picture drift
# audible immediately rather than something a watcher has to squint for.
#
# The keyframe interval is forced to two seconds (frames = 2x the clip's
# own frame rate, not a hardcoded number, because the fps pair differs in
# exactly that) with scene-change detection off, so the encoder cannot
# fall back to its own much longer default interval. An eight-second clip
# of a single solid color has no motion at all, and a default interval
# over it produces exactly one keyframe -- the segmenter below can only
# cut where a keyframe already is, and one keyframe means one segment.
build_clip() {
  local label="$1" color="$2" size="$3" rate="$4" freq="$5" channels="$6" abitrate="$7" output="$8"
  local keyframe_interval=$((rate * 2))
  "$FFMPEG" -y \
    -f lavfi -i "color=c=${color}:size=${size}:rate=${rate}:duration=8" \
    -f lavfi -i "sine=frequency=${freq}:duration=8" \
    -vf "drawtext=fontfile='${FONTFILE_ESCAPED}':text='${label}':fontcolor=white:fontsize=72:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p \
    -g "${keyframe_interval}" -keyint_min "${keyframe_interval}" -sc_threshold 0 \
    -c:a aac -ac "${channels}" -b:a "${abitrate}" \
    -movflags +faststart \
    "$output"
  check_encoding "$output"
}

# Segments one clip with -c copy, writing the .ts segments directly into
# the output directory root (so they sit next to the combined manifest
# that will reference them by bare filename) and its own per-clip
# manifest into a scratch subdirectory that the final "exactly three
# .m3u8 files" count does not see.
#
# -hls_time is set to match the forced two-second keyframe interval, not
# to the four seconds the unexecuted research protocol sketch used: with
# a two-second keyframe interval, -hls_time 4 only ever finds a cutting
# keyframe every four seconds and yields two segments per eight-second
# clip, not the four this fixture set needs to be a usable multi-segment
# boundary test. Measured directly while building this script.
segment_clip() {
  local pair_side="$1" input="$2"
  local manifest="$SCRATCH_DIR/clip_${pair_side}.m3u8"
  ( cd "$OUTPUT_DIR" && "$FFMPEG" -y -i "$input" -c copy -f hls -hls_time 2 \
      -hls_playlist_type vod -hls_segment_filename "seg_${pair_side}_%03d.ts" \
      ".scratch/clip_${pair_side}.m3u8" )
  local count
  count=$(grep -c '^#EXTINF' "$manifest")
  if [ "$count" -lt 4 ]; then
    fail "clip_${pair_side} segmented into only ${count} segment(s) -- the keyframe interval did not take. Fix the encode, do not lower this requirement."
  fi
}

target_duration() {
  grep '#EXT-X-TARGETDURATION:' "$1" | head -1 | sed 's/.*://'
}

# The EXTINF/URI segment lines only, header and footer stripped.
segment_body() {
  sed -n '/^#EXTINF/,/^#EXT-X-ENDLIST/p' "$1" | sed '$d'
}

# Hand-assembles one combined manifest: A's segments, an unconditional
# discontinuity tag, B's segments, end tag. Target duration is the longer
# of the two halves' own target durations, never assumed, so a malformed
# manifest never ships as a side effect of a wrong constant.
build_combined_manifest() {
  local pair="$1"
  local manifest_a="$SCRATCH_DIR/clip_${pair}_a.m3u8"
  local manifest_b="$SCRATCH_DIR/clip_${pair}_b.m3u8"
  local out="$OUTPUT_DIR/boundary_${pair}.m3u8"
  local td_a td_b td
  td_a=$(target_duration "$manifest_a")
  td_b=$(target_duration "$manifest_b")
  td=$(( td_a > td_b ? td_a : td_b ))
  {
    echo "#EXTM3U"
    echo "#EXT-X-VERSION:3"
    echo "#EXT-X-TARGETDURATION:${td}"
    echo "#EXT-X-MEDIA-SEQUENCE:0"
    echo "#EXT-X-PLAYLIST-TYPE:VOD"
    segment_body "$manifest_a"
    echo "#EXT-X-DISCONTINUITY"
    segment_body "$manifest_b"
    echo "#EXT-X-ENDLIST"
  } > "$out"

  local disc_count extinf_count
  disc_count=$(grep -c '^#EXT-X-DISCONTINUITY' "$out")
  extinf_count=$(grep -c '^#EXTINF' "$out")
  [ "$disc_count" -eq 1 ] || fail "boundary_${pair}.m3u8 has ${disc_count} discontinuity tags, expected exactly 1."
  [ "$extinf_count" -ge 8 ] || fail "boundary_${pair}.m3u8 has ${extinf_count} segments, expected at least 8."
  grep -q '^#EXT-X-ENDLIST$' "$out" || fail "boundary_${pair}.m3u8 is missing the end-list tag."
}

# Pair "res": resolution differs (1920x1080 vs 1280x720), fps and audio
# layout held constant.
build_clip "RES A" blue 1920x1080 24 440 2 192k "$OUTPUT_DIR/clip_res_a.mp4"
build_clip "RES B" red 1280x720 24 880 2 192k "$OUTPUT_DIR/clip_res_b.mp4"
segment_clip res_a "$OUTPUT_DIR/clip_res_a.mp4"
segment_clip res_b "$OUTPUT_DIR/clip_res_b.mp4"
build_combined_manifest res

# Pair "fps": frame rate differs (24 vs 30), resolution and audio layout
# held constant.
build_clip "FPS A" blue 1920x1080 24 440 2 192k "$OUTPUT_DIR/clip_fps_a.mp4"
build_clip "FPS B" red 1920x1080 30 880 2 192k "$OUTPUT_DIR/clip_fps_b.mp4"
segment_clip fps_a "$OUTPUT_DIR/clip_fps_a.mp4"
segment_clip fps_b "$OUTPUT_DIR/clip_fps_b.mp4"
build_combined_manifest fps

# Pair "audio": channel layout differs (stereo vs 5.1), resolution and
# frame rate held constant.
build_clip "AUDIO A" blue 1920x1080 24 440 2 192k "$OUTPUT_DIR/clip_audio_a.mp4"
build_clip "AUDIO B" red 1920x1080 24 880 6 384k "$OUTPUT_DIR/clip_audio_b.mp4"
segment_clip audio_a "$OUTPUT_DIR/clip_audio_a.mp4"
segment_clip audio_b "$OUTPUT_DIR/clip_audio_b.mp4"
build_combined_manifest audio

# Final sanity count before serving anything -- six clips, at least twelve
# segments (four per clip, six clips), exactly three combined manifests.
mp4_count=$(find "$OUTPUT_DIR" -maxdepth 1 -name '*.mp4' | wc -l)
ts_count=$(find "$OUTPUT_DIR" -maxdepth 1 -name '*.ts' | wc -l)
m3u8_count=$(find "$OUTPUT_DIR" -maxdepth 1 -name '*.m3u8' | wc -l)
[ "$mp4_count" -eq 6 ] || fail "expected 6 clips in $OUTPUT_DIR, found $mp4_count."
[ "$ts_count" -ge 12 ] || fail "expected at least 12 segments in $OUTPUT_DIR, found $ts_count."
[ "$m3u8_count" -eq 3 ] || fail "expected exactly 3 combined manifests in $OUTPUT_DIR, found $m3u8_count."

# Serve the output directory with a throwaway container rather than by
# editing this project's own edge configuration (edge/nginx.conf.template stays untouched). Docker Desktop on Windows needs a native Windows path for
# a bind mount; cygpath does that conversion when it is present, and the
# already-POSIX path works unmodified everywhere else.
if command -v cygpath >/dev/null 2>&1; then
  DOCKER_VOL_SRC="$(cygpath -w "$OUTPUT_DIR")"
else
  DOCKER_VOL_SRC="$OUTPUT_DIR"
fi

docker run --rm -d --name "$CONTAINER_NAME" -p "${PORT}:80" \
  -v "${DOCKER_VOL_SRC}:/usr/share/nginx/html:ro" nginx:alpine >/dev/null

echo "OK: fixtures built, self-checked, and served from $OUTPUT_DIR"
echo ""
echo "Manifest addresses (replace <host> with the hostname a reverse proxy points at this port):"
echo "  http://<host>:${PORT}/boundary_res.m3u8"
echo "  http://<host>:${PORT}/boundary_fps.m3u8"
echo "  http://<host>:${PORT}/boundary_audio.m3u8"
echo ""
echo "Container: ${CONTAINER_NAME}"
echo "Access log: docker logs ${CONTAINER_NAME}"
echo "Stop and remove when the session is over: docker rm -f ${CONTAINER_NAME}"
echo "  (or: $0 --stop)"
