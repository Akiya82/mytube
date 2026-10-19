# Orchestrator image. Also serves as the CLI image: commands run via
# `docker compose exec api python -m orchestrator.cli ...` against the
# already-running container — nothing is installed on the host.
FROM python:3.13-slim

WORKDIR /app

# ffmpeg/ffprobe, copied by digest (not a moving tag) from a static-binary
# image with no shell — pinned and verified in
# docs/experiments/08-ffmpeg-in-image.md. Placed above the source COPY
# lines so an orchestrator source edit never invalidates this layer.
COPY --from=mwader/static-ffmpeg:9.0@sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36 /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:9.0@sha256:f1e16a2f8fd9c4470fcccf4de56655c58b4ef7f2e6a5fcefb0e636e124363b36 /ffprobe /usr/local/bin/ffprobe

# fontconfig plus one bundled Cyrillic-capable family (DejaVu Sans) --
# this is the image the preview's burn-in renders in, and the
# slim base ships neither a font configuration library nor any font
# files at all. Without this layer, libass resolves no family and every
# preview renders blank or boxed text: a feature that appears to work,
# produces an image, and shows nothing the operator asked to see.
# The worker image installs the same package and names the same family
# so both programs render the same picture.
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
