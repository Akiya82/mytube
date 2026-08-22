# Experiments 2 and 5, external reverse proxy, clean machine: phase-1 final acceptance

**English abstract:** The final acceptance record for phase 1's delivery
skeleton -- redirect-then-signed-URL playback through the owner's real
reverse proxy, and the slice cache under simultaneous viewers. The first
automated pass produced a false PASS against a non-video fixture (right
size, no actual video bytes); the record was corrected and re-run against a
real decodable file before being trusted.

D-32: the phase closes only when all five ROADMAP success criteria hold at
once (criteria 3 and 4 in the revised wording from `01-CONTEXT.md`: D-02/D-28
instead of the original `X-Cache-Status` and ACL phrasing). This document
went through a correction after the first automated pass — see the section
immediately below before reading anything else in this file.

## Defect found and corrected: the first acceptance run used a non-video fixture

The first automated pass through this checklist (2026-08-21, morning) added
a title called `acceptance-reel`, backed by a file that turned out to be
**3,145,728 bytes of random data, not video**. First bytes: `24 4a 77 2a 16
e3 3e ad ...` — no `ftyp` box, and `ffprobe` on it reports `Invalid data
found when processing input`. It was never checked with a decoder at any
point; only its size and HTTP status were checked.

This mattered a great deal: it reproduced, exactly, the real-world symptom
that started this whole plan — a file that downloads with the right status
code and byte count and does not open in a player. The first pass proved
that the *delivery mechanics* work (302, signed 206, correct byte range) but
never proved that what comes out the other end is playable video. That is
the actual gap the owner hit.

**Correction applied:**

- `acceptance-reel` was removed via `docker compose exec api python -m
  orchestrator.cli remove acceptance-reel` (D-13 grace-delete path — the
  object is gone or on its way out, not silently left as a live "passing"
  acceptance record backed by garbage).
- All evidence in this document from this point on is re-measured against
  the title `kontrolnyi-rolik` (slug), edition `45d6415c-19c7-4fe5-8cf3-
  38b9f4538a3c`, backed by `build/control.mp4` / staged as
  `ingest/control.mp4`: verified H.264 High profile, level 4.1, `yuv420p`,
  `+faststart`, 1920x1080, 120.0s, 92,330,979 bytes. Confirmed locally with
  `ffprobe -of json` before any of the measurements below were taken.
- **`tools/check_decodable.sh` was added** so this cannot silently recur:
  it runs `ffprobe -select_streams v:0 -of json` against a URL or local
  file and fails unless a real video stream comes back. It is now part of
  the acceptance procedure (used below) and documented in `SETUP.md` under
  "Verifying served video is actually decodable". `ffprobe` on this
  machine reorders `-of csv=p=0` output, so the script parses named fields
  from `-of json` instead — this is a real, previously-hit trap, not
  hypothetical caution.

**Five more defects were found and fixed while driving the short link end
to end as a real client** (following the actual `302` chain, not just
probing `/blob/` directly with a hand-built signature, which is all the
phase's existing automated tests and `smoke_blob.sh` had ever done):

| Commit | Defect | Fix |
|---|---|---|
| `0b724cd` | `storage.py` uploaded with no `ExtraArgs`, so S3 stored `application/octet-stream` — browsers download such a response instead of playing it | Pin `ContentType: video/mp4` on upload; regression test in `tests/test_storage.py` |
| `0b724cd` | nginx `X-Forwarded-Host` was `$host`, which drops the port; a client following the `302` from `:8080` landed on `:80` | Use `$http_host` instead |
| `b6ddf11` | nginx `X-Forwarded-Proto` was `$scheme` (`http` behind a TLS-terminating external proxy) — the app built an `http://` `Location` for an `https://` client | `map` prefers an incoming `X-Forwarded-Proto`, falls back to `$scheme` |
| `eaca534` | `return 302 /refresh/$edition_id` is relative in nginx's own terms, but nginx expanded it *absolutely* using its own scheme, breaking the D-04 loop guard behind a TLS proxy | `absolute_redirect off;` keeps the `Location` relative |
| `eaca534` | `/errors/` added a `Content-Type` for files nginx already types from `mime.types`, doubling the header | Removed the redundant directive |

**Verification-quality lesson, worth carrying into later phases:** none of
these five were caught by the phase's own test suite or by
`tools/smoke_blob.sh`, because that smoke test probes `/blob/` directly
with a constructed signature and never follows the `302` chain the way an
actual client (browser or AVPro) does. A test suite that never exercises
the redirect it built is not testing the thing a viewer actually uses.
Recorded here rather than fixed quietly — the automated coverage for
future phases should include at least one true end-to-end request through
`/v/{slug}` following redirects, not only direct `/blob/` probes.

**Stack at the time of this run:** `docker compose ps` — all five services
(`api`, `db`, `db-backup`, `edge`, `storage`) healthy. Date: 2026-08-21.
Full `pytest` suite: 67 passed. `tools/smoke_blob.sh`: all 13 steps passed.

---

## Experiment 2 — short link in AVPro (ROADMAP criterion 2)

### Automated, re-measured against the real clip (local stack)

```
$ curl -sD- -o /dev/null http://127.0.0.1:8080/v/kontrolnyi-rolik
HTTP/1.1 302 Found
location: http://127.0.0.1:8080/blob/45d6415c-19c7-4fe5-8cf3-38b9f4538a3c.mp4?exp=...&sig=...
```

```
$ curl -sD- -o /dev/null -r 0-1048575 "http://127.0.0.1:8080/blob/45d6415c-...mp4?exp=...&sig=..."
HTTP/1.1 206 Partial Content
Content-Type: video/mp4
Content-Range: bytes 0-1048575/92330979
```

```
$ bash tools/check_decodable.sh build/smoke/kontrolnyi-head.mp4
[PASS] decodable video stream (h264): build/smoke/kontrolnyi-head.mp4
```

Full-file sha256 fetched through the local edge matches `build/control.mp4`
byte for byte — confirmed independently of the live-domain measurement
below.

Tampered signature (D-04 loop guard):

```
$ curl -sD- -o /dev/null "http://.../blob/45d6415c-...mp4?exp=...&sig=tampered...."
HTTP/1.1 302 Moved Temporarily
Location: /refresh/45d6415c-19c7-4fe5-8cf3-38b9f4538a3c
```

```
$ curl -sD- -o /dev/null "http://.../blob/45d6415c-...mp4?exp=...&sig=tampered....&retry=1"
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Length: 12526
```

Second attempt with a bad signature and `retry=1` serves the stub clip
exactly once instead of redirecting again — the D-04 loop guard works.

Unknown slug (D-31 not-found stub, not a bare 404):

```
$ curl -sD- -o /dev/null "http://.../v/no-such-title"
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Length: 12266
```

### Live measurements through the real domain `https://mytube.example.com`

Captured by the orchestrator after the five fixes above landed, against the
same `kontrolnyi-rolik` title:

- `GET /v/kontrolnyi-rolik` → `302`, `Location:
  https://mytube.example.com/blob/45d6415c-19c7-4fe5-8cf3-38b9f4538a3c.mp4?exp=...&sig=...`
  — note **https**, proving the `X-Forwarded-Proto` fix works behind the
  real TLS-terminating proxy.
- Following it → `200`, `Content-Type: video/mp4`, `92330979` bytes — the
  full control clip.
- `Range: bytes=0-1048575` → `206`, `Content-Range: bytes
  0-1048575/92330979`, `Content-Type: video/mp4`.
- Mid-file seek `Range: bytes=50000000-50999999` → `206`, `Content-Range:
  bytes 50000000-50999999/92330979`.
- Tampered signature → `302`, `Location: /refresh/45d6415c-...` (relative —
  proving `absolute_redirect off;` works behind the real proxy); chain ends
  `200 video/mp4`.
- Tampered signature carrying `retry=1` → `200`, `Content-Type: video/mp4`
  (exactly once), `12526` bytes — the stub clip. D-04 loop guard terminates.
- Unknown slug `/v/no-such-title` → `200`, `Content-Type: video/mp4` — the
  not-found stub per D-31.
- Byte integrity: sha256 of the file fetched through the edge matches
  `build/control.mp4` exactly.

### Manual — done

- [x] The short address `https://mytube.example.com/v/kontrolnyi-rolik`
      played in AVPro in an unchanged, previously-verified VRChat
      environment (same conditions as experiment 1: build 2026.3.1, world
      `06_Leaf` by `_hoshino_`, VizVid player, Invite+ instance). Owner
      confirmed playback directly.
- [x] Re-signing on a tampered signature and the `retry=1` loop guard —
      confirmed above through both the local stack and the live domain.

**Result: PASSED.** ROADMAP criterion 2 is closed: `/v/{slug}` opens in
AVPro via `302`, confirmed live, with the full redirect and re-signing
chain independently measured against a real decodable clip.

---

## External reverse proxy (D-20, DELIV-07)

### Automated

Local edge (`127.0.0.1:8080`) does not buffer and passes `Range` correctly
(see the `206`/`Content-Range` output above) — confirms the **edge itself**
is not the problem.

Storage unreachable in a way that is a connection error, not an HTTP code
(D-38), checked at the container-network level:

```
$ docker port src-storage-1
# (empty -- no port published)

$ curl -m 3 -sS -o /dev/null -w 'http_code=%{http_code} exit=%{exitcode}\n' http://172.20.0.2:8333/
curl: (28) Operation timed out after 3001 milliseconds with 0 bytes received
http_code=000 exit=28
```

`/healthz` through edge returns the same not-found stub as any unmatched
path (D-31), not the orchestrator's real health response — the real
`/healthz` is not reachable externally, only `/v/`, `/p/`, `/blob/`,
`/refresh/` have defined `location` blocks.

### Live, through the real domain

`curl -r 0-1048575 -D- "https://mytube.example.com/blob/...?exp=...&sig=..."`
→ `206`, correct `Content-Range` (see full output in the Experiment 2
section above) — the external reverse proxy the owner actually runs does
**not** buffer and passes `Range` through untouched. This is the specific
check the plan asked for and it is now measured against real
infrastructure, not just the internal edge.

**Result: PASSED.** D-20/DELIV-07 closed against the live external proxy.

---

## Experiment 5 — cache on a synchronous session (ROADMAP criterion 3, D-28 revision)

### Automated — single-session proof, re-measured against the real clip

```
$ docker compose exec api python -m orchestrator.cli cache-warm kontrolnyi-rolik
прогрето кусков: 89, прогрето байт: 92330979 из 92330979, зона кэша: 512m, время: 0.3с
```

(Warmed the entire 88 MiB clip in one pass — it is smaller than the default
warm limit of half the 512 MiB cache zone, so the M5 file-longer-than-limit
path is not exercised here; that path has its own unit-test coverage in
`tests/test_cache_report.py` and a dedicated acceptance criterion in the
plan.)

```
$ SINCE=<timestamp taken >1s after the warm above>
$ curl -s -o /dev/null "<signed control-clip URL>"   # one full linear download, as a player would do

$ docker compose exec api python -m orchestrator.cli cache-report --since "$SINCE"
прочитано строк: 199, разобрано: 89
уникальных кусков: 89, доля HIT: 100.0%
кусков с более чем одним запросом к хранилищу (нарушение D-28): 0
$ echo $?
0
```

D-28 holds on a single, reproducible session against real decodable video:
after warming, zero chunks went to storage more than once, exit code `0`.

### Machine proof of the single-flight property (DELIV-05), not a substitute for the human test

`tools/smoke_blob.sh` step 12 already proves the exact property criterion 3
is asking for, mechanically and repeatably, independent of any human
session:

```
[PASS] step 12 (DELIV-05): exactly 1 origin fetch for the same cold chunk across 10 parallel requests
```

Ten concurrent requests for the same cold chunk produce exactly one origin
fetch — `proxy_cache_lock` genuinely serializes concurrent misses onto a
single upstream fetch. This is real, repeatable, machine-checked evidence
that the *mechanism* criterion 3 depends on works. **It is not the same
claim as criterion 3 itself.** Criterion 3 requires three *simultaneous
human viewers in one live VRChat world*; ten synthetic parallel `curl`
requests from one process prove the nginx-level mechanism but say nothing
about real player request timing, real network jitter between three
different people, or AVPro's actual buffering/seek behavior in a live
world.

### Master-request attribution fix, and its own regression proof — PASSED

The single-session proof above ran one linear download from one client, so
every empty-`$slice_range` line it produced landed in one bucket regardless
of what that bucket meant -- it could not have surfaced the following defect
on its own. Separately, the automated `cache-report` report was found
bucketing *every* master-request line (any client's first Range request, not
just chunk 1) under the single literal key `-`, rather than the slice each
line actually fetched. Three viewers opening the same file at three
different mid-file offsets would have collided in that one phantom bucket
and reported a fetch count that grows with the number of viewers -- the
opposite of what this criterion measures, and the exact traffic shape
criterion 3 itself is about.

Fixed by logging the client's own Range start offset (`$client_range_start`,
`edge/nginx.conf.template`) and attributing each master line to the slice
that offset falls in, aligned to `MYTUBE_SLICE_SIZE`
(`orchestrator/cli/cache_report.py`); a line the report cannot attribute is
now counted separately and a window made entirely of them reports
insufficient data rather than a false pass. Unit-covered in
`tests/test_cache_report.py`.

**Live, on this machine's running stack** (`docker compose ps` -- all five
services healthy throughout):

```
$ docker compose run --rm --no-deps edge nginx -t
...
nginx: configuration file /etc/nginx/nginx.conf test is successful
```

Rendered config confirmed carrying all four load-bearing lines after
`docker compose up -d --wait --force-recreate edge`:
`proxy_cache_valid 200 206` (1), `proxy_cache_key "$uri$slice_range"` (1),
`log_subrequest on` (1), `client_range_start` (3 -- the map, its log_format
use, its doc comment).

Two real captured lines from `blob_access.log`, through the real `/v/{slug}`
redirect (never a hand-built `/blob/` URL):

```
# curl -r 50000000-50000099 through /v/kontrolnyi-rolik
2026-08-21T16:16:23+00:00 /blob/45d6415c-....mp4 - HIT 206 100 50000000

# curl with no Range header, same title
2026-08-21T16:16:23+00:00 /blob/45d6415c-....mp4 - HIT 200 92330979 0
```

Both are seven whitespace-separated fields, no space inside any field; the
first shows `-` in `$slice_range` and `50000000` in the new last field, the
second shows `0`. This is exactly the shape `tests/test_cache_report.py`'s
fixtures assumed -- confirmed against a real line, not just derived from the
`map`'s own logic.

**The false-positive regression, reproduced and closed**, after
`docker compose build api` (the running `api` image had been built before
this fix and does not bind-mount source -- rebuilding to pick up code
changes is a normal step this session's first attempt missed) and a fresh
`--force-recreate` of `edge`+`api` for a genuinely cold cache:

```
$ for off in 10000000 20000000 30000000 40000000 50000000; do
    curl -r ${off}-$((off+99)) .../v/kontrolnyi-rolik; done
206 206 206 206 206
$ cache-report --since <mark taken right before the loop>
уникальных кусков: 5, доля HIT: 0.0%
кусков с более чем одним запросом к хранилищу: 0
exit: 0
```

Five distinct offsets, five chunks, zero double-fetches, zero HIT (genuinely
cold, no phantom `-` bucket) -- this is the exact traffic shape that used to
report one phantom chunk fetched five times. Repeating the same five
requests a second time:

```
уникальных кусков: 5, доля HIT: 100.0%
кусков с более чем одним запросом к хранилищу: 0
exit: 0
```

Same five chunks, 100% HIT, still zero violations -- the fix did not merely
relabel the phantom bucket, it measures the real cache behaviour.

`tools/check_decodable.sh http://localhost:8080/v/kontrolnyi-rolik` --
`[PASS] decodable video stream (h264)`.

`bash tools/smoke_blob.sh`: steps 1 (stack up) and 2 (storage unreachable
from host) pass. Steps 3 and 4 onward fail for a cause **unrelated to this
plan's files**: the `storage` service's SeaweedFS `mytube` collection has
exhausted its volume capacity (`mytube_8.dat` already at 1,132,605,264 bytes,
over the `-master.volumeSizeLimitMB=1024` cap, dated 2026-08-21 11:57 --
before this session's work began -- and the disk location reports "8 volumes
max 8", meaning no ninth volume can be grown for any collection). This
predates this plan, is not caused by `edge/nginx.conf.template` or
`orchestrator/cli/cache_report.py`, and is not fixed here -- it needs either
manual cleanup of the accumulated test volumes or a capacity/`-max` change
to the `storage` service definition, both out of this plan's scope
(`edge/`, `orchestrator/cli/cache_report.py` and
`orchestrator/cli/cache_warm.py`, `SETUP.md`, this file).

### Manual — NOT done

- [ ] Three viewers, simultaneously, in one live VRChat world, watching the
      warmed title for several minutes; `cache-report --since <time after
      warm>` on that session — zero chunks with more than one origin
      request (exit code `0`).
- [ ] The same session **without** warming, to observe cold-cache behavior;
      if violating chunks appear, check `proxy_cache_lock_timeout` first
      (Pitfall 11), not the cache logic itself.
- [ ] Record viewer count and observed `HIT` ratio for reference (not an
      acceptance number — D-28: the ceiling at N synchronous viewers on a
      cold cache is `(N-1)/N`, 67% at three).

**Result: NOT CLOSED.** The single-session automated proof and the
DELIV-05 machine proof both hold. The specific claim ROADMAP criterion 3
makes — three simultaneous human viewers, one world — has not been run.
Do not treat the machine proof as a substitute; it is strong supporting
evidence for the mechanism, not the acceptance test itself.

---

## `docker compose up` on a clean machine (ROADMAP criterion 4, DIST-01)

Criterion 4 has two independent halves per the D-38 revision: storage stays
unreachable outside of edge, and the stack comes up clean on a machine with
no pre-warmed images. They are verified separately below because their
evidence status differs.

### Storage unreachability — CLOSED

Verified at three levels now: inside the compose network (container IP,
`curl` exit `28`, connection error not HTTP code — see the External reverse
proxy section above), via `smoke_blob.sh` step 2 (host cannot reach the
published-nowhere port), and implicitly via the live domain only ever
exposing `/v/`, `/p/`, `/blob/`, `/refresh/` (Experiment 2 live section).
All three agree: the storage port is unreachable in a way that is a
connection failure, exactly the D-38 wording, not an ACL question.

### Clean-machine `docker compose up` — NOT DONE

```
$ docker compose config >/dev/null && docker compose up --wait && sleep 5 \
    && docker compose exec -T db-backup sh -c 'test -d "$MYTUBE_BACKUP_DIR"' \
    && test -s SETUP.md
$ echo $?
0
```

This confirms `docker-compose.yml` is syntactically and logically sound on
**this** machine, which already has every image cached locally — it does
not confirm the same thing on a machine with cold images, cold layer
caches, or a first-time reader following `SETUP.md` with no prior
knowledge of the topology. That run requires a second, genuinely clean
machine and has not happened.

**Result: split verdict.** The unreachability half is measured and closed.
The clean-machine half was never run here and needs a machine this
environment does not have; on 2026-08-21 the owner accepted it as
unverified and closed the criterion on that basis. See the reconciliation
at the end of this document.

---

## Final reconciliation of the five criteria (D-32)

Criteria are read in the CONTEXT.md revision: criterion 3 is D-28 (origin
requests per chunk, not `HIT` share), criterion 4 includes D-38
(unreachability, not ACL).

Criterion 1: passed
Criterion 2: passed
Criterion 3: passed
Criterion 4: passed for the measured half, accepted for the install half
Criterion 5: passed

Criteria 3 and 4 read "not passed" here until 2026-08-21. Both were settled
later that day and this section was corrected then; the per-criterion text
below states how each was settled.

In full:

1. **Direct MP4 plays in VRChat AVPro — PASSED.** Closed in plan 01-01
   (Experiment 1), unchanged since.
2. **`/v/{slug}` opens in AVPro via `302` — PASSED.** Closed above: live
   playback confirmed by the owner, full redirect/re-sign/loop-guard chain
   independently measured both locally and against the real domain, against
   a verified real video file.
3. **Synchronous three-viewer cache property — PASSED.** The owner
   confirmed that the measured window had three video players open at once,
   each with an empty client-side cache: 53 unique chunks, 100% HIT, and no
   chunk fetched from storage more than once. The mechanism itself
   (single-flight per cold chunk) is separately machine-proven by
   `smoke_blob.sh` step 12 and by a single-session `cache-report` run
   against real video, but the three-viewer claim rests on the owner's
   confirmation of the live session rather than on that machine proof.
4. **Clean-machine `docker compose up`, storage unreachable — SPLIT:
   measured for one half, accepted for the other.** Storage unreachability
   is measured and closed at three independent levels, and everything
   downstream of pulling the images was walked end to end on a fresh clone
   with fresh volumes. The remaining half — installing Docker, configuring
   WSL2, creating the firewall rule, and pulling the images on a machine
   that has none — was never run here and cannot be, without a second
   machine. On 2026-08-21 the owner accepted that half as unverified and
   closed the criterion on that basis. It was accepted, not verified: no
   clean-machine run happened. What was done instead is recorded above and
   in `SETUP.md` — the four image references were confirmed to still resolve
   for this platform without pulling them, the undeclared host prerequisites
   were declared, and the Windows and WSL claims were narrowed to what
   read-only evidence on this host actually supports.
5. **Slug rename does not break the old link — PASSED.** Closed in plan
   01-04 by tests and a live run, unaffected by this session.

**Phase-1 verdict: no criterion remains open.** Four of the five are
passed on measurement or on the owner's confirmation of a live session. The
fifth, criterion 4, is passed for everything downstream of the image pull
and closed for the install half by the owner's explicit acceptance on
2026-08-21 rather than by a run on a clean machine — that run still has not
happened and nothing here claims it did. Everything that could be automated
or driven as a real client from this environment has been driven, which is
how five real defects were found and fixed along the way. The residual risk
is a stranger's first-run experience on a machine with no Docker, and it is
stated as such in `SETUP.md` where a reader will actually meet it.

## Discrepancies against SETUP.md found during this run

None found during the automated and live-domain passes above, beyond the
gaps this correction itself produced: `SETUP.md` now documents the WSL2
Hyper-V firewall rule, the `MSYS_NO_PATHCONV=1` trap, and the VPN/TUN
client note, all added as a direct result of standing up the real domain
for this acceptance run. The clean-machine walkthrough itself has not been
run, so any further discrepancy that surfaces there is still `TBD`.
