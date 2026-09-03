# Experiment 10: a redistributable LGPL ffmpeg build with NVENC

**Run date (workstation pass):** 2026-09-03
**Run date (on-machine pass):** 2026-09-03
**Rig:** `src/tools/verify_ffmpeg_build.sh`
**Environment (workstation pass):** Docker Desktop 29.7.2 (Windows 11, Git Bash) --
the downloaded binary is a Linux ELF and cannot run on Windows directly, so this pass
executed it inside a plain `debian:bookworm-slim` container, a portability shim the
script itself falls back to automatically on a non-Linux host. On the target host
(a native Linux box) the script runs the binary directly, no container involved.
**Environment (on-machine pass):** `ml-server`, Ubuntu 26.04 LTS, kernel 7.0.0-30,
AMD FX-8320E (no AVX2 -- the specific CPU criterion 1 exists to catch), NVIDIA RTX
3050 8 GiB, driver `nvidia-driver-595-open` 595.84, CUDA 13.2. Three other GPU
containers (ollama, whisper, reranker) share the card.

## Task

The worker image needs an ffmpeg build with NVENC that this project is actually
allowed to redistribute. The obvious ready-made image for this kind of job is built
with `--enable-nonfree` and cannot be published; that flag is exactly what this
requirement exists to avoid repeating. A source-level reading of a third-party
publisher's build scripts (done separately, ahead of this document) found one
candidate whose scripts gate every non-redistributable piece out of its LGPL variant
-- but reading a build script is not the same as running the file it produces. This
experiment downloads pinned releases of that candidate, records each one's own
checksum, and asks the binary six direct questions rather than trusting the scripts
that built it.

**The on-machine pass changed the outcome the workstation pass alone would have
suggested.** The `n9.0` branch build, initially the leading candidate, passes five
of six criteria including the redistribution check -- but fails the end-to-end
encode on this specific host because its NVENC implementation requires a newer
driver than the host runs. The `n8.1` branch build under the same publisher and the
same dated tag passes all six, including the end-to-end encode with confirmed card
activity. The approved artifact is the `n8.1` build, not `n9.0`.

## The candidate (approved)

- **Publisher:** `BtbN/FFmpeg-Builds` on GitHub (11,602 stars, actively maintained --
  the release used here is one day younger than the day this document was written)
- **Pinned tag:** `autobuild-2026-08-31-13-27` -- a dated, non-moving tag, not the
  publisher's `latest` label, which is rebuilt under the same name on every automated
  run and is refused outright by the verification script. `2026-08-31` is a
  month-end build, which this publisher's own retention policy keeps for two years
  rather than the fourteen days it keeps daily builds -- see "How to re-verify" below.
- **Asset:** `ffmpeg-n8.1.2-50-g1a748fe2cd-linux64-lgpl-8.1.tar.xz` (the `n8.1`
  branch build, the non-shared static variant, so the worker image needs no separate
  shared-library layer)
- **Archive checksum (SHA-256), as downloaded and verified by the script:**
  `7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee`
- **Licence, matching the `lgpl` variant name:** built with `--enable-version3` and
  none of `--enable-nonfree`/`--enable-cuda-nvcc`/`--enable-libnpp`, confirmed
  directly from `-buildconf` below, same discipline experiment 8 already used for
  the orchestrator's own ffmpeg pin

## The candidate rejected on this host: `n9.0`

- **Same publisher, same pinned tag** (`autobuild-2026-08-31-13-27`)
- **Asset:** `ffmpeg-n9.0.1-11-ge47273f4d9-linux64-lgpl-9.0.tar.xz`
- **Archive checksum (SHA-256):**
  `204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf`
- **Why it was tried first:** it matches the version branch already pinned for the
  orchestrator's own ffmpeg in `docs/experiments/08-ffmpeg-in-image.md`, so using the
  same branch in both images looked like the simpler choice going in.
- **Why it was rejected:** its NVENC implementation refuses to open on this host at
  encode time -- `Driver does not support the required nvenc API version. Required:
  13.1 Found: 13.0` / `The minimum required Nvidia driver for nvenc is 610.00 or
  newer`. `nvidia-driver-610` has no candidate in the Ubuntu 26.04 repositories this
  host draws from, and the driver is not to be upgraded outside a repository release
  (a host reboot on a card shared by three other running GPU containers). This is a
  driver ceiling, not a defect in the `n9.0` build itself -- see the closing note
  below for when this line becomes usable again.

## The six questions this experiment answers

| # | Question | Pass bar |
|---|----------|----------|
| 1 | Does the binary run at all on a processor without the newer vector instructions? | No illegal-instruction death (exit 132/139) on the target no-AVX2 host |
| 2 | Does it list the card's H.264 encoder? | `h264_nvenc` present in `-encoders` |
| 3 | Does it list the card's H.264 decode path? | `h264_cuvid` or `h264_nvdec` present in `-decoders` |
| 4 | Does it carry the subtitle burn-in filter? | `subtitles` present in `-filters` |
| 5 | Does its build configuration name any licence flag that would block redistribution? | No `--enable-nonfree`, no `--enable-cuda-nvcc`, no `--enable-libnpp` in `-buildconf`; `--enable-libass` present |
| 6 | Does it actually produce output using the card on the real machine? | An end-to-end `-hwaccel cuda ... -c:v h264_nvenc` run exits 0, with `nvidia-smi` confirming encoder activity during the run |

This is the shape `src/tools/verify_ffmpeg_build.sh` interrogates the binary with, one
delimited block and one `PASS`/`FAIL`/`SKIP` line per step, ending in a summary and a
non-zero exit if anything failed outright.

## What this workstation could answer, and what it could not

This machine has neither the target's GPU nor its CPU. Four of the six questions were
answerable by running the real `n9.0` binary through a Linux container on this
workstation during the workstation pass, since they only require reading the
binary's own build configuration and its self-reported capability lists -- nothing
here needs the card to be physically present. **Two questions were not answerable on
this machine**: whether the binary survives on a processor lacking the newer vector
instructions (this workstation's own CPU has them, so a clean run here proves nothing
about that specific failure mode), and whether the card itself actually encodes
anything (this workstation has no GPU at all). Both `SKIP` outcomes below are exactly
what the on-machine pass closed -- and closing them is what changed the candidate
from `n9.0` to `n8.1`.

## Raw output (workstation pass, `n9.0`, superseded)

Invocation:

```
$ cd src && bash tools/verify_ffmpeg_build.sh autobuild-2026-08-31-13-27 \
    204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf "" 9.0
```

```
=== Step 1: resolve and download the pinned release asset ===
asset: ffmpeg-n9.0.1-11-ge47273f4d9-linux64-lgpl-9.0.tar.xz
=== Step 1: checksum ===
expected sha256: 204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf
computed sha256: 204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf
PASS: checksum matches the expected value
=== Step 2: unpack and run --version ===
ffmpeg version n9.0.1-11-ge47273f4d9-20260831 Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 15.2.0 (crosstool-NG 1.28.0.23_185f348)
[... version banner omitted, full configuration string quoted below ...]
Exiting with exit code 0
PASS: runs without illegal instruction on this CPU
SKIP: this run says nothing about the target no-AVX2 CPU -- this host's own CPU
carries the newer vector instructions the target processor lacks; not answerable on
this machine, only on the real GPU host
=== Step 3: build configuration -- redistribution and capability terms ===
    --enable-libass
PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)
=== Step 4: hardware acceleration methods ===
Hardware acceleration methods:
cuda
vaapi
qsv
drm
opencl
vulkan
amf
PASS: the card's hwaccel method (cuda) is listed
=== Step 5: encoders (the card's H.264 encoder) ===
 V....D av1_nvenc            NVIDIA NVENC av1 encoder (codec av1)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
PASS: h264_nvenc encoder listed
=== Step 5: decoders (the card's H.264 decode path) ===
 V..... h264_cuvid           Nvidia CUVID H264 decoder (codec h264)
 V..... hevc_cuvid           Nvidia CUVID HEVC decoder (codec hevc)
[... other cuvid decoders omitted ...]
PASS: h264 decode path (cuvid or nvdec) listed
=== Step 6: filters (subtitle burn-in) ===
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 .. subtitles         V->V       Render text subtitles onto input video using the libass library.
PASS: subtitles filter listed
=== Step 7: end-to-end encode using the card ===
SKIP: no test clip given -- pass a small clip as the third argument to run this probe
=== Summary ===
PASS: checksum matches the expected value
PASS: runs without illegal instruction on this CPU
SKIP: this run says nothing about the target no-AVX2 CPU [...]
PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)
PASS: the card's hwaccel method (cuda) is listed
PASS: h264_nvenc encoder listed
PASS: h264 decode path (cuvid or nvdec) listed
PASS: subtitles filter listed
SKIP: no test clip given -- pass a small clip as the third argument to run this probe
Overall: PASS (SKIP lines above name what this run could not attempt)
```

The full `-buildconf` configuration string, quoted verbatim, filtered for every term
the redistribution criterion turns on (none of the disqualifying flags appear; every
required one does):

```
--enable-version3 --disable-debug [...] --enable-fontconfig --enable-libharfbuzz
--enable-libfreetype --enable-libfribidi [...] --enable-ffnvcodec --enable-cuda-llvm
[...] --enable-libass [...] --disable-libx264 --disable-libx265 [...]
```

No `--enable-nonfree`, no `--enable-cuda-nvcc`, no `--enable-libnpp` anywhere in the
full string (checked by the script with an exact-flag grep, not a substring match that
could be fooled by a similarly-named flag). `--enable-cuda-llvm` is present and is not
one of the three disqualifying flags this criterion checks for -- it compiles CUDA
kernels through LLVM rather than NVIDIA's own `nvcc` toolchain and carries no licence
gate in the publisher's build scripts. This paragraph applies equally to the `n8.1`
build below, whose `-buildconf` carries the same set of flags for the same reason.

This workstation pass alone read as a clean `PASS` on every criterion it could answer.
The on-machine pass below is what actually found the driver ceiling this workstation
had no way to see.

## On-machine pass (GPU host)

Both builds were downloaded and run directly on `ml-server` (no container, a native
Linux host) -- first `n9.0`, which failed criterion 6, then `n8.1` under the same
pinned tag, which passed every criterion.

### `n9.0` -- rejected

Invocation:

```
$ cd src && bash tools/verify_ffmpeg_build.sh autobuild-2026-08-31-13-27 \
    204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf \
    /tmp/nvenc-probe-clip.mp4 9.0
```

```
=== Step 1: resolve and download the pinned release asset ===
asset: ffmpeg-n9.0.1-11-ge47273f4d9-linux64-lgpl-9.0.tar.xz
=== Step 1: checksum ===
expected sha256: 204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf
computed sha256: 204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf
PASS: checksum matches the expected value
=== Step 2: unpack and run --version ===
ffmpeg version n9.0.1-11-ge47273f4d9-20260831 Copyright (c) 2000-2026 the FFmpeg developers
[... version banner omitted ...]
Exiting with exit code 0
PASS: runs without illegal instruction on this CPU
=== Step 3: build configuration -- redistribution and capability terms ===
    --enable-libass
PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)
=== Step 4: hardware acceleration methods ===
Hardware acceleration methods:
cuda
vaapi
qsv
drm
opencl
vulkan
amf
PASS: the card's hwaccel method (cuda) is listed
=== Step 5: encoders (the card's H.264 encoder) ===
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
PASS: h264_nvenc encoder listed
=== Step 5: decoders (the card's H.264 decode path) ===
 V..... h264_cuvid           Nvidia CUVID H264 decoder (codec h264)
PASS: h264 decode path (cuvid or nvdec) listed
=== Step 6: filters (subtitle burn-in) ===
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 .. subtitles         V->V       Render text subtitles onto input video using the libass library.
FAIL: subtitles filter not listed
=== Step 7: end-to-end encode using the card ===
Stream mapping:
  Stream #0:0 -> #0:0 (h264 (native) -> h264 (h264_nvenc))
  Stream #0:1 -> #0:1 (aac (native) -> pcm_s16le (native))
[h264_nvenc @ 0x58c59c880440] Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0
[h264_nvenc @ 0x58c59c880440] The minimum required Nvidia driver for nvenc is 610.00 or newer
[vost#0:0/h264_nvenc @ 0x58c59c880180] [enc:h264_nvenc @ 0x58c59c824300] Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
[vf#0:0 @ 0x58c59c880800] Error sending frames to consumers: Function not implemented
[vost#0:0/h264_nvenc @ 0x58c59c880180] [enc:h264_nvenc @ 0x58c59c824300] Could not open encoder before EOF
[out#0/null @ 0x58c59c879240] Nothing was written into output file, because at least one of its streams received no packets.
frame=    0 fps=0.0 q=0.0 Lsize=       0KiB time=00:00:00.34 bitrate=   0.0kbits/s speed=2.85x elapsed=0:00:00.12
Conversion failed!
FAIL: end-to-end encode exited 218
=== Summary ===
PASS: checksum matches the expected value
PASS: runs without illegal instruction on this CPU
PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)
PASS: the card's hwaccel method (cuda) is listed
PASS: h264_nvenc encoder listed
PASS: h264 decode path (cuvid or nvdec) listed
FAIL: subtitles filter not listed
FAIL: end-to-end encode exited 218
Overall: FAIL
```

`nvidia-smi dmon -s u -d 1` sampled around the failed encode showed no encoder or
decoder activity (0% for every sample) -- consistent with the encoder failing to
open at all rather than running and being slow.

**The `FAIL: subtitles filter not listed` line above is a defect in the verification
script, not in the binary** -- see "Script defect found and fixed" below. The raw
output block two lines above it shows the `subtitles` filter present and correctly
described. The real, binary-level reason `n9.0` is rejected is the end-to-end
encoder failure: this host's driver (595.84) reports NVENC API version 13.0, and this
`n9.0` build's NVENC implementation requires 13.1, available only from driver 610.00
onward -- not present in this host's package repositories.

### `n8.1` -- approved, script fix applied

The `FAIL: subtitles filter not listed` false negative above was root-caused between
the two on-machine runs (see "Script defect found and fixed") and fixed in
`src/tools/verify_ffmpeg_build.sh` before this run. This is the fixed script's own
unedited output.

Invocation:

```
$ cd src && bash tools/verify_ffmpeg_build.sh autobuild-2026-08-31-13-27 \
    7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee \
    /tmp/nvenc-probe-clip.mp4
```

(`8.1` is the script's own default release line as of this fix -- see the script's
`--help` usage line -- so the fourth argument is omitted here.)

```
=== Step 1: resolve and download the pinned release asset ===
asset: ffmpeg-n8.1.2-50-g1a748fe2cd-linux64-lgpl-8.1.tar.xz
=== Step 1: checksum ===
expected sha256: 7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee
computed sha256: 7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee
PASS: checksum matches the expected value
=== Step 2: unpack and run --version ===
ffmpeg version n8.1.2-50-g1a748fe2cd-20260831 Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 15.2.0 (crosstool-NG 1.28.0.23_185f348)
configuration: --prefix=/ffbuild/prefix [...] --enable-version3 --disable-debug [...]
--enable-ffnvcodec --enable-cuda-llvm [...] --enable-libass [...] --disable-libx264
--disable-libx265 [...]
libavutil      60. 26.102 / 60. 26.102
libavcodec     62. 28.102 / 62. 28.102
[... other library version lines omitted ...]
Exiting with exit code 0
PASS: runs without illegal instruction on this CPU
=== Step 3: build configuration -- redistribution and capability terms ===
    --enable-libass
PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)
=== Step 4: hardware acceleration methods ===
Hardware acceleration methods:
cuda
vaapi
qsv
drm
opencl
vulkan
amf
PASS: the card's hwaccel method (cuda) is listed
=== Step 5: encoders (the card's H.264 encoder) ===
 V....D av1_nvenc            NVIDIA NVENC av1 encoder (codec av1)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
PASS: h264_nvenc encoder listed
=== Step 5: decoders (the card's H.264 decode path) ===
 V..... av1_cuvid            Nvidia CUVID AV1 decoder (codec av1)
 V..... h264_cuvid           Nvidia CUVID H264 decoder (codec h264)
 V..... hevc_cuvid           Nvidia CUVID HEVC decoder (codec hevc)
[... other cuvid decoders omitted ...]
PASS: h264 decode path (cuvid or nvdec) listed
=== Step 6: filters (subtitle burn-in) ===
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 .. subtitles         V->V       Render text subtitles onto input video using the libass library.
PASS: subtitles filter listed
=== Step 7: end-to-end encode using the card ===
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/nvenc-probe-clip.mp4':
  Duration: 00:00:04.00, start: 0.000000, bitrate: 2876 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1280x720 [SAR 1:1 DAR 16:9], 2795 kb/s, 30 fps
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, mono, fltp, 69 kb/s
Stream mapping:
  Stream #0:0 -> #0:0 (h264 (native) -> h264 (h264_nvenc))
  Stream #0:1 -> #0:1 (aac (native) -> pcm_s16le (native))
Output #0, null, to 'pipe:':
  Stream #0:0(und): Video: h264 (Main), nv12(tv, progressive), 1280x720 [SAR 1:1 DAR 16:9], q=2-31, 2000 kb/s, 30 fps
    Metadata:
      encoder         : Lavc62.28.102 h264_nvenc
  Stream #0:1(und): Audio: pcm_s16le, 44100 Hz, mono, s16, 705 kb/s
[out#0/null @ 0x5fe6c94f4ac0] video:1294KiB audio:346KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
frame=  120 fps=0.0 q=33.0 Lsize=N/A time=00:00:03.90 bitrate=N/A speed=10.6x elapsed=0:00:00.36
PASS: end-to-end encode exited 0 -- confirm actual card activity separately via nvidia-smi during the run, this script cannot see that on its own
=== Summary ===
PASS: checksum matches the expected value
PASS: runs without illegal instruction on this CPU
PASS: redistribution (no nonfree/cuda-nvcc/libnpp flags, libass present)
PASS: the card's hwaccel method (cuda) is listed
PASS: h264_nvenc encoder listed
PASS: h264 decode path (cuvid or nvdec) listed
PASS: subtitles filter listed
PASS: end-to-end encode exited 0 -- confirm actual card activity separately via nvidia-smi during the run, this script cannot see that on its own
Overall: PASS (SKIP lines above name what this run could not attempt)
```

`nvidia-smi dmon -s u -d 1` sampled for the duration of the end-to-end step, one
sample per second: peak encoder utilisation **74%**, peak decoder utilisation
**48%**, both nonzero across multiple consecutive samples -- real card activity, not
a silent CPU fallback that happened to still produce a file. A second run using CPU
decode into `h264_nvenc` (the shape this project's transcode path actually uses when
the source codec is one the card cannot decode) also exited 0 with no illegal
instruction.

## Script defect found and fixed

Before this record could close, the `n9.0` on-machine run above surfaced a false
`FAIL` on criterion 4 (subtitle filter) that the raw output on the very same line
contradicts. Root cause: `src/tools/verify_ffmpeg_build.sh` ran
`echo "$FILTERS" | grep -q 'subtitles'` under `set -o pipefail`. `grep -q` exits as
soon as it finds its first match and closes its end of the pipe; `echo`, still
writing, receives `SIGPIPE` and exits 141. Under `pipefail`, that 141 -- not `grep`'s
own successful `0` -- becomes the pipeline's reported exit status, so the `if` took
the `FAIL` branch despite the filter being listed one line above it. The same
pattern existed in three other checks (criteria 2, 3, and the hwaccel check in
criterion 5's neighbourhood).

**Fix:** every `echo "$VAR" | grep -q ...` was replaced with a here-string,
`grep -q ... <<<"$VAR"`, which tests the pattern with no pipe and nothing to receive
a `SIGPIPE`. The `n8.1` on-machine run above used the fixed script and shows the
correct `PASS` for criterion 4.

**Secondary fix, same script:** the asset filename was previously hardcoded to the
`n9.0`/`lgpl-9.0` suffix. The script now takes an optional fourth argument,
`[ffmpeg-line]`, shaped like `N.N` (refused otherwise), defaulting to `8.1` -- the
line this record approves. Passing `9.0` explicitly reproduces the rejected run
above; passing no fourth argument reproduces the approved one.

## Per-criterion verdict (final, `n8.1`)

| # | Question | Verdict | Evidence |
|---|----------|---------|----------|
| 1 | Runs on a processor without the newer vector instructions | PASS | Ran to completion on `ml-server`'s AMD FX-8320E (no AVX2) with exit 0, both for `-version` and for the end-to-end encode |
| 2 | Lists the card's H.264 encoder | PASS | `h264_nvenc` present in `-encoders` |
| 3 | Lists the card's H.264 decode path | PASS | `h264_cuvid` present in `-decoders` |
| 4 | Carries the subtitle burn-in filter | PASS | `subtitles` present in `-filters` (see "Script defect found and fixed" for why the first on-machine run misreported this) |
| 5 | Build configuration free of redistribution-blocking flags | PASS | `-buildconf` carries no `--enable-nonfree`/`--enable-cuda-nvcc`/`--enable-libnpp`, and carries `--enable-libass` |
| 6 | Produces output using the card on the real machine | PASS | End-to-end `-hwaccel cuda ... -c:v h264_nvenc` exited 0; `nvidia-smi dmon` showed peak encoder utilisation 74%, peak decoder utilisation 48% during the run |

All six criteria pass for the `n8.1` line on the real GPU host. The `n9.0` line
fails criterion 6 on this host's driver alone -- see "The candidate rejected on this
host" above.

## What the image must use

This is what the image must use, downloaded and verified by the worker image's
Dockerfile at build time as a step whose failure stops the build, not a note somebody
is trusted to repeat by hand:

```
Repository:  BtbN/FFmpeg-Builds
Tag:         autobuild-2026-08-31-13-27
Asset:       ffmpeg-n8.1.2-50-g1a748fe2cd-linux64-lgpl-8.1.tar.xz
SHA-256:     7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee
```

The `n9.0` line and its checksum above are rejected evidence, kept in this record for
the reasoning behind the rejection -- they must not be copied into any Dockerfile or
compose file.

## When the `n9.0` line becomes usable

If `ml-server`'s driver is ever upgraded to 610.00 or newer -- currently blocked by
`nvidia-driver-610` having no candidate in this host's Ubuntu 26.04 repositories, and
by the upgrade requiring a reboot of a card three other running containers share --
the `n9.0` line's end-to-end criterion should be re-run before switching back to it.
Nothing else about `n9.0` failed; a driver upgrade alone might clear criterion 6. This
is not scheduled work, only a note for whoever next changes the host's driver.

## How to re-verify

`2026-08-31` is a month-end build under this publisher's retention policy, kept for
roughly two years rather than the fourteen days ordinary daily builds get -- so this
pin is expected to keep resolving into 2028. When it eventually ages out, or when a
newer month-end build is preferred: pick a fresh dated `autobuild-YYYY-MM-DD-HH-MM`
tag from `https://github.com/BtbN/FFmpeg-Builds/releases`, run
`verify_ffmpeg_build.sh <tag> <placeholder>` once to learn the real checksum from its
own `FAIL` line, then re-run with that checksum pasted in for a clean `PASS`. Append
the new run to this record as a dated addendum rather than editing this pass away --
the rejected `n9.0` run above is exactly this kind of evidence and stays for the same
reason.

## Owner confirmation

**Date:** 2026-09-03
**Outcome:** approved, with the amendment that the evidence itself forced -- `n8.1`,
not the originally-expected `n9.0`.

The owner ran the original script on `ml-server` against `n9.0`, watched
`nvidia-smi` during the failed encode (no encoder activity, consistent with the
driver-ceiling error above), confirmed the process did not die by illegal
instruction, and opened `BtbN/FFmpeg-Builds`'s repository and release page for tag
`autobuild-2026-08-31-13-27` to confirm the repository, the tag, and the asset name
against what this record claims -- no substitution found. Given the driver ceiling
on `n9.0`, the owner directed a second run against `n8.1` under the same tag and
publisher, which passed the end-to-end criterion with confirmed card activity, and
approved that artifact and its publisher for the worker image.

The plan that builds the worker image may pin `BtbN/FFmpeg-Builds` tag
`autobuild-2026-08-31-13-27`, asset `ffmpeg-n8.1.2-50-g1a748fe2cd-linux64-lgpl-8.1.tar.xz`,
checksum `7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee`, as recorded
above.
