"""One-at-a-time in-process background worker that assembles a playlist,
in the shape `orchestrator.media.queue` already established for the add
pipeline: a module logger, a frozen job with one constructor, one consumer
so concurrency of one is a property of the construction rather than of a
lock, and per-job exception isolation so one failed build never stops the
next one.

**Nothing synchronous is called bare from this module.** This loop shares
its event loop with every HTTP request this process answers, including
`GET /p/{slug}`, `GET /v/{slug}` and `/healthz` -- and the compose health
check reads a `/healthz` that does not return as the container being
down, which takes the edge's `depends_on: service_healthy` condition with
it. Every blocking call here -- the disk check, each download, ffmpeg,
each probe, each upload, each cleanup call -- goes through
`asyncio.to_thread`. The rule is stated once, here, so the next person
adding a step follows it instead of rediscovering it from an outage.

**What a build produces.** One segment object per member, MPEG-TS bytes
under a key shaped `{a fresh identifier}.mp4`, plus one artifact row each
in position order. The document listing them is *not* stored -- the
public address renders it fresh on every request with current signatures,
because a stored one carries signatures that age out partway through an
evening and there is no refresh path for a manifest the way there is for
a blob. `orchestrator.media.playlist_build`'s module docstring carries the
reasoning for the container and for the key shape.

**Fresh keys, never an overwrite.** Overwriting rewrites bytes a viewer
may be part-way through reading. New keys plus a pointer moved inside one
transaction make a rebuild atomic from every reader's point of view, and
the address the world holds does not change either way.

**A superseded generation is not deleted here.** It stays in the bucket,
marked superseded, and the reclamation sweep removes it once it is older
than the signing lifetime -- the longest an address handed out before the
rebuild can still be valid. Deleting it at swap time would contradict
this module's own promise: a viewer holding a signed address, past
whatever the edge still has cached, would get nothing and see the stub
clip, in the middle of exactly the evening this product exists for.

**Deliberately not built: a startup sweep that resumes an interrupted
build.** A build killed by a restart leaves the previous generation
serving and the playlist row untouched; the operator presses save again.
The cost of that choice is named rather than hidden: the objects a
half-finished build had already uploaded are orphaned, since no artifact
row names them -- the storage audit lists them and the operator removes
them. The existing startup sweep already clears the scratch directory, so
nothing accumulates on local disk.
"""
import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from mytube_media import compat, remux
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media import playlist_build
from orchestrator.storage import (
    abort_dangling_multipart,
    delete_object,
    download_object_to_file,
    put_object_from_file,
)

logger = logging.getLogger(__name__)

# A stream copy runs far faster than a re-encode, but a member can be an
# eight-gigabyte rip and the read comes off local scratch. Generous, and
# still bounded, so a genuinely hung ffmpeg cannot wedge the worker.
SEGMENT_TIMEOUT_SECONDS = 1800

# The bytes are MPEG-TS whatever the key is spelled like -- see
# `playlist_build`'s module docstring. Stating the real type on the upload
# is what keeps the response a player receives honest about what it holds.
SEGMENT_CONTENT_TYPE = "video/mp2t"

REASON_KEY_EMPTY = "playlist.refused.empty"
REASON_KEY_STORAGE_FULL = "playlist.refused.storage_full"
REASON_KEY_CHECKS = "playlist.refused.checks"
REASON_KEY_ASSEMBLY = "playlist.refused.assembly"


class _InsufficientSpaceError(Exception):
    """Internal signal only. Raised by the free-space precheck and mapped
    to its own catalogue key, the same way the add worker maps its own.
    """


class _SegmentCheckError(Exception):
    """The file ffmpeg produced did not pass this repository's own checks.
    Its own signal, and its own catalogue key, because the premise this
    whole mechanism rests on is that the assembly tool can exit
    successfully over a broken result -- so a zero exit code is not the
    check and must not be reported as one.
    """


@dataclass(frozen=True)
class PlaylistBuildJob:
    """The playlist to assemble, and nothing else.

    Every other fact is read by the worker itself, in one query, from the
    catalog as it is when the build starts -- not as it was when save was
    pressed. Two saves in a row are ordinary, and a job carrying a
    membership snapshot from the first one would build the wrong list.
    """

    playlist_id: object


class PlaylistBuildQueue:
    """Wraps an unbounded `asyncio.Queue`. Exactly one worker task ever
    calls `.get()`, so one build at a time is a property of how this is
    constructed rather than of a lock somebody has to remember.
    """

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[PlaylistBuildJob]" = asyncio.Queue()

    async def enqueue(self, job: PlaylistBuildJob) -> None:
        await self._queue.put(job)

    async def run(self, pool: asyncpg.Pool) -> None:
        while True:
            job = await self._queue.get()
            try:
                await process_playlist_build(pool, job)
            except Exception:
                logger.exception("playlist build %s failed", job.playlist_id)
            finally:
                self._queue.task_done()


async def _record_failure(pool, playlist_id, reason_key: str, reason_params: dict) -> None:
    async with pool.acquire() as conn:
        await queries.set_playlist_build_failed(conn, playlist_id, reason_key, reason_params)


async def _discard(keys) -> None:
    """The abort-then-delete pair the add worker already runs, against
    every key this build wrote. The abort covers an upload that died
    partway (an incomplete multipart upload); the delete covers one that
    completed before a later step failed. Both are safe no-ops against a
    key that was never touched, so both run unconditionally.
    """
    for key in keys:
        try:
            await asyncio.to_thread(abort_dangling_multipart, key)
            await asyncio.to_thread(delete_object, key)
        except Exception:
            logger.exception("could not remove the orphaned segment %s", key)


def _probe_duration(probe_json: dict) -> float | None:
    try:
        return float(probe_json.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


async def _check_segment(path) -> float:
    """This repository's own two checks over what ffmpeg produced, before
    anything points at it.

    The faststart check that every MP4 this project produces goes through
    does not apply: an MPEG-TS stream has no index atom to move to the
    front, it is readable from any point by construction. What remains is
    the decodability check -- the same one `tools/check_decodable.sh`
    runs, through the same probe -- and it does double duty, because the
    duration the document declares is read from the produced file rather
    than carried over from the member's catalog row. A declared duration
    that does not match the bytes is a malformed document, and a malformed
    document hangs every player in the instance.
    """
    probe_json = await asyncio.to_thread(compat.probe, path)
    if not any(s.get("codec_type") == "video" for s in probe_json.get("streams", [])):
        raise _SegmentCheckError(f"{path}: no decodable video stream")
    duration = _probe_duration(probe_json)
    if duration is None or duration <= 0:
        raise _SegmentCheckError(f"{path}: no usable duration ({duration!r})")
    return duration


async def process_playlist_build(pool: asyncpg.Pool, job: PlaylistBuildJob) -> None:
    """Assemble one playlist end to end.

    1. Read the members in one query. A member whose title has been
       deleted is skipped: it drops out and the rest play, which is the
       whole promise of a link that keeps working. Nothing left means
       there is nothing to build, and the address already answers
       not-ready for that.
    2. Run the joinability check on the recorded parameters, before
       anything is downloaded. Under this mechanism it is informational
       rather than blocking -- the boundary marker is what handles a
       parameter change, measured rather than assumed -- so the finding is
       logged and the build continues.
    3. Check free space against the directory ffmpeg actually writes into,
       using this build's own formula. The configured storage figure in
       this project is a display value that enforces nothing, and this
       stack has run out of space twice; a build that pulls every member
       onto local disk and writes a copy beside them is the obvious next
       occasion.
    4. Stream each member out of storage into scratch -- never into
       memory.
    5. Copy its packets into one deliverable segment.
    6. Check what came out before anything points at it (see
       `_check_segment`).
    7. Upload under a fresh key, never the playlist's own identifier,
       which is stable across rebuilds and would mean overwriting live
       objects.
    8. Validate the document these segments render into, against the set
       of keys actually uploaded, and only then record the swap. On any
       validation failure nothing is pointed at anything: this is the one
       failure in this project that reaches every viewer in an instance at
       once.
    9. If the swap reports the playlist is no longer active, delete every
       object just uploaded -- the row is gone, nothing will ever point at
       them, and no artifact row exists for the deletion sweep to find
       them by.
    """
    settings = get_settings()
    work_dir = Path(settings.work_dir)
    scratch: list[Path] = []
    uploaded: list[str] = []

    try:
        async with pool.acquire() as conn:
            members = await queries.playlist_build_inputs(conn, job.playlist_id)

        live = [m for m in members if not m["dropped"] and m["object_key"]]
        if not live:
            await _record_failure(pool, job.playlist_id, REASON_KEY_EMPTY, {})
            return

        parity = playlist_build.parity_verdict(live)
        if not parity.ok:
            # Recorded, not refused. The boundary between two clips whose
            # parameters differ was measured in a real player and carried,
            # so refusing here would refuse exactly the heterogeneous
            # library this feature exists for.
            logger.info(
                "playlist %s joins members that differ (%s): %s",
                job.playlist_id,
                parity.reason_key,
                parity.reason_params,
            )

        required = playlist_build.required_build_bytes(live)
        free = await asyncio.to_thread(lambda: shutil.disk_usage(settings.work_dir).free)
        if free < required:
            raise _InsufficientSpaceError(
                f"{free} free bytes < {required} required in {settings.work_dir}"
            )

        artifacts = []
        sections = []
        for index, member in enumerate(live):
            source = work_dir / f"{job.playlist_id}-{index}-source"
            segment = work_dir / f"{job.playlist_id}-{index}-segment"
            scratch.extend((source, segment))

            await asyncio.to_thread(
                download_object_to_file, member["object_key"], str(source)
            )
            await asyncio.to_thread(
                remux.run_ffmpeg,
                playlist_build.build_segment_command(source, segment),
                timeout=SEGMENT_TIMEOUT_SECONDS,
            )
            duration = await _check_segment(segment)

            key = f"{uuid.uuid4()}.mp4"
            await asyncio.to_thread(
                put_object_from_file, str(segment), key, SEGMENT_CONTENT_TYPE
            )
            uploaded.append(key)

            artifacts.append(
                {
                    "object_key": key,
                    "position": member["position"],
                    "duration_seconds": duration,
                }
            )
            sections.append(
                playlist_build.Section(
                    title_name=member["title_name"] or "",
                    segments=(
                        playlist_build.Segment(
                            object_key=key, duration_seconds=duration
                        ),
                    ),
                )
            )

        check = playlist_build.validate_manifest(sections, set(uploaded))
        if not check.ok:
            raise _SegmentCheckError(f"{check.reason_key}: {check.reason_params}")

        async with pool.acquire() as conn:
            swapped = await queries.set_playlist_built(
                conn, job.playlist_id, "hls", artifacts
            )

        if not swapped:
            logger.info(
                "playlist %s was deleted while its build ran -- removing the "
                "%d object(s) it uploaded, since no row will ever name them",
                job.playlist_id,
                len(uploaded),
            )
            await _discard(uploaded)
            return

        logger.info(
            "playlist %s built: %d segment(s)", job.playlist_id, len(artifacts)
        )
    except Exception as exc:
        logger.exception("playlist build %s failed", job.playlist_id)
        await _discard(uploaded)
        await _record_failure(
            pool, job.playlist_id, _reason_key_for(exc), _reason_params_for(exc)
        )
    finally:
        for path in scratch:
            try:
                if await asyncio.to_thread(path.exists):
                    await asyncio.to_thread(path.unlink)
            except OSError:
                logger.exception("could not remove the scratch file %s", path)


def _reason_key_for(exc: Exception) -> str:
    """Map a failure to a translation catalogue key, never an English
    sentence and never raw tool output.

    A produced file that failed its own checks gets a different key from
    an assembly that failed outright, because they send the operator to
    different places: the first means the clip that came out is wrong, the
    second means it never came out.
    """
    if isinstance(exc, _InsufficientSpaceError):
        return REASON_KEY_STORAGE_FULL
    if isinstance(exc, _SegmentCheckError):
        return REASON_KEY_CHECKS
    return REASON_KEY_ASSEMBLY


def _reason_params_for(exc: Exception) -> dict:
    """A refusal carries values, not a message. The only value any of
    these sentences substitutes today is nothing at all -- the underlying
    detail goes to the log, where it belongs, rather than in front of an
    operator who cannot act on ffmpeg's stderr.
    """
    return {}
