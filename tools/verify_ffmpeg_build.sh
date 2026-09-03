#!/usr/bin/env bash
# Proves a downloaded BtbN/FFmpeg-Builds LGPL release actually carries what
# the worker image needs, rather than trusting the publisher's build
# scripts on their own: pins a dated tag, verifies its own checksum, and
# runs the binary through six capability/licence probes. Its raw output
# belongs in docs/experiments/10-lgpl-ffmpeg-nvenc.md, quoted verbatim --
# this script produces evidence, the record is where the evidence is kept.
#
# ffprobe/ffmpeg from BtbN publish a fresh SHA256 with every autobuild, so
# a checksum is only known after the first download of a given tag; run
# once with a placeholder value to learn the real one from the FAIL output
# below, then re-run with that value pasted in for a clean PASS.
#
# Usage: verify_ffmpeg_build.sh <dated-tag> <expected-sha256> [test-clip]
set -euo pipefail

usage() {
  cat <<'USAGE' >&2
Usage: verify_ffmpeg_build.sh <dated-tag> <expected-sha256> [test-clip]

  dated-tag        A BtbN/FFmpeg-Builds release tag shaped like
                    autobuild-YYYY-MM-DD-HH-MM (see the repository's
                    releases page). The moving "latest" label is refused --
                    a tag that can be rebuilt under the same name is not a
                    pin, and this project already pins its other binary by
                    digest for exactly that reason.
  expected-sha256   The checksum already recorded for the linux64-lgpl-9.0
                    asset under that tag. Unknown on a first run against a
                    new tag -- pass any value, the FAIL line prints the
                    real computed checksum so it can be pasted back in.
  test-clip         Optional path to a small local video file. Enables the
                    end-to-end NVENC/NVDEC probe; omitted, that probe
                    prints SKIP rather than a silent pass.
USAGE
}

if [[ $# -lt 2 ]]; then
  usage
  exit 1
fi

TAG="$1"
EXPECTED_CHECKSUM="$2"
TEST_CLIP="${3:-}"

REPO="BtbN/FFmpeg-Builds"
ASSET_SUFFIX="-linux64-lgpl-9.0.tar.xz"

# A rebuildable label is not a pin -- refuse outright rather than resolve it.
if [[ "$TAG" == "latest" ]] || ! [[ "$TAG" =~ ^autobuild-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "REFUSED: '$TAG' is not a dated, non-moving release tag." >&2
  echo "The publisher's moving 'latest' label is rebuilt under the same name" >&2
  echo "on every automated run and cannot stand in for a pin. Pass a dated" >&2
  echo "tag instead, e.g. autobuild-2026-08-31-13-27 -- see" >&2
  echo "https://github.com/${REPO}/releases" >&2
  exit 1
fi

for tool in curl sha256sum tar python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required tool '$tool' not found on PATH. Install it and re-run." >&2
    exit 1
  fi
done

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/verify_ffmpeg_build.XXXXXX")
trap 'rm -rf "$TMPDIR"' EXIT

VERDICT_LOG="$TMPDIR/verdicts.log"
: > "$VERDICT_LOG"

verdict() {
  echo "$1"
  echo "$1" >> "$VERDICT_LOG"
}

OS_KERNEL="$(uname -s)"

win_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# Runs an already-extracted binary from the release, transparently through
# a throwaway Linux container when this host cannot execute a Linux ELF
# binary directly -- ponytail: a plain debian:bookworm-slim shim, not a
# cross-compiler or a WSL dependency. On a native Linux host (the GPU
# machine this build actually ships to) this direct-execs the real binary
# and no container is involved at all.
run_bin() {
  local bin_name="$1"; shift
  if [[ "$OS_KERNEL" == Linux ]]; then
    "$EXTRACT_DIR/bin/$bin_name" "$@"
  else
    if ! command -v docker >/dev/null 2>&1; then
      echo "ERROR: this host ($OS_KERNEL) cannot run a Linux binary directly and docker is not on PATH -- install Docker Desktop or run this script on a Linux host." >&2
      exit 1
    fi
    MSYS_NO_PATHCONV=1 docker run --rm -v "$(win_path "$EXTRACT_DIR/bin"):/ff" debian:bookworm-slim "/ff/$bin_name" "$@"
  fi
}

echo "=== Step 1: resolve and download the pinned release asset ==="
RELEASE_JSON="$TMPDIR/release.json"
if ! curl -fsSL "https://api.github.com/repos/${REPO}/releases/tags/${TAG}" -o "$RELEASE_JSON"; then
  echo "ERROR: could not fetch release metadata for tag '$TAG' from the ${REPO} GitHub API." >&2
  exit 1
fi

ASSET_INFO=$(python3 -c "
import json
with open(r'$(win_path "$RELEASE_JSON")') as f:
    data = json.load(f)
for a in data.get('assets', []):
    if a['name'].endswith('$ASSET_SUFFIX'):
        print(a['name'], a['browser_download_url'])
        break
")
ASSET_NAME=$(printf '%s\n' "$ASSET_INFO" | cut -d' ' -f1)
ASSET_URL=$(printf '%s\n' "$ASSET_INFO" | cut -d' ' -f2)

if [[ -z "$ASSET_NAME" || -z "$ASSET_URL" ]]; then
  echo "ERROR: no asset ending in '$ASSET_SUFFIX' found in the '$TAG' release." >&2
  exit 1
fi

echo "asset: $ASSET_NAME"
ARCHIVE="$TMPDIR/$ASSET_NAME"
curl -fsSL -o "$ARCHIVE" "$ASSET_URL"

echo "=== Step 1: checksum ==="
COMPUTED_CHECKSUM=$(sha256sum "$ARCHIVE" | awk '{print $1}')
echo "expected sha256: $EXPECTED_CHECKSUM"
echo "computed sha256: $COMPUTED_CHECKSUM"
if [[ "${COMPUTED_CHECKSUM,,}" == "${EXPECTED_CHECKSUM,,}" ]]; then
  verdict "PASS: checksum matches the expected value"
else
  verdict "FAIL: checksum does not match -- refusing to unpack. The computed value above is what this tag's asset actually hashes to today; paste it back in as the second argument to proceed."
  exit 1
fi

echo "=== Step 2: unpack and run --version ==="
tar -xf "$ARCHIVE" -C "$TMPDIR"
EXTRACT_DIR="${ARCHIVE%.tar.xz}"
if [[ ! -d "$EXTRACT_DIR" ]]; then
  EXTRACT_DIR=$(find "$TMPDIR" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)
fi

set +e
VERSION_OUT=$(run_bin ffmpeg -version 2>&1)
VERSION_RC=$?
set -e
echo "$VERSION_OUT"
if [[ "$VERSION_RC" -eq 132 || "$VERSION_RC" -eq 139 ]]; then
  verdict "FAIL: process died by illegal instruction (exit $VERSION_RC) -- the documented signature of a build compiled for instructions this CPU does not have"
elif [[ "$VERSION_RC" -ne 0 ]]; then
  verdict "FAIL: process exited $VERSION_RC (not the illegal-instruction signature -- see the output above)"
else
  verdict "PASS: runs without illegal instruction on this CPU"
fi
if [[ "$OS_KERNEL" != Linux ]]; then
  verdict "SKIP: this run says nothing about the target no-AVX2 CPU -- this host's own CPU carries the newer vector instructions the target processor lacks; not answerable on this machine, only on the real GPU host"
fi

echo "=== Step 3: build configuration -- redistribution and capability terms ==="
BUILDCONF=$(run_bin ffmpeg -buildconf 2>&1)
echo "$BUILDCONF" | grep -Ei 'nonfree|nvenc|nvdec|cuvid|libass|cuda-nvcc|libnpp|nv-codec|enable-gpl' || true
HAS_NONFREE=$(echo "$BUILDCONF" | grep -c -- '--enable-nonfree' || true)
HAS_CUDA_NVCC=$(echo "$BUILDCONF" | grep -c -- '--enable-cuda-nvcc' || true)
HAS_LIBNPP=$(echo "$BUILDCONF" | grep -c -- '--enable-libnpp' || true)
HAS_LIBASS=$(echo "$BUILDCONF" | grep -c -- '--enable-libass' || true)
if [[ "$HAS_NONFREE" -eq 0 && "$HAS_CUDA_NVCC" -eq 0 && "$HAS_LIBNPP" -eq 0 && "$HAS_LIBASS" -ge 1 ]]; then
  verdict "PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)"
else
  verdict "FAIL: redistribution (nonfree=$HAS_NONFREE cuda-nvcc=$HAS_CUDA_NVCC libnpp=$HAS_LIBNPP libass=$HAS_LIBASS)"
fi

echo "=== Step 4: hardware acceleration methods ==="
HWACCELS=$(run_bin ffmpeg -hwaccels 2>&1)
echo "$HWACCELS"
if echo "$HWACCELS" | grep -qx 'cuda'; then
  verdict "PASS: the card's hwaccel method (cuda) is listed"
else
  verdict "FAIL: cuda hwaccel not listed"
fi

echo "=== Step 5: encoders (the card's H.264 encoder) ==="
ENCODERS=$(run_bin ffmpeg -encoders 2>&1)
echo "$ENCODERS" | grep -i nvenc || true
if echo "$ENCODERS" | grep -q 'h264_nvenc'; then
  verdict "PASS: h264_nvenc encoder listed"
else
  verdict "FAIL: h264_nvenc encoder not listed"
fi

echo "=== Step 5: decoders (the card's H.264 decode path) ==="
DECODERS=$(run_bin ffmpeg -decoders 2>&1)
echo "$DECODERS" | grep -Ei 'nvdec|cuvid' || true
if echo "$DECODERS" | grep -qE 'h264_cuvid|h264_nvdec'; then
  verdict "PASS: h264 decode path (cuvid or nvdec) listed"
else
  verdict "FAIL: h264 decode path not listed"
fi

echo "=== Step 6: filters (subtitle burn-in) ==="
FILTERS=$(run_bin ffmpeg -filters 2>&1)
echo "$FILTERS" | grep -i subtitles || true
if echo "$FILTERS" | grep -q 'subtitles'; then
  verdict "PASS: subtitles filter listed"
else
  verdict "FAIL: subtitles filter not listed"
fi

echo "=== Step 7: end-to-end encode using the card ==="
if [[ -z "$TEST_CLIP" ]]; then
  verdict "SKIP: no test clip given -- pass a small clip as the third argument to run this probe"
elif [[ "$OS_KERNEL" != Linux ]]; then
  verdict "SKIP: this host cannot pass a GPU through to the container shim used for the other probes -- run this probe on the real GPU host instead"
else
  set +e
  E2E_OUT=$(run_bin ffmpeg -hwaccel cuda -i "$TEST_CLIP" -c:v h264_nvenc -f null - 2>&1)
  E2E_RC=$?
  set -e
  echo "$E2E_OUT"
  if [[ "$E2E_RC" -eq 0 ]]; then
    verdict "PASS: end-to-end encode exited 0 -- confirm actual card activity separately via nvidia-smi during the run, this script cannot see that on its own"
  else
    verdict "FAIL: end-to-end encode exited $E2E_RC"
  fi
fi

echo "=== Summary ==="
cat "$VERDICT_LOG"

if grep -q '^FAIL:' "$VERDICT_LOG"; then
  echo "Overall: FAIL"
  exit 1
fi
echo "Overall: PASS (SKIP lines above name what this run could not attempt)"
exit 0
