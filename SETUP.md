# MyTube — SETUP

[Русская версия](SETUP.ru.md)

A short setup guide: `.env` variables, startup order, what to expose through
an external reverse proxy, how to check the cache, backup, and cache modes.
This file stays intentionally minimal for now; it is expected to grow into a
full README once the project publishes images.

Every command below was either actually run while writing this document,
against a live stack (`docker compose ps` — every service healthy), or is
explicitly marked as not run.

## Quick index

- [Interface](#interface) — how to reach it, first run, changing the password
- [`.env` variables](#env-variables) — includes `MYTUBE_WORKER_TOKEN`, the conversion worker's credential
- [Playlists](#playlists) — what the link does, and what the world player has to be set to
- [Conversion worker](#conversion-worker) — optional, needs a GPU, the second compose file
- [What to expose to the reverse proxy](#what-to-expose-to-the-reverse-proxy) — a worked config block
- [Upgrading](#upgrading) — pulling a change into a running deployment
- [Rolling back](#rolling-back) — reverting one
- [Backup and restore](#backup-and-restore) — what the dump now contains
- [When something is wrong](#when-something-is-wrong) — symptom-first troubleshooting

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
| `MYTUBE_S3_BUCKET` / `MYTUBE_S3_REGION` | Bucket name, and the region the storage signs requests with — any non-empty value will do, but **the converter must be given the same one** | `mytube` / `mytube` | Optional |
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
| `MYTUBE_ADMIN_FQDN` | The bare hostname the operator interface answers on — no scheme, no trailing slash. Must differ from `MYTUBE_PUBLIC_BASE_URL`'s own host | — | **Required — the interface container refuses to start without it.** `docker compose logs api` names `admin_fqdn` in a validation error: `Field required` if the line is missing from `.env` entirely, `must not be empty` if it is present but blank |
| `MYTUBE_TRUSTED_PROXY` | The address edge believes a forwarded client address from, so the login rate limit counts real callers instead of counting your reverse proxy once for everybody. An address or CIDR | `172.16.0.0/12` (the range Docker's own bridge gateways fall in) | Optional, and the default is usually right. **Not your proxy's address** — Docker rewrites the source of anything arriving through a published port, so edge sees its bridge gateway instead. See "Reverse proxy" for how to read the address and check which way it is counting |
| `MYTUBE_SHARE_URL` | Address of the source share, in the form a CIFS mount takes: `//host/share`. The stack mounts it itself and gives `api` a read-only view at `/share` | — | **Required — `api` does not start without a share it can mount** |
| `MYTUBE_SHARE_USERNAME` | Account the share is mounted as. Read-only access on the server's side is enough | — | Required |
| `MYTUBE_SHARE_PASSWORD` | That account's password | — | Required |
| `MYTUBE_SHARE_DOMAIN` | Windows domain, for a domain-joined share | empty | Optional — leave empty for a local account on the machine serving the share |
| `MYTUBE_STORAGE_QUOTA_BYTES` | Configured storage quota in bytes, `0` means no quota enforced — used only for the interface's usage figure | `0` | Optional |
| `MYTUBE_WORKER_TOKEN` | The conversion worker's only credential — opens the `/worker/` prefix behind a constant-time comparison. Both this service and the converter must be given the same value | — | **Required — the interface container refuses to start without it.** Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `MYTUBE_CONVERTER_OFFLINE_SECONDS` | Seconds with no converter check-in before a queued conversion shows "waiting for a converter" instead of plain "queued" | `60` | Optional |
| `MYTUBE_TRANSCODE_BITRATE_CEILING_BPS` | Advisory bitrate above which a compatible source earns a recommendation to convert. Gates nothing | `9000000` | Optional |
| `MYTUBE_TRANSCODE_TARGET_BITRATE_BPS` | Output bitrate ceiling for a full-height (1080p) transcode | `6000000` | Optional |
| `MYTUBE_TRANSCODE_ESTIMATE_REALTIME_FACTOR` | Fallback speed estimate for the pre-start time estimate, before any transcode has finished | `4.0` | Optional |

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

The share this stack browses and adds files from is mounted by the stack
itself, not by the machine it runs on. Nothing has to be mapped, mounted or
prepared on the host first — give the stack the address and an account and
it does the rest, which is what makes moving to another machine a matter of
copying `.env`.

Four settings, three of them required:

```bash
MYTUBE_SHARE_URL=//192.0.2.5/media
MYTUBE_SHARE_USERNAME=readonly-user
MYTUBE_SHARE_PASSWORD=the-password
MYTUBE_SHARE_DOMAIN=
```

`MYTUBE_SHARE_URL` is the address written the way a CIFS mount wants it:
forward slashes, no trailing one. A Windows UNC path is the same address the
other way round — `\\192.0.2.5\media` becomes `//192.0.2.5/media`.

Leave `MYTUBE_SHARE_DOMAIN` empty unless the share is domain-joined; for a
local account on the machine serving it, a domain is not just unnecessary but
will make the mount fail.

The share is read-only twice over — once in the mount options, once again on
the container's own mount. Nothing this stack does can write back to it,
including a failed add: the source file on the share is never touched by any
code path.

**The host needs CIFS support in its kernel.** Docker Desktop's own VM has
it. A bare Linux host usually needs `cifs-utils` installed; without it the
mount fails with `wrong fs type`.

**Two things about this that will otherwise cost you an evening:**

The mount happens when `api` starts, not when `docker compose` reads its
configuration. A wrong address, a wrong password or an unreachable server is
therefore not a configuration error — it is `api` failing to start, and the
real reason is in `docker compose logs api`.

Changing any of these four settings later is not enough on its own. Docker
records a named volume's mount options when it first creates the volume and
keeps using the recorded ones, so an edited `.env` changes nothing until the
volume is thrown away:

```bash
docker compose down
docker volume rm src_share
docker compose up -d
```

## Interface

The interface answers on its own hostname (`MYTUBE_ADMIN_FQDN`), through the
same edge port that already serves video. It publishes no host port of its
own — the edge is the only door in, for both surfaces.

That hostname needs its own DNS record and its own certificate on the
reverse proxy the owner already runs; TLS terminates there and never inside
this stack (see "What to expose to the reverse proxy" below for a worked
example). The two hostnames — the delivery one in `MYTUBE_PUBLIC_BASE_URL`
and this one — must be different names. The separation between what a
viewer gets and what an operator gets is the difference between the two
names, and nothing else.

The session cookie is marked `Secure`, which means the interface needs TLS
on any address that is not loopback. That is deliberate, and a LAN address
is not offered as an alternative: there is no host port left to reach one
on, and this document does not describe a route the project does not
support.

**First run.** Bring the stack up, then read the container log:

```bash
docker compose logs api
```

It prints exactly one line carrying a complete, clickable address, shaped
like `https://<admin-hostname>/setup?token=<random>` — the one-time setup
link. Open it once; choosing a username and a password finishes setup and
signs the operator in immediately. The link works once, a restart mints a
new one, and nobody who cannot read the container log can reach that page.

Signing out ends that browser's session on the server; other devices stay
signed in.

**To change the password**, run this inside the container. It takes effect
immediately with no restart, and it signs every device out — which is the
point of it:

```bash
docker compose exec api python -m orchestrator.cli reset-password
```

It prompts for the new password twice, prints the username it reset, and
says that every signed-in device was signed out.

**If the account is gone, or the setup link expired and was never used**,
this is the way back in:

```bash
docker compose exec api python -m orchestrator.cli create-operator --username <name>
```

It refuses if an operator already exists.

## Playlists

A playlist has an address of its own, the same shape as a video's but under a
different prefix: `https://<delivery-hostname>/p/<name>`. Paste it into a
world's video player and the clips play one after another. It is one link and
it does not change when the list is edited, so a link already pasted somewhere
keeps working.

Three things about it are worth knowing before somebody asks you about them in
a world.

**An edit reaches people the next time they open the link, not while they are
watching.** The player fetches the whole list once, at the moment the address
is opened, and never asks again for the rest of the session. Reordering,
adding or removing clips is therefore invisible to anyone already playing;
they finish the list as it was when they started, and everyone who opens the
link afterwards gets the new one. This is a property of the player, not a
setting, and there is nothing on this side that changes it.

**Deleting a video from the library takes it out of every playlist it was in,
by itself.** The remaining clips keep playing and nothing has to be rebuilt.
On the operator's side the playlist keeps a marked gap where that clip used to
be, so the list does not quietly change shape behind your back; take the gap
out once you have seen it.

**Repeating at the end is the world player's own loop control, not something
this service does.** The service hands over a list that ends. Whether it
starts again is a setting in the player's own menu, and this service builds
nothing for it — a list that ends is all either half of the arrangement can
express. With that control on, the list repeats correctly in the common
players. One player behaves differently: in VizVid the picture freezes after
the end when it loops back while the sound carries on. That was seen with a
list whose clips differ from one another in resolution, frame rate or audio
layout; whether a list of uniform clips does the same there has not been
checked. If somebody reports a frozen picture on repeat, ask which player the
world uses before suspecting the service.

**In a browser, a playlist address does not play yet.** Playlists are a
VRChat capability for the moment. A single video's address plays in a browser
exactly as it always has and is not affected by any of this.

## Conversion worker

**This is optional.** Without it, adding a file as-is (a remux, no re-encode)
still works exactly as described above — only converting an incompatible
source needs a worker. If your machine has no capable GPU, the rest of this
section does not apply to you.

Conversion needs a second machine — or a second process on the same one — with
an NVIDIA GPU that can encode (NVENC), the vendor driver, and the NVIDIA
Container Toolkit's runtime integration, which is what actually hands a
container access to the card. Without that last piece the worker container
starts, then fails the instant it tries to touch the GPU — its log names a
CUDA/NVENC initialization error, not a missing-driver message, so it is easy
to mistake for a broken card when the real gap is the toolkit.

Bring it up with the second compose file, on the machine that has the card:

```bash
docker compose -f docker-compose.worker.yml up -d --build
```

It reads its variables from its own `.env.worker`, a separate file on the
GPU host — **not** a copy of this service's own `.env`. Copying that file
instead would hand the GPU host the service's own storage key, database
password, signing secret and share password, all readable from the
container's environment; only the eleven variables below belong on that
machine.

| Variable | Purpose | Requirement |
|---|---|---|
| `MYTUBE_ORCHESTRATOR_URL` | Where the worker reaches this service — scheme included, no trailing slash | Required, no default |
| `MYTUBE_WORKER_TOKEN` | The shared credential. **Must be the exact same value** given to this service's own `MYTUBE_WORKER_TOKEN` (see the variable table above) | Required, no default |
| `MYTUBE_S3_ENDPOINT` / `MYTUBE_S3_BUCKET` / `MYTUBE_S3_REGION` | The same storage endpoint, bucket and region this service uses. The region is part of what the storage signs a request with, so a value that differs from this service's own fails every upload with `SignatureDoesNotMatch` | Required |
| `MYTUBE_WORKER_S3_KEY` / `MYTUBE_WORKER_S3_SECRET` | The worker's **own** storage credential — under these names, not `MYTUBE_S3_KEY`/`MYTUBE_S3_SECRET` | Required, no default, **no fallback to the service's own key pair** |
| `MYTUBE_WORKER_NAME` | The converter's own name, defaulting to the machine's hostname when unset | Optional |
| `MYTUBE_WORKER_VRAM_MIN_MIB` | The free-VRAM floor below which the converter reports "waiting for GPU" instead of claiming | Optional |
| `MYTUBE_WORKER_POLL_SECONDS` | How often the converter polls when idle or waiting for the card | Optional |
| `MYTUBE_WORKER_MAX_OUTPUT_WIDTH` | The encoder's output width ceiling | Optional |
| `MYTUBE_SHARE_HOST_PATH` | Where the source share is mounted on this host, bind-mounted read-only into the container | Required |
| `MYTUBE_WORKER_SCRATCH_PATH` | A scratch directory with room for a large file plus its own rewrite pass | Required |

**The worker's own storage credential should be narrower than the service's,
not a copy of it.** Scope it to put objects and abort a multipart upload in
the one bucket, nothing else — no delete, no bucket management. This machine
runs a process that talks to two other systems over the network; a leaked
credential here should not be able to remove anything. Put it under
`MYTUBE_WORKER_S3_KEY`/`MYTUBE_WORKER_S3_SECRET` even when both this service
and the worker run on one machine — there is no fallback, on purpose: reusing
the service's own key here would be the intent working and the behavior
(a wider credential exposed on the machine most likely to be compromised)
being exactly wrong.

**`MYTUBE_ORCHESTRATOR_URL` must be the hostname this service answers on, not
a bare IP address.** The service checks the hostname a request actually
arrived under and refuses anything else with a plain `400` that carries no
explanation of why — so a worker pointed at an IP address gets a short
refusal on every single poll, with nothing in the response saying the
hostname is the problem. If the worker's log shows nothing but repeated
`400` responses and never claims a job, this is the first thing to check.

**The source share has to be the same tree at the same relative root on both
machines.** A stored source path is relative, so the string only means the
same file if the mount point matches; a different root silently points every
existing source path at the wrong file (or nothing) the moment a conversion
tries to open it.

On a single machine, bringing the worker up is simply a second `docker
compose ... up`, alongside the first, untouched. Across two machines it is
the exact same file, run on whichever one has the card.

**How to tell it is working:** in the library, a queued conversion moves from
"waiting for a converter" to "working" once a worker claims it. If it never
leaves "waiting for a converter", no worker has reached this service with the
matching token yet.

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
# MYTUBE_S3_SECRET, POSTGRES_PASSWORD, MYTUBE_PUBLIC_BASE_URL,
# MYTUBE_ADMIN_FQDN (see the variable table above)

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

**First run on the interface** is described once, under [Interface](#interface)
above — read it there before opening the admin hostname for the first time.

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

Through the owner's own external reverse proxy, **two hostnames** forward to
the **one** edge port (`MYTUBE_EDGE_PORT`): the delivery hostname
(`MYTUBE_PUBLIC_BASE_URL`'s own host) and the interface hostname
(`MYTUBE_ADMIN_FQDN`). The storage console and `/healthz` are never exposed
to either — they are not proxied by edge at all. There is deliberately no
separate access path from inside the LAN that bypasses the external
channel: one address and one code path for everyone, including the owner —
so the owner sees a break first, through the same eyes as everyone else.

**A worked example, in nginx** (the edge in this repository is nginx, so
this is not a second syntax to learn). It is an example, not a supported
product — run something else if you prefer, but carry these properties
across whatever you use:

```nginx
# Delivery hostname -- video addresses, VRChat and browsers alike.
server {
    listen 443 ssl;
    server_name mytube.example.com;   # MYTUBE_PUBLIC_BASE_URL's own host

    ssl_certificate     /etc/letsencrypt/live/mytube.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mytube.example.com/privkey.pem;

    location / {
        # Load-bearing, not decorative. The ordinary default form of a
        # reverse proxy leaves Host at whatever proxy_pass's own upstream
        # name is, which matches neither of edge's own server_name
        # directives -- the request then falls into edge's delivery block
        # (the first one in its file, and the implicit default for any
        # unmatched Host), and the operator's panel answers with a video
        # download and no explanation.
        proxy_set_header Host $host;

        # Delivery is progressive video served in byte ranges. A buffering
        # proxy, or one that strips the Range header, turns every seek into
        # a full re-download.
        proxy_buffering off;
        proxy_http_version 1.1;

        proxy_pass http://127.0.0.1:8080;   # MYTUBE_EDGE_PORT
    }
}

# Interface hostname -- must be a different name than the one above.
server {
    listen 443 ssl;
    server_name admin.mytube.example.com;   # MYTUBE_ADMIN_FQDN

    ssl_certificate     /etc/letsencrypt/live/admin.mytube.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.mytube.example.com/privkey.pem;

    location / {
        proxy_set_header Host $host;   # same reason as above

        # Who is knocking. Every request that reaches edge through this
        # proxy arrives carrying the proxy's own address, so without this
        # header edge counts the whole internet as one caller and the login
        # rate limit becomes a single shared bucket -- a stranger hammering
        # the form would drain it and lock you out of your own interface.
        # Only the delivery block above can skip it: it has no rate limit
        # to key and no interest in who is asking.
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        proxy_buffering off;
        proxy_http_version 1.1;
        proxy_pass http://127.0.0.1:8080;   # MYTUBE_EDGE_PORT
    }
}
```

Edge only believes that header from an address you have named in
`MYTUBE_TRUSTED_PROXY`, so a client cannot forge its own.

**Name the address edge actually sees, which is almost certainly not your
proxy's.** Docker rewrites the source address of anything arriving through a
published port, so the container sees its own bridge gateway no matter where
the request came from — setting this to the proxy's LAN address is the
natural guess and it does not work. The default covers the range those
gateways fall in, and is usually the right answer. To see the address for
yourself:

```bash
docker network inspect src_edge_net \
  --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
```

A value that does not match costs nothing but the fix: the header is
ignored, and the rate limit goes back to counting everyone as one. To check
which it is doing, make a few requests and look at what the refusal names:

```bash
docker compose logs edge | grep "limiting requests" | tail -1
```

`client:` is the address the limit is counting. If it shows the gateway
address rather than a real caller, the header is not being believed.

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

## Upgrading

`docker compose up -d --wait` — the only bring-up command elsewhere in this
document — **builds an image only when one is absent.** An existing
deployment that pulls a change and runs that command as-is gets the new
edge template and the new compose file running against the **old**
application image: no gate, no login screen, no first run. If the interface
hostname is already published by then, the whole operator surface is on the
public internet with nothing in front of it, and `--wait` reports every
service healthy — the old image is perfectly healthy, it just predates the
gate.

1. Pull the change.
2. Add `MYTUBE_ADMIN_FQDN` to `.env` if it is not already there. The
   interface refuses to start without it and names it in its own log; every
   other compose subcommand keeps working while you fix it.
3. **Rebuild and bring up in one command:**
   ```bash
   docker compose up -d --build --wait
   ```
   `--build` is not optional here — the application is baked into its
   image, and the plain form reuses whatever image is already there.
4. **Check the interface is closed before it is reachable**, from the
   machine itself, with a forged host header:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -H "Host: $MYTUBE_ADMIN_FQDN" http://127.0.0.1:${MYTUBE_EDGE_PORT}/library
   ```
   must print `302`. If it prints `200`, the old image is still running —
   stop, and go back to step 3.
5. Only then create the DNS record and the reverse-proxy vhost.
6. Read `docker compose logs api` for the setup link and finish first run
   (see [Interface](#interface) above).

Step 4 before step 5 is the whole point: a reader who publishes first and
checks later has no way to know how long the panel was open.

## Rolling back

Reverting the application while the reverse-proxy vhost keeps forwarding the
interface hostname is not a rollback — it is the same exposure, arrived at
from the other direction, and it would happen at speed, under stress, by
somebody who has just decided something is wrong. There is also a database
step with no obvious symptom until it kills the container.

1. **Remove the interface vhost from the reverse proxy first.** Not the DNS
   record — DNS is cached and slow, the vhost is immediate. The interface
   stops being reachable from outside at this moment, and everything after
   it happens on a surface nobody outside can see.
2. **Downgrade the schema before switching the image back:**
   ```bash
   docker compose exec api alembic downgrade -1
   ```
   This phase's schema revision is unknown to an older version's migration
   history, so an older container's start-up migration cannot proceed past
   it and the container would die on a message naming a revision it cannot
   find.
3. Check out the previous revision and run `docker compose up -d --build --wait`.
4. Confirm delivery — which is what the rollback was for.

The interface is unreachable between steps 1 and 4, and that is deliberate:
it is the only ordering with no window in which a published hostname fronts
an application with no gate.

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

**The dump directory now contains the operator's password hash** and
deserves the same care as any other secret on the machine — treat
`./data/backups` accordingly, including on any machine it is copied to.

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

**What a restore produces:** the operator account survives, every session
does not — the dump keeps the session table's definition but excludes its
rows, so nobody stays signed in on a restored stack and everyone signs in
again afterwards, including on the device that took the dump.

**Restoring a dump taken before the interface had a login** is both likely
and, without this note, would look broken in a confusing way. Such a dump
carries no `operator_credential` table and no `sessions` table, so it emits
no drop for either — but it does restore the recorded schema version to
what it was when the dump was taken. The stack then comes back up with both
tables already standing (this phase's migration created them earlier) and a
schema version that predates them; the migration is written to tolerate
that, which is what keeps the container alive. What the operator sees next
is an interface with no account. Restart the `api` container, read
`docker compose logs api` for a fresh setup link, and choose a password
again, the same as any other first run.

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

## When something is wrong

Every symptom below is new as of the interface gaining its own hostname and
login; none of them is self-explanatory from the error alone.

- **The panel hands you a video download instead of a page** — the reverse
  proxy is not preserving the original host. See the worked example under
  [What to expose to the reverse proxy](#what-to-expose-to-the-reverse-proxy).
- **The interface container will not start and the log names a variable** —
  `MYTUBE_ADMIN_FQDN` is missing from `.env`.
- **The interface container will not start and the log says the two
  hostnames are the same** — `MYTUBE_ADMIN_FQDN` and `MYTUBE_PUBLIC_BASE_URL`
  must be different names, or nginx routes every request for that name into
  whichever server block wins the exact match, breaking the other silently.
- **Every address answers with a short clip instead of a page** — the
  interface hostname is reaching the delivery block; either it is not in
  `.env` or it does not match the reverse-proxy vhost.
- **The browser will not keep you signed in** — the session cookie is
  marked `Secure`, so the address needs TLS.
- **The setup link says it is no longer valid** — the container restarted
  and a new one was printed; read `docker compose logs api` again.
- **You are locked out entirely** — see the two credential commands under
  [Interface](#interface): `reset-password` if the account still exists,
  `create-operator` if it does not.
- **Too many attempts, and requests start being refused** — the edge bounds
  the rate on the login and setup routes; wait a minute.

**A better first move than working through the list above:**
`bash tools/smoke_auth.sh` walks the whole authentication path against a
running stack and prints a pass or fail line per step.

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
