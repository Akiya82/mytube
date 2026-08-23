# Experiment 7: choosing the object storage backend (DIST-02)

**English abstract:** Compared Garage and SeaweedFS as the self-hosted S3
backend, both driven through the same nginx `slice` setup, on two required
criteria: sha256 integrity through the full delivery chain, and anonymous
unsigned Range GET from inside the network. Garage failed the anonymous
Range criterion; SeaweedFS passed both. Verdict: SeaweedFS
(`chrislusf/seaweedfs:3.97`), the image/tag/parameters this document
records being what the stack actually runs.

**Run date:** 2026-08-21
**Rig:** `src/tools/experiment07_storage.sh`
**Environment:** Docker Desktop 29.7.2 (Windows 11, Git Bash), network `mytube-exp07`, every rig object is temporary (see `## Cleanup` below)

## Task

Upstream `minio/minio` is archived, and official Community Edition images
have not been published since October 2025. D-17 requires comparing
**Garage** and **SeaweedFS** through the same nginx with `slice`, rather
than picking one by default. This document's outcome is a concrete image,
tag and parameter set, dropped straight into `docker-compose.yml` without
re-opening the discussion.

## Image versions (pinned tags, not `latest`)

| Candidate | Image | Tag | Size |
|---|---|---|---|
| Garage | `dxflrs/garage` | `v2.3.0` | 27.2 MiB |
| SeaweedFS | `chrislusf/seaweedfs` | `3.97` | 63.4 MiB |

The rig's S3 client is `amazon/aws-cli:2.27.50` (a pinned tag), run as a
container on the `mytube-exp07` network; `nginx:1.27-alpine` is the same
version that goes into this delivery work's `docker-compose.yml`.

## Selection rule (per the plan)

Two conditions are mandatory — a candidate that fails either one is
disqualified:

1. **Range through `slice` produced a matching sha256.**
2. **An anonymous, unsigned Range GET from inside the network returns
   `206`** (D-38).

Beyond that — preferences: (3) lower TTFB on a cold chunk, (4) smaller
image. If both candidates fail any mandatory condition, the fallback is
the pinned `minio/minio:RELEASE.2025-04-22T22-12-26Z`.

## Results table

Both candidates were run through the `experiment07_storage.sh` rig
(invoked as `cd src && bash tools/experiment07_storage.sh`, exit code `0`).

| Check | Garage `v2.3.0` | SeaweedFS `3.97` |
|---|---|---|
| Unreachable from outside (curl from the host to the candidate's port) | **PASS** — `curl -m 3` did not establish a connection (exit 28, timeout) | **PASS** — same result (exit 28) |
| Container name does not resolve from the `bridge` network | confirmed (`getent hosts` — NORESOLVE) | confirmed (`getent hosts` — NORESOLVE) |
| **Anonymous, unsigned Range GET from inside the network** (D-38, mandatory condition 2) | **FAIL** — both ranges (`0-1048575` and a tail range) returned `403 Forbidden`. Garage has no mechanism for anonymous `GetObject` through the S3 API: `PutBucketPolicy` returns `NotImplemented`; the `garage bucket allow/deny` subcommands only work with named keys, not an anonymous principal; `garage bucket website` is a separate HTTP server with Host-based routing and `index.html` semantics, not the `GetObject` path that `slice` uses | **PASS** — both ranges returned `206 Partial Content` with a correct `Content-Range` |
| Signed Range (`s3api get-object --range`) | **PASS** — the `206` equivalent, a correct `ContentRange: bytes 0-1048575/67108864` | **PASS** — same result |
| Download through nginx `slice` (HTTP code) | **403** — nginx proxies `Range` without a signature (OSS nginx cannot do SigV4), and Garage answers `403` on the very first slice subrequest | **200** |
| sha256 of the object downloaded through `slice` matched the original (**mandatory condition 1**) | **FAIL** — the response body is the `403` (213 bytes of XML), not the object | **PASS** — byte-for-byte match |
| Lines in access_log when attempting to download 64 MiB with `slice 1m` | **1** — nginx proxies one `Range` subrequest, gets `403`, and does not continue slicing (the object is unavailable) | **64** — one line per one-megabyte slice (see the "access_log format" section below for the 64 vs. the expected 65) |
| `X-Cache-Status`, 1st / 2nd run | not measured (the body is not the object) | `MISS` / `HIT` — the second run is confirmed served from cache |
| TTFB cold / warm chunk (median of 3, seconds) | 0.0019 / 0.0017 — **measured against the 403 error**, not against a real object being served; on a local docker network with no network latency, the cold-vs-warm difference is not representative | 0.0044 / 0.0015 — the same local-network caveat applies: both values land in single-digit milliseconds, and the cold/warm difference is not meaningful on this rig |
| `CreateBucket` through the bare S3 API (a signed request) | **PASS**, but only after `garage key allow --create-bucket <key>` — a freshly-created key defaults to `Can create buckets: false` | **PASS** immediately, no extra permission needed |
| Image size | 27.2 MiB | 63.4 MiB |

**Only SeaweedFS met both mandatory conditions at once.** Garage fails
condition 2 (anonymous Range GET) and, as a direct consequence, condition 1
too (nginx cannot download the object at all, because it cannot sign
requests to the origin). The fallback to pinned MinIO was not triggered:
SeaweedFS is a candidate that passed both mandatory conditions.

## Decision

**`chrislusf/seaweedfs:3.97` is chosen** — the only candidate that passed
both mandatory conditions: Range through `slice` serves a byte-for-byte
matching object, and from inside the `mytube-exp07` network the candidate
answers an unsigned Range GET with `206`, exactly what D-38 requires (OSS
nginx cannot sign AWS SigV4, so there is no alternative — either the
candidate can serve anonymously, or `slice` does not work at all).

Garage is disqualified not because of Range as such (signed Range works
correctly on it), but because it has no built-in mechanism for anonymous
`GetObject`: `PutBucketPolicy` is unimplemented (`NotImplemented`), and its
only publicly-readable mode (`bucket website`) is a different HTTP server,
with virtual hosting by `Host` header and `index.html` semantics,
incompatible with the path-style `GetObject` that nginx's `slice` module
proxies requests through. This is not a rig bug — it is an architectural
limitation of Garage v2.3.0 regarding anonymous reads through the S3 API
itself.

## Parameters for docker-compose.yml

**Image:** `chrislusf/seaweedfs:3.97`
**Internal S3 endpoint port:** `8333`
**Service name in compose:** `storage`

**Container start command:**

```
server -dir=/data -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json -master.volumeSizeLimitMB=1024
```

**Required volumes:**
- `/data` — SeaweedFS's single data directory (covers both master/filer
  metadata and volume data; unlike Garage, no separate metadata volume is
  required)
- `/etc/seaweedfs/s3.json` — the identities config, mounted `:ro`

**Full text of the candidate's configuration file (`s3.json`):**

```json
{"identities":[
  {"name":"mytube","credentials":[{"accessKey":"${MYTUBE_S3_KEY}","secretKey":"${MYTUBE_S3_SECRET}"}],
   "actions":["Admin","Read","Write","List","Tagging"]},
  {"name":"anonymous","actions":["Read"]}
]}
```

**Important for the deployment step that follows:** the
`accessKey`/`secretKey` values in `s3.json` must match
`MYTUBE_S3_KEY`/`MYTUBE_S3_SECRET` from `.env` — this file must be
generated by templating (`envsubst` or equivalent) when the container
starts, not stored hardcoded in the image or in the repository. The
`anonymous` identity with the single `Read` action is SeaweedFS's built-in
and **only** mechanism for anonymous reads; it must be present in the
config from the very first start, there is no separate "enable it after
start" step (unlike the hypothetical Garage scenario, where no such
built-in step even exists).

**The exact bring-up and `mytube` bucket initialisation sequence, as
measured on the rig:**

1. Generate `s3.json` (see above) from the `.env` values
   `MYTUBE_S3_KEY`/`MYTUBE_S3_SECRET`, mount it at
   `/etc/seaweedfs/s3.json:ro`.
2. Start the `storage` container with the command above.
3. Wait for the S3 port to become ready — poll from inside the network
   (for example `docker exec <storage> ...` or a separate healthcheck
   container), calling `s3api list-buckets` with the `mytube` identity's
   credentials, until the first successful response (on the rig, readiness
   arrived 15-20 seconds after start — the master + volume + filer + S3
   gateway all come up inside one process).
4. Create the `mytube` bucket with a **signed** request: `aws
   --endpoint-url http://storage:8333 s3api create-bucket --bucket mytube`
   (the orchestrator itself can do the same via
   `boto3.client("s3").create_bucket(Bucket="mytube")` at startup —
   `CreateBucket` through the bare S3 API is confirmed to work, see the
   table above; no separate candidate-specific CLI command is needed,
   unlike with Garage).
5. Store objects with plain `put_object`/`upload_file` calls through
   `boto3` — no extra steps.

An example of this sequence's live run (steps 3-4), commands and output:

```
$ docker exec exp07-seaweedfs sh -c 'true'   # контейнер уже запущен командой из п.2
$ docker run --rm --network mytube-exp07 \
    -e AWS_ACCESS_KEY_ID=exp07key -e AWS_SECRET_ACCESS_KEY=exp07secretexp07secret \
    -e AWS_DEFAULT_REGION=mytube \
    amazon/aws-cli:2.27.50 --endpoint-url http://exp07-seaweedfs:8333 s3api create-bucket --bucket mytube
{
    "Location": "/mytube"
}
```

The captured transcript above shows the bucket-creation call succeeding
(`"Location": "/mytube"`), with a single inline comment left as it was
printed in the session.

**Anonymous reads from inside the network (D-38):** enabled by the
`s3.json` config from the very start (identity `anonymous`, action
`Read`) — no separate "enable after start" step is needed, unlike what the
plan assumed for Garage (`PutBucketPolicy`/`garage bucket` subcommands,
which in fact do not exist for this purpose in v2.3.0).

**`CreateBucket` through the bare S3 API:** works (see the table) — the
stack can create the `mytube` bucket at startup with a
`boto3.client("s3").create_bucket(...)` call, and does not need to run a
separate candidate-specific CLI command.

**Values for `.env.example`:**

```
MYTUBE_S3_ENDPOINT=http://storage:8333
MYTUBE_S3_BUCKET=mytube
MYTUBE_S3_REGION=mytube
MYTUBE_S3_KEY=<generate: openssl rand -hex 16>
MYTUBE_S3_SECRET=<generate: openssl rand -hex 32>
MYTUBE_S3_UPSTREAM=storage:8333
```

`MYTUBE_S3_REGION=mytube` — an arbitrary non-empty value; SeaweedFS (like
Garage) does not strictly validate the region, but client and server must
use the same value for a correct SigV4 signature.

**boto3 client parameters for the deployment step that follows** (confirmed
working on the rig through `aws-cli` with the same `addressing_style`):

```python
import boto3
from botocore.client import Config

s3 = boto3.client(
    "s3",
    endpoint_url=settings.s3_endpoint,        # MYTUBE_S3_ENDPOINT
    aws_access_key_id=settings.s3_key,        # MYTUBE_S3_KEY
    aws_secret_access_key=settings.s3_secret, # MYTUBE_S3_SECRET
    region_name=settings.s3_region,           # MYTUBE_S3_REGION = "mytube"
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
```

The full check of Pitfall F (compatibility of `boto3` calls with the
SeaweedFS S3 API beyond `put_object`/`head_object`/`get_object`/
`list_objects_v2`, already exercised through `aws-cli` on this rig) happens
in the step where a real Python environment first appears (M2) — this
experiment deliberately does not install `boto3` on the host.

## access_log format

The edge config for this experiment:

```nginx
log_format cache_audit '$time_iso8601 $uri $slice_range $upstream_cache_status $status $body_bytes_sent';
access_log /var/log/nginx/blob_access.log cache_audit;
log_subrequest on;
```

Sample of the first lines from the live run (64 MiB object, `slice 1m`,
SeaweedFS candidate):

```
2026-08-20T22:45:12+00:00 /blob/probe.bin bytes=1048576-2097151 MISS 200 2097698
2026-08-20T22:45:12+00:00 /blob/probe.bin bytes=2097152-3145727 MISS 200 3146274
2026-08-20T22:45:12+00:00 /blob/probe.bin bytes=3145728-4194303 MISS 200 4194850
...
2026-08-20T22:45:13+00:00 /blob/probe.bin bytes=66060288-67108863 MISS 200 67109410
2026-08-20T22:45:13+00:00 /blob/probe.bin - MISS 200 67108864
```

**Measured line count for a full 64 MiB download with `slice 1m`: 64**
(reproduced twice by independent rig runs — `experiment07_storage.sh` gave
64 both times, and a separate manual run for this document also gave 64).

This **diverges from the plan's acceptance-criteria wording** ("64
subrequests plus one main-request line, i.e. 65"). The real cause of the
discrepancy is visible in the data itself: **the first slice
(`bytes=0-1048575`) does not get its own log line** — it is logged as part
of the main request's final line (the last line of the example above: `-
MISS 200 67108864`, where `slice_range` is empty and `body_bytes_sent`
equals the full file size). Of the 64 one-megabyte slices, 63 are logged as
separate `subrequest` lines (`log_subrequest on` genuinely works — without
it there would be **one** line, not dozens), plus one more line for the
main request. Total: `63 + 1 = 64`, not `64 + 1 = 65`. The plan's formula
was based on the assumption "N subrequests plus the main request," never
checked empirically; the actual behaviour of `ngx_http_slice_module` is
that the first internal redirect merges with the main request rather than
being logged as a separate subrequest.

**This changes nothing material for the `cache-report` parser**: the point
of B2 is to confirm that `log_subrequest on` produces many lines per file
rather than one, and that is confirmed (`64 ≫ 1`). The regular expression
built against the format `$time_iso8601 $uri $slice_range
$upstream_cache_status $status $body_bytes_sent` must accept an empty
`slice_range` (`-`) for the main request's line — exactly as shown in the
sample above.

For Garage (disqualified per D-38), attempting to download the same object
through the same `slice` config produces a log with **1** line — nginx
proxies the first `Range` subrequest, gets a `403`, and does not continue
slicing, since the object is entirely unavailable.

## Privacy under D-38

Storage privacy here is **unreachability from outside**, not a closed
bucket ACL. Both measured facts:

1. **Connection refused from the host.** `curl -m 3` from the real host
   (not from the docker network) to the candidate container's IP and
   internal S3 port times out (`curl` exit 28) — the connection is never
   established, because the port is not published (`-p`/`ports:` is absent
   from the service definition). Confirmed for both candidates.
2. **`206` from inside the network.** From a container **inside**
   `mytube-exp07`, an unsigned Range GET to SeaweedFS returns `206` with a
   correct `Content-Range` — without this, edge nginx (which physically
   cannot sign AWS SigV4) would not be able to read the object at all.

Both facts together are what D-38's privacy means: the bucket serves
anonymously **only** from inside the `internal` `storage` network, and
there is no path to it from outside, because the port is not published.
This is **not** "an anonymous GET returned a non-2xx from outside" — from
outside, the request never gets through at all, well before the
HTTP-status level. In `docker-compose.yml`, the `storage` service must live
on a network with `internal: true` and publish no port at all — if someone
"fixes" this later by adding `ports:`, edge keeps working, but privacy
disappears silently.

## The owner's production MinIO (D-18)

The owner's production MinIO with the old HLS library (`s3.example.com`,
see `01-01-SUMMARY.md`) is **not touched and not addressed** by any command
in this experiment. The rig runs exclusively on temporary containers on an
isolated `mytube-exp07` network (Garage/SeaweedFS/nginx/aws-cli), none of
which have any network path to the owner's production storage. The chosen
storage (`chrislusf/seaweedfs:3.97`) comes up **alongside it**, in this
delivery work's own `docker-compose.yml` (D-16) — migrating the old HLS
library is not part of this work.

## Cleanup

After the rig finishes, `docker ps -a --filter name=exp07` and `docker
volume ls --filter name=exp07` show no objects at all — confirmed twice
after independent `experiment07_storage.sh` runs within this session. The
`mytube-exp07` network, the `exp07-garage`, `exp07-seaweedfs`, `exp07-nginx`
containers, the `exp07-meta`, `exp07-data` volumes, and the temporary
`src/build/exp07/` directory are all removed by `trap cleanup EXIT`,
regardless of the run's outcome.
