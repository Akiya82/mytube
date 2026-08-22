# MyTube — SETUP

[Русская версия](SETUP.ru.md)

A short setup guide: `.env` variables, startup order, what to expose through
an external reverse proxy, how to check the cache, backup, and cache modes.
This file stays intentionally minimal for now; it is expected to grow into a
full README once the project publishes images.

Every command below was either actually run while writing this document,
against a live stack (`docker compose ps` — every service healthy), or is
explicitly marked as not run.

## What this is, and what it does not do

Self-hosted progressive-MP4 delivery for a synchronous group of 7–15 friends
watching in a browser and in the AVPro player inside VRChat.

Three honest limitations:

- Every viewer inside VRChat has to enable "Allow Untrusted URLs" in their
  own settings **once** — without it AVPro will not open any external
  address, including this service's own.
- **Public and group-public VRChat instances are not supported**, as of
  build 1546 — only private and friends instances.
- **There is no adaptive bitrate, and there will not be.** Different
  qualities, if ever needed, live as separate editions of the same title,
  not as one stream switching on the fly — adaptive bitrate is confirmed not
  to work inside VRChat.

## `.env` variables

Copy `.env.example` to `.env` and fill it in. The values already in
`.env.example` are working defaults for a local stand, except the ones
marked "must be generated".

| Variable | Purpose | Default | Requirement |
|---|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres credentials, consumed directly by the `postgres:16-alpine` image | `mytube` / `change-me...` / `mytube` | The password must be generated |
| `MYTUBE_SECURE_LINK_SECRET` | Signing secret for `/blob/` addresses | — | Must be generated: `openssl rand -hex 32`. **Never committed** — `.env` is in `.gitignore` |
| `MYTUBE_SIGN_TTL_SECONDS` | Signature lifetime, seconds | `43200` (12h) | Optional |
| `MYTUBE_DATABASE_URL` | Postgres connection string, must match `POSTGRES_*` above | `postgresql://mytube:...@db:5432/mytube` | Must be kept in sync with `POSTGRES_*` |
| `MYTUBE_S3_ENDPOINT` | The storage's S3 endpoint. If it names an external host, the external storage is used | `http://storage:8333` | Optional |
| `MYTUBE_S3_UPSTREAM` | `host:port` of that same storage, used by edge's own upstream block to the storage | `storage:8333` | **Must change together with `MYTUBE_S3_ENDPOINT`** — see the warning below |
| `MYTUBE_S3_BUCKET` / `MYTUBE_S3_REGION` | Bucket name and an arbitrary non-empty region | `mytube` / `mytube` | Optional |
| `MYTUBE_S3_KEY` / `MYTUBE_S3_SECRET` | Credentials for the `mytube` S3 identity (Admin/Read/Write/List/Tagging) | — | Must be generated: `openssl rand -hex 16` / `openssl rand -hex 32` |
| `MYTUBE_DELETE_GRACE_SECONDS` | Window between the `410` response and the object's physical deletion | `300` (5 min) | Optional |
| `MYTUBE_CACHE_MODE` | `memory` or `off` — selects `edge/cache-${MYTUBE_CACHE_MODE}.conf.template` | `memory` | Optional, see "Cache modes" |
| `MYTUBE_CACHE_SIZE` | Cache zone size in tmpfs | `512m` | Optional |
| `MYTUBE_CACHE_INACTIVE` | Eviction window for chunks nobody has touched | `120s` | Optional |
| `MYTUBE_SLICE_SIZE` | Chunk size for `nginx slice` | `1m` | Optional |
| `MYTUBE_CACHE_LOCK_TIMEOUT` | `proxy_cache_lock_timeout` — keep noticeably above nginx's own default | `20s` | Optional |
| `MYTUBE_EDGE_PORT` | Host port edge listens on | `8080` | Optional |
| `MYTUBE_BACKUP_DIR` | Dump directory inside the `db-backup` container, bind-mounted to `./data/backups` on the host | `/backups` | Optional |
| `MYTUBE_BACKUP_KEEP_DAYS` | How many days to keep dumps before deleting them | `7` | Optional |
| `MYTUBE_WARM_BYTES` | Cache warm-up limit in bytes | `268435456` (half of the 512m zone) | Optional |
| `MYTUBE_PUBLIC_BASE_URL` | The address viewers reach, scheme included, no trailing slash | — | **Required — the service refuses to start without it.** Written into every copied link, so it is never derived from a browser's own address bar |
| `MYTUBE_SHARE_HOST_PATH` | Host path of the mounted network share, bind-mounted read-only into `api` at `/share` | `./ingest` | Optional |
| `MYTUBE_UI_PORT` | Host port the interface is published on | `8081` | Optional |
| `MYTUBE_UI_BIND` | Host address the interface port binds to | `127.0.0.1` | Optional, but see "Interface" below before changing it |
| `MYTUBE_STORAGE_QUOTA_BYTES` | Configured storage quota in bytes, `0` means no quota enforced — used only for the interface's usage figure | `0` | Optional |

**About `MYTUBE_S3_UPSTREAM` specifically.** `api` reaches storage through
`MYTUBE_S3_ENDPOINT`; `edge` reaches it through `MYTUBE_S3_UPSTREAM`. These
are two different addresses pointing at the same storage: change one and
forget the other, and uploads (through `api`) and playback (through `edge`)
end up talking to two different places — a break that does not surface
immediately, only the first time someone opens a newly added title. Both
variables change together whenever the storage endpoint changes.

**External storage endpoint.** Two more variables exist for this case but
are not required in `.env` because they default to values matching the
bundled storage service, so a fresh clone still starts with neither set:
`MYTUBE_S3_SCHEME` (`http` or `https`; the bundled storage is plain HTTP,
an external endpoint is normally HTTPS) and `MYTUBE_S3_HOST` (the bare
hostname of the external endpoint, deliberately **without** a port — edge
uses it as both the TLS SNI name and the `Host` header it sends upstream,
and a hostname with a port attached would not match either). Set all four
variables together (`MYTUBE_S3_ENDPOINT`, `MYTUBE_S3_UPSTREAM`,
`MYTUBE_S3_SCHEME`, `MYTUBE_S3_HOST`) when pointing this stack at storage
that is not the bundled one.

A separate constraint: an external storage endpoint must be reachable
**only from the owner's own internal network**. Nothing in this stack
enforces bucket-level access control — storage privacy rests on the bucket
being unreachable from outside, and a public S3 endpoint with anonymous
read would break that on its own, independent of every other setting here.

## Network share

The share this stack browses and adds files from is mounted on the **host**
and bind-mounted read-only into the `api` container at a single mount
point — the container cannot mount a CIFS/NFS share for itself. Set
`MYTUBE_SHARE_HOST_PATH` to the host path of an already-mounted share
before starting the stack.

**Linux**, an already-mounted CIFS share under `/mnt`:

```bash
MYTUBE_SHARE_HOST_PATH=/mnt/media-share
```

**Windows**, a share already mapped to a drive letter (map it first with
Windows' own "Map network drive", then point at that drive):

```bash
MYTUBE_SHARE_HOST_PATH=//./Z:
```

The share is bind-mounted read-only (`:ro`): nothing this stack does can
write back to it, including a failed add — the source file on the share is
never touched by any code path.

## Interface

The web interface is published on its own host port
(`MYTUBE_UI_PORT`, default `8081`), separate from the edge port that serves
video. It is bound to `127.0.0.1` (loopback) by default — reachable only
from the machine running the stack, not from the LAN.

**The public reverse proxy must never forward this port.** It points at the
edge port only (see "What to expose to the reverse proxy" below); the
interface is not meant to be reachable from outside the owner's own network
at all.

**Widening `MYTUBE_UI_BIND` to a LAN address is a deliberate, informed act,
not a convenience toggle.** Every route on the interface port is
unauthenticated, including the one that deletes a video immediately —
anyone on that network, any browser open to any page, can reach every one
of those routes. The loopback default is the only thing standing between
those routes and the network; widening it hands that boundary away
knowingly. The service logs the same warning at startup whenever the bind
address is not loopback.

## Storage capacity

The bundled storage service (SeaweedFS) is configured with no fixed volume
count — capacity follows the host's own free disk space, volumes are grown
lazily as needed, and each individual volume is capped at 30 GB. In
practice this means the collection's real ceiling is whatever disk space is
free on the host running the `storage` container's data volume, not a
number this stack imposes on top of that.

When the host genuinely runs low on disk, adds start failing for a reason
that has nothing to do with the file being added — the add's own pre-flight
free-space check may still pass (it compares against the ephemeral work
directory, not the storage backend), so a failure here can look unrelated
to capacity at first. Check the host's own free disk space for the volume
backing the `storage` service before assuming anything else is wrong.

To change the per-volume ceiling, edit `-master.volumeSizeLimitMB` in
`storage/entrypoint.sh` and recreate the service:

```bash
docker compose up -d --force-recreate storage
```

**Reading current usage** does not require running anything: the library
screen's header shows the total bytes used (and free bytes, once
`MYTUBE_STORAGE_QUOTA_BYTES` is set to a real quota). For detail beyond the
total — which objects nothing in the catalog points at, and which catalog
addresses point at objects the bucket no longer has — run:

```bash
docker compose exec api python -m orchestrator.cli storage-audit
```

This command only reads; it never deletes or modifies anything. When it
finds unreferenced objects, it prints the exact command to remove each one
through your own storage client — reclaiming that space is left to the
operator, on purpose, rather than automated on the strength of a
comparison that could be run against the wrong storage pairing.

## Prerequisites

Before the first command below can run, the machine needs Docker Desktop
with Compose v2. Install it from the [official Docker Desktop for Windows
guide](https://docs.docker.com/desktop/setup/install/windows-install/)
rather than a copy of it here — a second copy of somebody else's install
guide rots, and this project would then be maintaining two.

Check the install from a shell before continuing:

```bash
docker version
docker compose version
```

This stack is known to run on Docker Engine 29.7.2 with Compose v5.4.0;
an older Docker Desktop that still ships Compose v1 (a separate
`docker-compose` binary, with a hyphen, no `version` output matching the
above) is not what any command in this document was written against.

If `docker version` reports the command not found even though Docker
Desktop is installed, its CLI is not on this shell's `PATH` — some Docker
Desktop installs place it under a per-user directory
(`%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin`) that is not always
added automatically. Add that directory to `PATH`, or open a fresh
terminal after installing.

The orchestrator image now carries `ffmpeg` and `ffprobe` internally — an
add through the interface or the CLI needs nothing installed on the host
for that step.

Past Docker itself, the commands in this document run a handful of other
binaries directly on the host, not inside a container. On a machine with
only Docker Desktop and Git for Windows installed:

| Binary | Ships with |
|---|---|
| `docker`, `docker compose` | Docker Desktop |
| `bash`, `cp` | Git for Windows |
| `openssl`, `curl` | Git for Windows (its bundled MinGW toolchain) |
| `ffprobe` | **Neither.** Required only for the decodability check under "Verifying served video is actually decodable" below — install it separately (for example, an [ffmpeg Windows build](https://www.gyan.dev/ffmpeg/builds/)) and put it on `PATH` before running that one command. |

This document's own promise is that every command in it was either really
run or is explicitly marked as not. Installing Docker Desktop itself and
pulling this stack's images for the first time have not been walked by
following this document on a machine that started with neither —
`docker compose up -d --wait` below has only ever run where the images
were already present. A registry lookup for each image reference
(`postgres:16-alpine`, `chrislusf/seaweedfs:3.97`, `nginx:1.27-alpine`)
confirms each one still resolves to a real, `linux/amd64`-capable manifest
— that is evidence the pull should succeed, not a record that it has.

## Startup order

```bash
cp .env.example .env
# fill in .env -- at minimum MYTUBE_SECURE_LINK_SECRET, MYTUBE_S3_KEY,
# MYTUBE_S3_SECRET, POSTGRES_PASSWORD, MYTUBE_PUBLIC_BASE_URL
# (see the variable table above)

docker compose up -d --wait
```

The order matters — **bring the stack up first, do not "run migrations
first"**. The database lives in this same compose project, so migrating
before it comes up is not physically possible, and there is deliberately no
separate one-shot migration service in the stack: `docker compose up
--wait` treats a container exiting as a failure, and a one-shot init
service would trip exactly that check. Instead, the `api` service's
`docker-entrypoint.sh` runs `alembic upgrade head`, then ensures the
storage bucket exists (`ensure_bucket()`) on **every** start — both
operations are idempotent, so a restart is safe.

Check that everything came up:

```bash
docker compose ps            # every service -- healthy
docker compose logs api      # look for a line about a successful migration
```

If `ensure_bucket()` could not create the bucket over the S3 API, the exact
manual command for the chosen storage backend (SeaweedFS) is in
`docs/experiments/07-object-storage.md` — run it, then restart `api`.

Add a first title from the command line:

```bash
docker compose exec api python -m orchestrator.cli add \
  --file /ingest/<file under src/ingest/> --name "<title>"
```

The command prints an address of the form `/v/<slug>`.

## Windows / WSL2 deployment notes

Found while closing out an earlier acceptance gate on a real Windows host.
None of this is Linux-specific advice — it only applies if the compose
stack runs on Windows with Docker Desktop's WSL2 backend. None of the
commands in this section have been walked by following this document
top-to-bottom on a machine that started with WSL2 unconfigured — what
follows is corroborated against artifacts already present on one
already-configured deployment, not a record of a fresh install-and-
configure run. That deployment's `.wslconfig` does set
`networkingMode=Mirrored`, the precondition this whole section assumes; if
yours does not set that, none of it applies to you.

**Published port unreachable from the LAN even though `docker compose ps`
looks fine and Windows Firewall is disabled.** If Docker Desktop's WSL2
integration uses `networkingMode: mirrored`, the edge port is reachable on
`127.0.0.1` but the actual gate for anything coming from the LAN or the
internet is the **Hyper-V firewall** governing the WSL VM, not Windows
Firewall. It defaults to `DefaultInboundAction: Block` and ships only ICMP
and mDNS allow rules — a published container port never gets an explicit
allow rule on its own. Symptom: the reverse proxy (or any machine other
than the host itself) cannot reach the edge at all, while everything works
from `127.0.0.1`. Fix:

```powershell
# Look up your own VM's creator ID first -- do not reuse this from another
# machine, it is not a universal constant.
Get-NetFirewallHyperVVMSetting

# Scoped default: only the given remote address or subnet may reach the
# edge port. Replace <reverse-proxy-address-or-subnet> with the reverse
# proxy host's address, or the LAN subnet if more than one machine needs
# it -- see the security note below before running this.
New-NetFirewallHyperVRule -Name "MyTube-Edge-8080" -DisplayName "MyTube edge (HTTP 8080)" `
  -Direction Inbound -VMCreatorId '<your VMCreatorId from the command above>' `
  -RemoteAddress '<reverse-proxy-address-or-subnet>' `
  -Protocol TCP -LocalPorts 8080 -Action Allow

# Unscoped form -- only if the edge is genuinely meant to be reachable from
# any address that can reach this machine at all (omitting -RemoteAddress
# defaults it to Any):
# New-NetFirewallHyperVRule -Name "MyTube-Edge-8080" -DisplayName "MyTube edge (HTTP 8080)" `
#   -Direction Inbound -VMCreatorId '<your VMCreatorId from the command above>' `
#   -Protocol TCP -LocalPorts 8080 -Action Allow
```

**What this rule opens.** Without `-RemoteAddress`, this admits inbound TCP
to the edge port from any remote address that can reach the VM at all —
the block-by-default posture the Hyper-V firewall normally provides is
gone for this one port, for anyone, not just the reverse proxy. Scope it
to the reverse proxy host or the LAN subnet, as the default form above
does; keep the unscoped form only for the case where the edge is
genuinely meant to be reachable from anywhere, which this project's own
design does not call for — external traffic is already meant to arrive
through the owner's own reverse proxy (see "What to expose to the reverse
proxy" below), not directly at this port.

On one already-configured deployment, a Hyper-V rule matching this exact
command — same name, display name, and that VM's own creator ID — is
present, which corroborates that this command (or one equivalent to it)
was applied there; it does not establish that running it from a blocked
state was directly observed to restore reachability, since no
before/after test accompanies it. That same host also carries a second,
broader Hyper-V rule this document never instructed creating
(`VMCreatorId: Any`, unscoped by remote address), so observed reachability
on it cannot be attributed to the command above alone.

**Git Bash mangles container-absolute paths.** Running the `add` command
above (or any `docker compose exec ... --file /ingest/...`) from Git Bash
on Windows silently rewrites `/ingest/x.mp4` into something like
`C:/Program Files/Git/ingest/x.mp4` before it reaches the container --
Git Bash's POSIX-path auto-conversion does not know the path is meant for
the container, not the host. Prefix the command with `MSYS_NO_PATHCONV=1`:

```bash
MSYS_NO_PATHCONV=1 docker compose exec api python -m orchestrator.cli add \
  --file /ingest/<file> --name "<title>"
```

Scope `MSYS_NO_PATHCONV=1` to this one command, not the whole shell session
-- exporting it globally also disables Git Bash's path translation for
tools like `curl -o /d/some/local/path`, which then silently fail to write
their local output file while still reporting success.

**Viewer-side VPN trap, not a service defect.** At least one viewer's VPN
client ("throne") had to be switched from its default mode to TUN mode
before the address worked at all. If a friend reports the link does not
load, ask about their VPN client's mode before debugging the server.

## What to expose to the reverse proxy

Through the owner's own external reverse proxy, **exactly four** paths are
exposed: `/v/`, `/p/`, `/blob/` and `/refresh/`. Everything else — any
admin surface, the storage console, `/healthz`, and the web interface
described above — stays inside the LAN. There is deliberately no separate
access path from inside the LAN that bypasses the external channel: one
address and one code path for everyone, including the owner — so the owner
sees a break first, through the same eyes as everyone else.

**Required check: the external proxy must not buffer the response, and
must pass `Range` through untouched.** This is the classic pitfall of two
nginx instances in a row — if the external proxy buffers the whole
response before forwarding it to the client, the slice cache does nothing
useful, and the response to a Range request comes back as the whole file
instead of a `206`. Check with one command against the external domain:

```bash
curl -r 0-1048575 -D- -o /dev/null "https://<domain>/blob/<edition_id>.mp4?exp=...&sig=..."
```

Expect a `206` status and a `Content-Range` header shaped like
`bytes 0-1048575/<full size>`. A `200` with the whole file means the
external proxy is buffering — fix that on its side, not in this compose
project.

## Verifying served video is actually decodable

A `200`/`206` with the right `Content-Length` is proof the delivery
mechanics work -- it is **not** proof the bytes are playable video. An
earlier acceptance record was once validated against 3 MiB of random bytes
that happened to have a plausible size; the delivery chain looked perfect
and nothing would have opened in a player (see
`docs/experiments/02-05-avpro-redirect-and-cache.md`). Use
`tools/check_decodable.sh <url-or-path>` after adding any new title to
assert the served bytes actually decode as video, not merely that a
request for them succeeded:

```bash
bash tools/check_decodable.sh "https://<domain>/blob/<edition_id>.mp4?exp=...&sig=..."
```

`ffprobe` on this machine reorders `-of csv=p=0` output, so the script
parses `-of json` by field name instead -- do not "simplify" it back to csv.

## Checking the cache

Warm the cache **right before a viewing session**:

```bash
docker compose exec api python -m orchestrator.cli cache-warm <slug>
```

Note the current time (for `--since` below), run the session, then count:

```bash
docker compose exec api python -m orchestrator.cli cache-report --since <recorded time>
```

Acceptance criterion: **zero chunks fetched from storage more than once**,
i.e. an exit code of `0`. The `HIT` ratio prints for reference only — it is
not the acceptance number itself: with N simultaneous viewers on a cold
cache, its theoretical ceiling is `(N-1)/N`, i.e. 67% for three viewers.

**A non-zero `cache-report` exit code with no offending-chunk list does not
mean "everything is wrong" — it means "there is nothing to measure".** For
example, the log is empty, or `--since` filtered everything out. In that
case, first check that `location ~ ^/blob/` in
`edge/nginx.conf.template` has `log_subrequest on;` enabled — without it
nginx never logs the slice subrequests at all, and `cache-report` has
nothing to parse.

**Honest arithmetic behind the warm-up.** The 512 MB cache zone at a
1080p/~6 Mbps profile holds about 11 minutes of video, and the `inactive`
window (~2 minutes by default) evicts anything nobody touched for that
long. That is why `cache-warm` only warms the start of the file by
default — that is the one thing warming actually fixes (start-of-playback
stall) — and for the same reason, the promise that "storage restarting
mid-session goes unnoticed" holds only for the length of the `inactive`
window after storage becomes reachable again, not indefinitely.

**When reading the log by hand:** `cache-warm` sends its own point Range
requests, and `cache-report` attributes each of them to the chunk it
actually asked for — a warm-up landing inside the measurement window costs
at most one storage request per chunk, not a false violation. The reason to
still measure a session starting from the printed timestamp is to see the
session itself, not a start-of-file segment stretched out by the whole
warm-up. Lines recorded before the edge update that added this offset
field cannot be attributed at all — `cache-report` counts them separately,
and if the whole window consists only of them, it reports "not enough
data" rather than a clean pass. `cache-warm` prints the `--since`
instruction itself after every run.

**Observed during a live check:** `$time_iso8601` in the log has one-second
resolution. Recording `--since` and starting a session in the same second
can put the tail of a warm-up and the start of a session in the same
timestamp, producing the same false violation. This does not come up during
a real session (minutes pass between warming and the group actually
watching) — mentioned here only because it reproduced once while checking
this document automatically.

## Backup and restore

**Why this exists this early:** video files can be re-added; slug history
cannot. The `db-backup` service in `docker-compose.yml` takes a `pg_dump`
once a day into a compressed, date-named file, drops it into
`./data/backups` on the host (the same `/data/` tree already in
`.gitignore`), and deletes dumps older than `MYTUBE_BACKUP_KEEP_DAYS` days.

A one-off manual run (for example, right before a risky operation):

```bash
docker compose exec db-backup bash /backup/backup.sh --once
```

**Restore** (verified on a live stack in a real case — a dump restored over
a **non-empty** database, every slug came back, the link played):

```bash
# 1. Stop api. Not a formality: step 3 drops tables, and api, holding
#    connections to them, would exit on its own at that point anyway.
docker compose stop api

# 2. Make sure the database is up
docker compose up -d --wait db

# 3. Restore the latest dump.
#    ON_ERROR_STOP=1 is required: without it psql swallows every error and
#    exits 0, so a completely failed restore would report success.
LATEST=$(docker compose exec -T db-backup sh -c 'ls -t /backups/mytube-*.sql.gz | head -1')
docker compose exec -T db-backup sh -c "gunzip -c $LATEST" \
  | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
echo "exit code: $?"   # 0 -- restored; anything else -- NOT restored

# 4. Bring the rest back up. api will not come back on its own without this.
docker compose up -d --wait
```

The dump is taken with `pg_dump --clean --if-exists`, so it is
self-contained: it applies cleanly to an empty database and over an
existing one alike. That matters because a restore is normally needed when
data **exists** and is wrong, not when it is absent.

**Dumps taken before this flag existed will not apply over a non-empty
database** — they fail on the first `CREATE TABLE ... already exists`.
Take a fresh dump (`--once` above) and confirm the restore works before it
is ever needed for real.

**Under Git Bash on Windows**, commands with a path inside the container
need the `MSYS_NO_PATHCONV=1` prefix, or `/backup/backup.sh` turns into
`C:/Program Files/Git/backup/backup.sh`.

## Cache modes

`MYTUBE_CACHE_MODE` selects between `edge/cache-memory.conf.template` and
`edge/cache-off.conf.template`:

- **`memory`** — the working mode. `proxy_cache` in tmpfs (`/dev/shm`),
  cache key `$uri$slice_range` (without `$arg_sig`/`$arg_exp` — otherwise
  the cache would be partitioned per viewer's own signature, and a group
  watching together would save nothing on storage requests).
- **`off`** — the cache is fully disabled. Useful for diagnosis: if some
  behavior (for example, the number of storage requests) differs only
  under `memory`, the cache itself is the problem, not `slice`, the
  signature, or storage.

To switch, change `MYTUBE_CACHE_MODE` in `.env` and restart `edge`:
`docker compose up -d --wait edge`.
