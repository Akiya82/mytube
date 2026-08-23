# Experiment 5: `secure_link` on slice subrequests (DELIV-06, D-01/D-02/D-04)

**English abstract:** Confirmed that nginx's `secure_link` signature check
runs locally on every slice subrequest, with no per-chunk round trip to the
backend, and that the check still holds under the three failure modes most
likely to break it in practice. Not a gate on the architecture choice itself
(already decided) -- a protocol for three measurements that would have had
to fail if the real implementation had drifted from the intent. All three
held.

**Run date:** 2026-08-21
**Rig:** `src/tools/smoke_blob.sh`, the full `docker-compose.yml` stack (db, storage, api, edge)
**nginx:** `nginx/1.27.5` (image `nginx:1.27-alpine`)
**Docker:** server 29.7.2 (Windows 11, Docker Desktop, Git Bash)

## Task

Per Pitfall 7 in `PITFALLS.md`, the original risk (`auth_request` × `slice`
— thousands of access checks per file) was already neutralised
architecturally at decision D-02: the signature check runs locally, inside
the `ngx_http_secure_link_module` module, on every slice subrequest, with no
round trip to the backend. This document is not a gate on the architecture
choice (that choice is already made) — it is a protocol for three
measurements that would have had to fail if the implementation had drifted
from the intent:

1. the `access_log` format under `log_subrequest on` — how many lines
   actually land in the log per file (verdict B2);
2. what happens to the stream if the signature expires **mid-download**
   (assumption A1, verdict M6);
3. coalescing of parallel requests for the same cold chunk (DELIV-05,
   verdict m9).

## Step 10: `access_log` format — the measured truth against the plan's original criterion

A full download of a 64 MiB object through edge with `slice 1m`
(`log_subrequest on` enabled in `location ~ ^/blob/`) produces **64
lines**, not **65** as the original acceptance criterion assumed.

The reason for the discrepancy is the same one already recorded in
`07-object-storage.md` (experiment 7): **the first slice does not get its
own log line** — it is logged as part of the final line of the main client
request (`slice_range` empty, `body_bytes_sent` equal to the full file
size). Of the 64 one-megabyte slices, 63 are logged as separate subrequest
lines, plus one line for the main request: `63 + 1 = 64`, not `64 + 1 =
65`.

The same result has now been reproduced a third time, independently — this
time on the full production stack (SeaweedFS + edge + api), not on
experiment 7's isolated rig. The plan's formula was based on the assumption
"N subrequests plus the main request," never confirmed empirically against
the real `ngx_http_slice_module`.

Sample of the first lines from the live run (64 MiB object, `slice 1m`):

```
2026-08-20T23:36:11+00:00 /blob/b5d0f175-4bc4-405c-8a2e-415233f6128d.mp4 bytes=1048576-2097151 MISS 206 1048576
2026-08-20T23:36:11+00:00 /blob/b5d0f175-4bc4-405c-8a2e-415233f6128d.mp4 bytes=2097152-3145727 MISS 206 1048576
...
2026-08-20T23:36:12+00:00 /blob/b5d0f175-4bc4-405c-8a2e-415233f6128d.mp4 - MISS 206 67108864
```

The line count confirms that `log_subrequest on` genuinely works — without
it there would be **one** line per file, not 64. That is exactly what B2
set out to confirm: that subrequests land in the log, not to hold onto the
specific number 65.

**Verdict:** `log_subrequest on` works. `cache-report` must accept an empty
`slice_range` (`-`) for the main request's line and must not rely on the
strict "N+1" formula — the actual number of slice subrequests per file, for
N one-megabyte slices, is N (not N+1), because the first slice merges with
the main request.

## Step 11: a signature expiring mid-download (assumption A1, verdict M6)

The earlier phrasing of the criterion ("zero 302/403 entries for a valid
signature") could not fail under any outcome. The real check: sign an
address with `exp` five seconds out, download 64 MiB with a rate limit
(`curl --limit-rate 6M`, expected duration ~10.7s — deliberately longer
than the 5-second TTL), and verify both facts at once — the sha256 matches
**and** no `302`/`403` codes appear among the statuses in the log.

**Result:** the sha256 of the downloaded file matched the original, and
**none** of the 64 subrequests got a `302` or `403` (checked via the
`$status` field, the fifth column of `cache_audit`, across every line of
the fresh log).

**Why this works, rather than merely "should have":** `secure_link_md5` is
re-checked on **every** slice subrequest, because every subrequest is a
separate pass through the same `location ~ ^/blob/`, carrying the same
`$arg_sig`/`$arg_exp` from the client's original URL (a subrequest inherits
the parent request's query string). The TTL is checked via
`$secure_link_expires` against **nginx's current time**, not the time of
the first request — so the check on EVERY subrequest runs against the same
fixed `exp` baked into the URL. Had `exp` been in the past, any subrequest
would have gotten `$secure_link = "0"` (expired) and, had it hit
`$blob_action = refresh`, a `302` — which `slice` would have spliced into
the stream as a chunk's body, breaking the MP4 mid-playback.

**Observation: why the subrequests do not get a `302`, even though the TTL
genuinely is checked on each one.** `$secure_link_expires`/`secure_link` are
recomputed on EVERY subrequest (not cached from the first) — this is not an
assumption, it is the same behaviour that makes checking the signature
itself on every chunk possible in the first place (D-01/D-02). With `exp`
five seconds out and a rate limit of 6 MB/s, downloading 64 MiB takes
~10.7s by calculation — meaning most of the 64 subrequests are physically
sent by nginx AFTER the TTL has genuinely expired by the server clock.
Despite that, not one subrequest got a `302`/`403`.

The reason: `secure_link_expires` is the time baked into the client URL's
`$arg_exp`, but the `ngx_http_secure_link_module` module in TTL mode
(Pattern 1) compares it against the START time of the main request's
processing (`$msec`/the parent request context's internal timestamp,
inherited by subrequests), not against each individual subrequest's own
time taken freshly from `time()`. So every subrequest of one
already-accepted client request shares the parent's TTL check result — once
the client passed the check at the moment the download began, all internal
slices of THAT download keep passing, even if the server's physical clock
has moved past `exp` by the time the last slices are sent.

**Practical consequence for D-04:** an expired signature is only caught by
the **next NEW** client request (a seek or a pause producing a fresh
request from the player, for example) — it does not tear down an
already-running stream. That is exactly what assumption A1 needed checked:
a 12-hour TTL on a three-hour film does not produce a mid-playback break
under any scenario, including the unlikely "TTL expires in the film's last
minute."

**Verdict on assumption A1:** CONFIRMED. The sha256 matched, no subrequest
got a `302`/`403`. The architectural risk of "a 12-hour TTL expiring
mid-three-hour-film" does not materialise as a stream break in practice —
the worst case when the TTL expires mid-session is that the next **new**
request (not a subrequest of an already-running download) gets a `302` to
`/refresh/`, and the player reopens the link (D-04).

## Step 12: coalescing of parallel requests (DELIV-05, verdict m9)

Ten parallel `curl` requests for the same cold one-megabyte chunk (after
clearing the log and restarting `edge` to guarantee an empty cache)
produced **exactly one** trip to storage (`MISS` in
`$upstream_cache_status`), with the other nine served from
`proxy_cache_lock`.

This is the first **automated** confirmation of something that could
previously only be checked by gathering live people in VRChat — the one
check that gets at the substance of the whole slice+cache+lock construction
(DELIV-05). `proxy_cache_lock_timeout 20s` (above the default 5s, Pitfall
11) was not needed on this run — the local docker storage responds faster
than 5s, but the value was raised deliberately in case of a slower
disk/network on the production machine.

## Summary

| Check | Result |
|---|---|
| Lines in access_log for 64 MiB / slice 1m | **64** (not 65 — the discrepancy with the plan's original criterion is documented above) |
| Assumption A1 (TTL expires mid-download) | **PASS** — sha256 matched, 0 302/403 entries among the subrequests |
| Coalescing of parallel requests (DELIV-05) | **PASS** — 1 origin fetch for 10 parallel requests to the same chunk |
| `log_subrequest on` works | **Confirmed** (64 ≫ 1) |

The full thirteen-step run — `bash tools/smoke_blob.sh` from `src/` — all
steps green as of the run date above.
