# Orchestrator image. Also serves as the CLI image: commands run via
# `docker compose exec api python -m orchestrator.cli ...` against the
# already-running container — nothing is installed on the host.
FROM python:3.13-slim

WORKDIR /app

# The same pinned, checksummed LGPL build the converter image downloads, so
# one artifact and one licence cover both images. Placed above the source
# COPY lines so an orchestrator source edit never invalidates this layer.
ARG FFMPEG_TAG=autobuild-2026-08-31-13-27
ARG FFMPEG_ASSET=ffmpeg-n8.1.2-50-g1a748fe2cd-linux64-lgpl-8.1.tar.xz
ARG FFMPEG_SHA256=7d6d93e9c39e0e461feb13c118e91e4eec2515e4da3a01d4ad6790996731bbee

RUN set -eu; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates \
    && curl -fsSL -o /tmp/ffmpeg.tar.xz \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_TAG}/${FFMPEG_ASSET}" \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && tar -xf /tmp/ffmpeg.tar.xz -C /tmp \
    && mv /tmp/ffmpeg-*/bin/ffmpeg /usr/local/bin/ffmpeg \
    && mv /tmp/ffmpeg-*/bin/ffprobe /usr/local/bin/ffprobe \
    && mkdir -p /usr/share/licenses/ffmpeg \
    && cp /tmp/ffmpeg-*/LICENSE.txt /usr/share/licenses/ffmpeg/LICENSE.txt \
    && rm -rf /tmp/ffmpeg* \
    && apt-get purge -y --auto-remove curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# fontconfig plus one bundled Cyrillic-capable family (DejaVu Sans) --
# this is the image the preview's burn-in renders in, and the
# slim base ships neither a font configuration library nor any font
# files at all. Without this layer, libass resolves no family and every
# preview renders blank or boxed text: a feature that appears to work,
# produces an image, and shows nothing the operator asked to see.
# The worker image installs the same package and names the same family, and
# now the same ffmpeg binary, so both programs render the same picture.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY mytube_media ./mytube_media
COPY orchestrator ./orchestrator
COPY static ./static
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install --no-cache-dir ".[api]"

# Ephemeral scratch space for ffmpeg output. Deliberately a plain
# directory in the container layer, never a named volume or bind mount —
# a container recreate wipes it, which is half of how an interrupted add
# leaves nothing behind (the free-space preflight and the "leaves
# nothing behind" promise both rest on it).
RUN mkdir -p /work

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# A fixed, non-root uid so an ffmpeg invocation over untrusted media (probe,
# attachment dump, burn-in) never runs as root -- /work is this
# user's own scratch space, the only local path anything here writes to.
RUN useradd -r -u 10001 mytube && chown mytube /work
USER mytube

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
