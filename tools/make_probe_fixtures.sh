#!/usr/bin/env bash
# Builds three real ffprobe fixtures for the 3D-detection and subtitle-
# enumeration test suites: two stereo-tagged Matroska clips and one clip
# carrying a mixed text-subtitle track pair. Captures what this project's
# own pinned ffmpeg actually emits -- never a value copied out of
# documentation, because the tag vocabulary and the side-data shape were not
# proven against a real file when the tests were first written.
#
# Run this to regenerate the fixtures; do not hand-edit their JSON output.
#
# Usage: make_probe_fixtures.sh [output_dir]
#   output_dir   default: tests/fixtures/probe (relative to this script's
#                repo root, i.e. src/)
set -euo pipefail

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$REPO_ROOT/tests/fixtures/probe}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if ! "$FFPROBE" -version >/dev/null 2>&1; then
  fail "tool '$FFPROBE' not found or does not run. Install ffmpeg/ffprobe locally on PATH, or set FFPROBE=<path>."
fi
if ! "$FFMPEG" -version >/dev/null 2>&1; then
  fail "tool '$FFMPEG' not found or does not run. Install ffmpeg locally on PATH, or set FFMPEG=<path>."
fi

FFMPEG_VERSION="$("$FFMPEG" -version | head -n 1)"
echo "ffmpeg: $FFMPEG_VERSION"

mkdir -p "$OUTPUT_DIR"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

probe_and_capture() {
  local media_path="$1" out_name="$2"
  "$FFPROBE" -v quiet -print_format json -show_streams -show_format "$media_path" \
    > "$OUTPUT_DIR/$out_name"
  local sha
  sha="$(sha256sum "$media_path" | cut -d' ' -f1)"
  echo "captured $out_name from $media_path (sha256 $sha)"
}

# --- stereo_sbs_tag.json: 3840x1080 full side-by-side, Matroska StereoMode tag ---

"$FFMPEG" -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc2=size=3840x1080:rate=24" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -t 1 \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
  -c:a aac \
  -metadata:s:v:0 stereo_mode=left_right \
  "$WORKDIR/stereo_sbs_tag.mkv"
probe_and_capture "$WORKDIR/stereo_sbs_tag.mkv" stereo_sbs_tag.json

# --- stereo_ou_tag.json: 1920x2160 full over/under, Matroska StereoMode tag ---

"$FFMPEG" -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc2=size=1920x2160:rate=24" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -t 1 \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
  -c:a aac \
  -metadata:s:v:0 stereo_mode=top_bottom \
  "$WORKDIR/stereo_ou_tag.mkv"
probe_and_capture "$WORKDIR/stereo_ou_tag.mkv" stereo_ou_tag.json

# --- subtitles_mixed.json: 1920x1080, one ASS track, one SubRip track, one audio track ---

cat > "$WORKDIR/sub.srt" << 'EOF'
1
00:00:00,000 --> 00:00:01,000
Hello
EOF

cat > "$WORKDIR/sub.ass" << 'EOF'
[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello
EOF

"$FFMPEG" -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc2=size=1920x1080:rate=24" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -i "$WORKDIR/sub.srt" -i "$WORKDIR/sub.ass" \
  -t 1 \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
  -c:a aac \
  -c:s:0 srt -c:s:1 ass \
  -map 0:v -map 1:a -map 2:s -map 3:s \
  -metadata:s:s:0 language=rus -metadata:s:s:0 title="Russian" \
  -metadata:s:s:1 language=eng -metadata:s:s:1 title="English" \
  "$WORKDIR/subtitles_mixed.mkv"
probe_and_capture "$WORKDIR/subtitles_mixed.mkv" subtitles_mixed.json

echo "OK: three fixtures written to $OUTPUT_DIR"
