#!/usr/bin/env bash
# Assert that a URL or local file is an actually decodable MP4 -- not just a
# 200/206 with a plausible Content-Length. This exists because the phase-1
# acceptance record was once validated against 3 MiB of random bytes that
# happened to have the right size and status code: the delivery mechanics
# (302 -> signed 206 -> right byte count) were proven, but nobody checked
# that a decoder could actually open the file (see 01-05-SUMMARY.md).
#
# ffprobe on this machine reorders `-of csv=p=0` output, so this parses
# named fields from `-of json` instead -- do not switch back to csv.
#
# Usage: check_decodable.sh <url-or-path>
set -euo pipefail

TARGET="${1:?usage: check_decodable.sh <url-or-path>}"

JSON=$(ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,pix_fmt,profile,level \
  -of json "$TARGET" 2>&1) || {
  echo "[FAIL] ffprobe could not even open: $TARGET" >&2
  echo "$JSON" >&2
  exit 1
}

CODEC=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
streams = data.get('streams') or []
print(streams[0]['codec_name'] if streams else '')
" "$JSON" 2>/dev/null || echo "")

if [ -z "$CODEC" ]; then
  echo "[FAIL] not a decodable video stream: $TARGET" >&2
  echo "$JSON" >&2
  exit 1
fi

echo "[PASS] decodable video stream ($CODEC): $TARGET"
