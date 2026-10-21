# MyTube

[Русская версия](README.ru.md)

Self-hosted video delivery for a small group: progressive MP4 served to
VRChat's AVPro player and to an ordinary browser, built for one person and
about a dozen friends watching in sync. It is not a social network and has
no comments, recommendations or ads.

## Read these three things first

**Every viewer must enable "Allow Untrusted URLs".** It is a setting in
VRChat's own client, once per viewer. There is no way around it from this
side: without it AVPro refuses to open any address this service gives out.

**Public and group-public VRChat instances are not supported.** Since client
build 1546, "Allow Untrusted URLs" no longer applies there, and a world's
domain would have to be on VRChat's own allowlist. This is a limitation of
the platform, not a bug in this project. Private and friends instances work.

**There is no adaptive bitrate.** Different quality levels are separate
videos with separate links, chosen when the video is converted. A VRChat
world player has no control to switch between them, so nothing here tries to.

## Quick start

You need Docker with the Compose plugin and a hostname for the interface.

```bash
cp .env.example .env
# fill in the values marked "generate" and set the two hostnames
docker compose up -d
docker compose ps
```

By default the stack pulls the published interface image,
`ghcr.io/akiya82/mytube`, and mounts a local `./share` directory as the
source library. The optional converter (`docker-compose.worker.yml`) is built
on the GPU machine that runs it; a published `ghcr.io/akiya82/mytube-worker`
image exists too. The images are built for amd64; on an ARM machine such as
an Apple silicon Mac, Docker runs them under emulation, slower but working.
Two overrides change that:

- `docker-compose.build.yml` builds the interface image from this checkout
  instead of pulling it.
- `docker-compose.cifs.yml` mounts a network share in place of `./share`.

Combine them with `-f`, for example
`docker compose -f docker-compose.yml -f docker-compose.cifs.yml up -d`.
If the stack does not come up, `docker compose logs api` names the setting
it is missing. SETUP.md has a symptom-first troubleshooting section.

## Storage

The code speaks plain S3, so any S3-compatible endpoint works. The compose
file starts SeaweedFS for that role because it needs something that runs on a
clean machine. The author's own deployment does not use it: it points at an
external MinIO. If you already run MinIO or another S3 store, point the
`MYTUBE_S3_*` variables at it and leave the bundled one idle.

The edge reads objects without a signature, so an external bucket must allow
anonymous reads and be reachable only from the stack's internal network.
Never point it at a public hosted bucket: making that bucket readable would
publish the whole library. SETUP.md has the details.

## Licence

MIT, see [LICENSE](LICENSE). Both images bundle an ffmpeg built under the
LGPL and carry its licence text inside the image at
`/usr/share/licenses/ffmpeg/LICENSE.txt`.

## Where next

- [SETUP.md](SETUP.md) is the full operator guide: every variable, the
  reverse proxy, backup and restore, upgrading and troubleshooting.
- [docs/experiments/](docs/experiments/) holds the measurements behind the
  design decisions, including what AVPro does and does not accept.
- [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) explains how to
  contribute.
