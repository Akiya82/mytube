"""cache-warm: prime edge's tmpfs cache before a viewing session,
by walking the exact same path a viewer does -- GET /v/{slug}, follow the
signed 302, then sequential Range requests -- so the cache is warm and the
run itself is a reproducible stand for `cache-report`.

**Warms only the start of the file, not the whole thing.**
This is arithmetic, not caution: the cache zone is 512 MiB by default
(`MYTUBE_CACHE_SIZE`), which at a 1080p/~6 Mbps profile holds about
11 minutes of video, and `inactive` (`MYTUBE_CACHE_INACTIVE`, ~2 min
default) evicts whatever nobody touched for that long. Warming a two-hour
rip "to the end" cannot make anything stick -- at best the tail 11 minutes
would survive, and those evaporate within `inactive` of the warm finishing,
before anyone has watched them. The one thing warming actually fixes is
start-of-playback stall, so by default it stops at half the cache zone
(`MYTUBE_WARM_BYTES`) -- one file's warm-up should not evict everything
else in memory. Warm immediately before the session: the promise
("storage restarting mid-session goes unnoticed") holds only for as long
as `inactive`'s window, not indefinitely.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import typer

# CLI runs inside the `api` container, on the same `edge_net` compose
# network as `edge` -- "http://edge" is edge's internal (unpublished, port
# 80) address on that network, not the host-published MYTUBE_EDGE_PORT.
DEFAULT_BASE_URL = "http://edge"

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB, mirrors MYTUBE_SLICE_SIZE's default
DEFAULT_WARM_BYTES = 256 * 1024 * 1024  # half of MYTUBE_CACHE_SIZE's 512m default

_SIZE_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _parse_size(value: str) -> int:
    """Parse an nginx-style size string ("512m", "1m", "20971520") to bytes."""
    value = str(value).strip().lower()
    if value and value[-1] in _SIZE_UNITS:
        return int(value[:-1]) * _SIZE_UNITS[value[-1]]
    return int(value)


def cache_warm(
    slug: str,
    base_url: str = DEFAULT_BASE_URL,
    chunk_size: int | None = None,
    warm_bytes: int | None = None,
) -> int:
    """Warm `slug` through `base_url`, up to `warm_bytes` (default: half the
    cache zone, per `MYTUBE_WARM_BYTES`). Returns a process exit code: 0 on
    a successful warm (whether it reached the limit or the end of the
    file), 1 if `/v/{slug}` did not resolve or the origin refused to serve
    ranges at all.
    """
    chunk_size = chunk_size or _parse_size(os.environ.get("MYTUBE_SLICE_SIZE", "1m"))
    warm_bytes = warm_bytes or int(
        os.environ.get("MYTUBE_WARM_BYTES", DEFAULT_WARM_BYTES)
    )
    cache_zone = os.environ.get("MYTUBE_CACHE_SIZE", "512m")

    started = time.monotonic()
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        resp = client.get(f"{base_url}/v/{slug}")
        if resp.status_code != 302:
            typer.echo(
                f"/v/{slug} did not answer 302 (got {resp.status_code}) -- nothing to warm",
                err=True,
            )
            return 1
        blob_url = resp.headers["location"]

        warmed = 0
        chunks = 0
        total_size: int | None = None
        warm_limit = warm_bytes

        range_refused = False

        # The byte limit is checked unconditionally, before anything is
        # known about the file. Gating it behind a known total_size meant
        # that an origin answering 200 with the whole body -- which is
        # exactly what a misconfigured edge does -- left total_size null,
        # kept the loop condition permanently true, and never consulted
        # the limit at all: the command re-downloaded a multi-gigabyte
        # file forever.
        while warmed < warm_limit and (total_size is None or warmed < total_size):
            end = warmed + chunk_size - 1
            r = client.get(blob_url, headers={"Range": f"bytes={warmed}-{end}"})
            if r.status_code != 206:
                range_refused = True
                break
            content_range = r.headers.get("content-range", "")
            if total_size is None and "/" in content_range:
                total_size = int(content_range.rsplit("/", 1)[-1])
                warm_limit = min(warm_bytes, total_size)

            got = len(r.content)
            if got == 0:
                break
            warmed += got
            chunks += 1
            if got < chunk_size:
                break  # short read -- reached end of file

    elapsed = time.monotonic() - started
    if range_refused:
        typer.echo(
            "the origin ignored Range and returned the whole file -- "
            "nothing to warm. Check slice and proxy_cache_key in edge's config.",
            err=True,
        )
        return 1

    limited_by_file_length = total_size is not None and warmed < total_size

    typer.echo(
        f"chunks warmed: {chunks}, bytes warmed: {warmed}"
        f"{f' of {total_size}' if total_size is not None else ''}, "
        f"cache zone: {cache_zone}, time: {elapsed:.1f}s"
    )
    if limited_by_file_length:
        typer.echo(
            f"the file is longer than the warm-up limit ({warm_bytes} bytes) -- "
            "only the opening stretch was warmed; the cache zone is smaller "
            "than the whole file anyway, and inactive would evict the warmed "
            "part before playback even starts, so warming further would be "
            "pointless"
        )
    # Hand the operator the exact --since value instead of telling them to take a
    # timestamp themselves. nginx logs whole seconds, so a warm that finishes in
    # a fraction of a second shares its log second with a timestamp taken right
    # after it, and --since is inclusive: the warm's own point-Range requests land
    # inside the measurement window and report a violation that did not happen.
    # Rounding up to the next whole second past the last warm request is what makes
    # the instruction actually followable.
    safe_since = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    typer.echo(
        "measure the session starting from this timestamp -- that way "
        "`cache-report` sees the session itself, not the opening stretch "
        "stretched out by the warm-up:"
    )
    typer.echo(f"    cache-report --since {safe_since}")

    return 0
