# Experiment 8: pinning the ffmpeg binary source and the two new PyPI packages

**Run date:** 2026-08-22
**Rig:** `docker buildx imagetools inspect`, `docker create`/`docker export`, `docker run`,
`pip index versions`, run directly against the public registries from this machine
**Environment:** Docker Desktop 29.7.2 (Windows 11, Git Bash)

## Task

The compatibility remux (`ASIS-01`/`ASIS-02`) needs `ffmpeg`/`ffprobe` inside the `api`
image, which is `python:3.13-slim` today with neither binary. RESEARCH.md named
`mwader/static-ffmpeg` as the source, found by web search and never resolved to a digest
(assumption A1). This document resolves that image to an immutable digest, records where
its binaries actually live, and records the provenance of the two new PyPI packages
(`jinja2`, `python-multipart`) this phase adds — so a reader who rebuilds the image a
year from now can tell where every third-party artifact came from and why.

Nothing is installed or edited here. `src/Dockerfile` and `src/pyproject.toml` are
untouched by this document.

## Binary source: `mwader/static-ffmpeg`

### Resolving the moving tag to a digest

`docker buildx imagetools inspect` was run against `latest` and the three most recent
explicit version tags the registry lists (`9.0`, `8.1.2`, `8.1.1`; confirmed via the
Docker Hub tags API ordered by `last_updated` — `9.0` is the newest explicit version tag
published):

| Tag inspected | Manifest-list digest | `linux/amd64` digest |
|---|---|---|
| `latest` | `sha256:78ebc8cc0368a109db21961a14a4e890a7b1ccafb373a1b3109f0be7fcec8171` | `sha256:5fbcaa5e07dde87438148ef19c61d40133a60227da684264079bb4219c42ac45` |
| `9.0` | `sha256:b90574a4e2ae62b763c39c384526689e7eb435da6398f4fb3f6c3f1c6a14ce33` | `sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36` |
| `8.1.2` | `sha256:33f770f812cbfc3de96c547157fc9faf8bd95a36481753439ffa761045167585` | `sha256:3bfa407c614a29a4535f1e3220fd9f6bc9cd7c25483036962e3c8ff711b56e01` |
| `8.1.1` | `sha256:735f84b905e00d5c618b667f0b053f83b1096f5fc404c607e6134bf2275a0e0a` | `sha256:b7bff94c951b543b289ca3cf0a04d147f385fda78017e159a9ac0555c294049b` |

`latest`'s `linux/amd64` digest does not match `9.0`'s — `latest` was last pushed
2026-08-14 while `9.0` carries a different build, so the two tags are not interchangeable
today and `latest` cannot be treated as a stand-in for the newest explicit version.

**Chosen reference for plan 02's `COPY --from=`:**

```
mwader/static-ffmpeg:9.0@sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36
```

The explicit `9.0` tag is kept alongside the digest purely as a human-readable label —
the digest is what makes the reference immutable per the project's own
`optimizing-dockerfiles` skill (rule 5: pin a digest, not a moving tag). `linux/amd64` is
the only platform relevant here; the `api` image is not built for `arm64`.

### Binary paths and version, verified by a real listing

The image has no shell (`sh` is absent — `docker run --entrypoint sh ... ls` fails with
`exec: "sh": executable file not found in $PATH`), consistent with it being a
scratch-based image carrying only the two static binaries plus documentation. Listing was
done instead with `docker create` + `docker export` piped to `tar -tv`, and each binary
was run directly with `--entrypoint`:

```
$ docker export <container-from-the-pinned-digest> | tar -tv | grep -E '/(ffmpeg|ffprobe)$'
-rwxr-xr-x 0/0       140394784 2026-08-04 17:02 ffmpeg
-rwxr-xr-x 0/0       140173536 2026-08-04 17:02 ffprobe
```

Both binaries live at the image root: **`/ffmpeg`** and **`/ffprobe`** — no `/usr/bin`,
no subdirectory. The image's own `Entrypoint` (from `docker inspect`) is `["/ffmpeg"]`,
confirming the same path independently.

```
$ docker run --rm mwader/static-ffmpeg:9.0@sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36 -version
ffmpeg version 9.0 Copyright (c) 2000-2026 the FFmpeg developers
```

```
$ docker run --rm --entrypoint /ffprobe mwader/static-ffmpeg:9.0@sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36 -version
ffprobe version 9.0 Copyright (c) 2007-2026 the FFmpeg developers
```

Both binaries run and report the version the tag promises. Plan 02's Dockerfile stage
should copy from the paths above, not assume a Debian-style `/usr/bin/ffmpeg`.

### Licence posture

The build's own `-version` output shows `--enable-gpl --enable-version3` and no
`--enable-nonfree` flag anywhere in the printed configuration string (checked by grepping
the full `-version` output for the literal word `nonfree`, no match). This phase re-encodes
audio to AAC and copies video with plain CPU instructions — no NVENC, no nonfree CUDA
flags are ever passed to this binary — so the separate LGPL/nonfree-CUDA build problem
already logged against the GPU worker (`DIST-03` in STATE.md, needs a self-built LGPL
ffmpeg with NVENC before that image can be published) does not apply here. This image is
GPLv3, not LGPL, and is invoked as a separate process via `subprocess` — the orchestrator
never links against it — so GPL's copyleft-on-linking clause has nothing to attach to;
running a GPL binary as a subprocess of a non-GPL program is ordinary, widely-practiced
use and carries none of the redistribution obligations that would trigger if the binary
were statically or dynamically linked into the orchestrator itself.

### Fallback if this image cannot be used

If the pinned digest above stops resolving, or the entrypoint/binaries are not where this
document says once plan 02 actually builds against it, plan 02 installs `ffmpeg` through
the base image's package manager instead:

```dockerfile
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

This is a larger image (Debian's `ffmpeg` package pulls its full shared-library closure)
and ties the pinned ffmpeg version to whatever Debian bookworm ships rather than a version
this project chose deliberately — but distribution size is not this phase's job, only
correctness is, and this path is not blocked on anything external. If plan 02 has to take
this fallback, it must add one line to this document recording that the fallback was
taken and why.

## The two new PyPI packages

| Package | Resolved version | Project URL | Source repository | Reason taken |
|---|---|---|---|---|
| `jinja2` | `3.1.6` (`pip index versions jinja2`, run 2026-08-22) | https://pypi.org/project/jinja2/ | https://github.com/pallets/jinja | The templating engine FastAPI's own documentation names for `fastapi.templating.Jinja2Templates` — no alternative considered, this is the one FastAPI expects |
| `python-multipart` | `0.0.32` (`pip index versions python-multipart`, run 2026-08-22) | https://pypi.org/project/python-multipart/ | https://github.com/Kludex/python-multipart | The exact package FastAPI's own documentation names as required before any route may declare a `Form(...)`/`UploadFile` field — FastAPI raises a runtime error at request time without it |

Both version numbers were re-verified live against PyPI in this session (`pip index
versions <name>`) rather than trusted from the earlier research pass; both came back
identical to what RESEARCH.md already recorded on 2026-08-21, so nothing moved in the
intervening day.

**On the `SUS` verdicts:** the automated package-legitimacy checker flagged both packages
`SUS` with the reason `unknown-downloads` — it has no download-count signal for PyPI in
this environment. This is the same signal-absence false positive the previous phase
already hit and recorded (`STATE.md`, plan 01-01: the checker returned false `SUS` on all
seven packages it first checked, purely for lacking a download-count feed, not for any
finding about the packages themselves). It is not evidence against either package, and it
is not being treated as one — it is being written down so the record states plainly why a
`SUS` tag sits next to two packages that are, in fact, unremarkable.

## What plan 02 pastes where

- `Dockerfile` builder stage: `COPY --from=mwader/static-ffmpeg:9.0@sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36 /ffmpeg /usr/local/bin/ffmpeg` (and the same for `/ffprobe`), or the fallback `apt-get install` block above if the digest fails to resolve at build time.
- `pyproject.toml`: add `jinja2` and `python-multipart` as dependencies, version-unpinned or pinned at `3.1.6`/`0.0.32` per the plan's own preference — this document only establishes that both are legitimate and current as of the date above.

## Owner confirmation

_Recorded once the owner responds to the checkpoint below — see the next task in this
plan._
