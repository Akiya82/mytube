# Experiment 10: a redistributable LGPL ffmpeg build with NVENC

**Run date (workstation pass):** 2026-09-03
**Run date (on-machine pass):** pending -- see "On-machine pass" below
**Rig:** `src/tools/verify_ffmpeg_build.sh`
**Environment (workstation pass):** Docker Desktop 29.7.2 (Windows 11, Git Bash) --
the downloaded binary is a Linux ELF and cannot run on Windows directly, so this pass
executed it inside a plain `debian:bookworm-slim` container, a portability shim the
script itself falls back to automatically on a non-Linux host. On the target host
(a native Linux box) the script runs the binary directly, no container involved.

## Task

The worker image needs an ffmpeg build with NVENC that this project is actually
allowed to redistribute. The obvious ready-made image for this kind of job is built
with `--enable-nonfree` and cannot be published; that flag is exactly what this
requirement exists to avoid repeating. A source-level reading of a third-party
publisher's build scripts (done separately, ahead of this document) found one
candidate whose scripts gate every non-redistributable piece out of its LGPL variant
-- but reading a build script is not the same as running the file it produces. This
experiment downloads one pinned release of that candidate, records its own checksum,
and asks the binary six direct questions rather than trusting the scripts that built
it.

## The candidate

- **Publisher:** `BtbN/FFmpeg-Builds` on GitHub (11,602 stars, actively maintained --
  the release used here is one day younger than the day this document was written)
- **Pinned tag:** `autobuild-2026-08-31-13-27` -- a dated, non-moving tag, not the
  publisher's `latest` label, which is rebuilt under the same name on every automated
  run and is refused outright by the verification script
- **Asset:** `ffmpeg-n9.0.1-11-ge47273f4d9-linux64-lgpl-9.0.tar.xz` (the `n9.0` branch
  build, matching the version already pinned for the orchestrator's own ffmpeg in
  `docs/experiments/08-ffmpeg-in-image.md`; the non-shared static variant, so the
  worker image needs no separate shared-library layer)
- **Archive checksum (SHA-256), as downloaded and verified by the script:**
  `204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf`
- **Licence, read from the archive's own `LICENSE.txt`:** GNU Lesser General Public
  License, Version 3 -- matches the `lgpl` variant name and is not the GPL text the
  `gpl` variant of the same release carries

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

This machine has neither the target's GPU nor its CPU. Four of the six questions are
answered fully by running the real binary through a Linux container on this
workstation, since they only require reading the binary's own build configuration and
its self-reported capability lists -- nothing here needs the card to be physically
present. **Two questions are not answerable on this machine**: whether the binary
survives on a processor lacking the newer vector instructions (this workstation's own
CPU has them, so a clean run here proves nothing about that specific failure mode),
and whether the card itself actually encodes anything (this workstation has no GPU at
all). Both are named `SKIP` below rather than assumed to pass, and both are exactly
what the on-machine pass exists to close.

## Raw output (workstation pass)

Invocation:

```
$ cd src && bash tools/verify_ffmpeg_build.sh autobuild-2026-08-31-13-27 \
    204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf
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
gate in the publisher's build scripts.

## Per-criterion verdict (workstation pass)

| # | Question | Verdict | Evidence |
|---|----------|---------|----------|
| 1 | Runs on a processor without the newer vector instructions | SKIP | This workstation's CPU is not the target no-AVX2 processor; a clean exit here does not answer the question |
| 2 | Lists the card's H.264 encoder | PASS | `h264_nvenc` present in `-encoders` |
| 3 | Lists the card's H.264 decode path | PASS | `h264_cuvid` present in `-decoders` |
| 4 | Carries the subtitle burn-in filter | PASS | `subtitles` present in `-filters` |
| 5 | Build configuration free of redistribution-blocking flags | PASS | `-buildconf` carries no `--enable-nonfree`/`--enable-cuda-nvcc`/`--enable-libnpp`, and carries `--enable-libass` |
| 6 | Produces output using the card on the real machine | SKIP | This workstation has no GPU; no test clip was given |

Four of six criteria pass on this machine's own evidence. The two `SKIP` rows are not
assumed -- they are exactly what the on-machine pass below has to close before this
verdict can be called complete.

## On-machine pass (GPU host)

*Not yet run. This section is filled by the next task, from the owner's own run on the
real GPU host -- do not pre-write its results here.*

The commands to run there, unchanged from the workstation invocation except for the
GPU host's own paths and the addition of a real test clip for the end-to-end probe:

```bash
# On the GPU host (a native Linux box -- the script direct-execs the binary there,
# no container involved):
cd src
bash tools/verify_ffmpeg_build.sh autobuild-2026-08-31-13-27 \
    204fc02692b11249c3e688ad18538ce2939129a1fc6abc32a6b2638a024496cf \
    /path/to/a-small-test-clip.mp4
```

While the end-to-end step runs, watch the card's own utilisation readout on that
machine (`nvidia-smi`) and note whether the encoder shows activity -- an encode that
silently fell back to the processor produces a file and proves nothing about the card.
Confirm separately that the process did not die by illegal instruction on that
specific CPU. Paste the full output back into this section, complete the two open
rows in the table above, and record the owner's explicit confirmation of the
publisher, the repository, and the tag against the release page directly.

## What happens if this fails

A failure on any one of the six criteria triggers the decision already made ahead of
this experiment: ffmpeg gets compiled from source in a multi-stage build with an
explicit configuration, instead of trusting a third-party binary. That build is its
own piece of work -- its own dependency set, its own configuration audit, its own
build time -- and does not get folded into the task that builds the worker image, which
is sized for copying a binary already proven to work, not for compiling one. Closing
that fallback in, with the exact configuration the failed criteria demand, is the
follow-up task's job once the on-machine pass reports its result.
