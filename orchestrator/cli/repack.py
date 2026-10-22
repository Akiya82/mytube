"""repack-layout: rewrite stored videos into the layout every new file gets
-- audio first, no chapter track (`mytube_media.remux.stream_layout_args`)
-- by copying streams, never re-encoding. Newest additions go first.

The object is overwritten under its own key, so every reference to the
edition (episode default, playlist items, jobs) stays as it is. What the
key cannot protect is the edge: it caches one-megabyte slices of
`/blob/{edition_id}.mp4` for an hour, and a viewer who already read the old
`moov` keeps asking for the old offsets. So a video is only touched when the
edge log shows nobody fetched it for longer than the cache keeps a slice;
anything else is deferred, and running the command again is how it gets
done later.
"""

import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import typer

from mytube_media.compat import moov_before_mdat
from mytube_media.remux import build_remux_command, run_ffmpeg
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.storage import (
    abort_dangling_multipart,
    delete_object,
    download_object_to_file,
    get_s3_client,
    head_object,
    put_object_from_file,
)

# The edge keeps a slice for an hour (`proxy_cache_valid 200 206 1h` in
# edge/nginx.conf.template), plus a margin for a slice whose log line was
# written a little after it was cached.
QUIET_FOR = timedelta(minutes=70)
# The downloaded original, the rewritten copy, and the +faststart pass over it.
SCRATCH_MULTIPLE = 3
PROBE_TIMEOUT_SECONDS = 60
REMUX_TIMEOUT_SECONDS = 3600
TEMP_PREFIX = "repack-"

ALREADY = "already"
REPACK = "repack"
REPACKED = "repacked"
DEFERRED = "deferred"
SKIPPED = "skipped"


def probe_layout(target: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_chapters",
            target,
        ],
        capture_output=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe could not read it")
    return json.loads(result.stdout)


def classify(probe_json: dict) -> str:
    """ALREADY, REPACK, or the reason this file is left alone."""
    streams = probe_json.get("streams", [])
    types = [s.get("codec_type") for s in streams]
    if types == ["audio", "video"] and not probe_json.get("chapters"):
        return ALREADY
    if any(s.get("disposition", {}).get("attached_pic") for s in streams):
        return "it carries a cover picture as a video stream"
    if types.count("audio") != 1 or types.count("video") != 1:
        return f"it has {types.count('audio')} audio and {types.count('video')} video streams"
    return REPACK


def edge_log_window(
    log_path: Path, uri: str
) -> tuple[datetime | None, datetime | None]:
    """When the edge log starts, and when `uri` was last fetched (None if
    never).
    """
    first = last = None
    # ponytail: reads the whole log per video; a day of heavy viewing is a
    # few hundred thousand lines, seconds per call. Index it once per run if
    # the log ever grows past that.
    with log_path.open(encoding="utf-8", errors="replace") as log:
        for line in log:
            stamp, _, rest = line.partition(" ")
            if first is None:
                first = datetime.fromisoformat(stamp)
            if rest.startswith(uri + " "):
                last = stamp
    return first, (datetime.fromisoformat(last) if last else None)


def deferral_reason(
    *,
    now: datetime,
    log_start: datetime | None,
    last_fetch: datetime | None,
    playlist_building: bool,
    free_bytes: int,
    size_bytes: int,
) -> str | None:
    if log_start is None or now - log_start < QUIET_FOR:
        return "the edge log is too recent to tell whether someone is watching it"
    if last_fetch is not None and now - last_fetch < QUIET_FOR:
        return f"it was watched at {last_fetch:%H:%M} UTC, less than {QUIET_FOR} ago"
    if playlist_building:
        return "a playlist is being built and may be reading it"
    if free_bytes < SCRATCH_MULTIPLE * size_bytes:
        return "not enough scratch space for it right now"
    return None


def _label(row) -> str:
    where = f"episode {row['episode_number']}"
    if row["season_number"] != 1:
        where = f"season {row['season_number']}, {where}"
    label = f"{row['title_name']}, {where}"
    return f"{label} ({row['edition_slug']})" if row["edition_slug"] else label


def _frame_counts(probe_json: dict) -> dict:
    return {
        s["codec_type"]: s.get("nb_frames")
        for s in probe_json["streams"]
        if s["codec_type"] in ("audio", "video")
    }


async def _repack_one(conn, row, work_dir: Path, log_path: Path) -> tuple[str, str]:
    key = row["object_key"]
    url = get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().s3_bucket, "Key": key},
        ExpiresIn=PROBE_TIMEOUT_SECONDS * 10,
    )
    try:
        verdict = classify(probe_layout(url))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return SKIPPED, "the stored file could not be read"
    if verdict == ALREADY:
        return ALREADY, ""
    if verdict != REPACK:
        return SKIPPED, verdict

    async def blocked() -> str | None:
        log_start, last_fetch = edge_log_window(
            log_path, f"/blob/{row['edition_id']}.mp4"
        )
        return deferral_reason(
            now=datetime.now(timezone.utc),
            log_start=log_start,
            last_fetch=last_fetch,
            playlist_building=await queries.playlist_build_in_progress(conn),
            free_bytes=shutil.disk_usage(work_dir).free,
            size_bytes=head_object(key)["ContentLength"],
        )

    if reason := await blocked():
        return DEFERRED, reason

    source = work_dir / f"{TEMP_PREFIX}{row['edition_id']}.source.mp4"
    output = work_dir / f"{TEMP_PREFIX}{row['edition_id']}.mp4"
    try:
        download_object_to_file(key, str(source))
        original = probe_layout(str(source))
        audio = next(s for s in original["streams"] if s["codec_type"] == "audio")
        run_ffmpeg(
            build_remux_command(
                source,
                output,
                audio_stream_index=audio["index"],
                reencode_audio=False,
                channels=audio.get("channels") or 2,
            ),
            timeout=REMUX_TIMEOUT_SECONDS,
        )
        rewritten = probe_layout(str(output))
        if (
            classify(rewritten) != ALREADY
            or not moov_before_mdat(output)
            or _frame_counts(rewritten) != _frame_counts(original)
        ):
            return SKIPPED, "the rewritten copy did not match the original"
        # Asked again: the download and the rewrite take minutes.
        if reason := await blocked():
            return DEFERRED, reason
        if await queries.edition_is_live(conn, row["edition_id"]) is not True:
            return SKIPPED, "it was deleted while being rewritten"
        try:
            put_object_from_file(str(output), key)
        except Exception:
            abort_dangling_multipart(key)
            raise
        # ponytail: a viewer who starts in the minute between the check above
        # and this upload, or one paused for over an hour, sees one glitch.
        if await queries.edition_is_live(conn, row["edition_id"]) is None:
            # Reaped while uploading: nothing points at the object any more.
            delete_object(key)
            return SKIPPED, "it was deleted while being uploaded"
        await queries.set_edition_size(conn, row["edition_id"], output.stat().st_size)
        return REPACKED, ""
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


async def repack_layout() -> int:
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    log_path = Path(settings.blob_access_log)
    if not log_path.is_file():
        typer.echo(
            f"cannot read the edge log at {log_path}, so there is no way to tell "
            "which videos are being watched; nothing was changed",
            err=True,
        )
        return 1
    log_start, _ = edge_log_window(log_path, "")
    now = datetime.now(timezone.utc)
    if log_start is None or now - log_start < QUIET_FOR:
        ready_at = (log_start or now) + QUIET_FOR
        typer.echo(
            "the edge log is too recent to tell which videos are being watched; "
            f"run this again after {ready_at:%H:%M} UTC. Nothing was changed",
            err=True,
        )
        return 1
    for leftover in work_dir.glob(f"{TEMP_PREFIX}*"):
        leftover.unlink(missing_ok=True)

    results: dict[str, list[str]] = {
        REPACKED: [],
        ALREADY: [],
        DEFERRED: [],
        SKIPPED: [],
    }
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await queries.editions_to_repack(conn)
        typer.echo(f"{len(rows)} videos to check, newest first")
        for row in rows:
            try:
                outcome, reason = await _repack_one(conn, row, work_dir, log_path)
            except Exception as exc:  # one bad video must not stop the batch
                outcome, reason = SKIPPED, f"{type(exc).__name__}: {exc}"
            line = f"{_label(row)} -- {reason}" if reason else _label(row)
            results[outcome].append(line)
            if outcome != ALREADY:
                typer.echo(f"{outcome}: {line}")
    finally:
        await conn.close()

    typer.echo("")
    typer.echo(
        f"repacked: {len(results[REPACKED])}, "
        f"already in the new layout: {len(results[ALREADY])}, "
        f"deferred: {len(results[DEFERRED])}, left alone: {len(results[SKIPPED])}"
    )
    if results[DEFERRED]:
        typer.echo("run the command again later for the deferred ones")
    return 0 if not results[DEFERRED] and not results[SKIPPED] else 1


def run_repack_layout() -> int:
    return asyncio.run(repack_layout())
