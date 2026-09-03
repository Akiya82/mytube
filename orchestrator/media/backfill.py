"""Measure the stream parameters of videos that were added before the
catalog had columns to record them in.

Seven parameters decide whether two clips can be concatenated without
re-encoding -- frame rate, video profile, level, pixel format, and the
selected audio track's codec, sample rate and channel layout. They are
recorded by the add path, and by nothing else, so every edition that
predates that path carries seven nulls. A null reads as "not known to
match", which is the honest reading and the reason this module exists:
without it the first playlist assembled out of a library that predates the
columns is refused, naming every member, for a measurement nobody ever
took.

The measurement is taken from the source file on the share, not by
downloading the stored object. The source is local and costs nothing to
read; the stored object lives in storage and is measured in gigabytes. For
every parameter but one the two are the same stream in a different
container, because the remux copies the video untouched. The exception is
the audio codec, which the remux converts whenever the source is not
already the codec it targets -- that one is reconciled here through the
same helper the add path uses, from the flag the catalog already stores.

Nothing in here raises. One source that has been moved, renamed or deleted
must not stop a run over the whole library, and an operator needs the list
of what could not be measured far more than they need a traceback. Every
failure comes back as a named result carrying a reason in words, and
nothing at all is written for it: a null says truthfully that the
measurement is missing, while a guessed value would make the refusal that
reads it confidently wrong.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

from orchestrator.db import queries
from mytube_media import compat
from mytube_media.share import ShareEscape, safe_join


@dataclass(frozen=True)
class Measurement:
    """What one edition came back with. `reason` is None exactly when the
    measurement succeeded, and the seven values are None exactly when it
    did not -- there is no half-measured state, because a partial row
    would be indistinguishable from a complete one to the check that
    reads it.
    """

    edition_id: object
    title_name: str
    reason: str | None = None
    fps: float | None = None
    video_profile: str | None = None
    video_level: int | None = None
    pix_fmt: str | None = None
    audio_codec: str | None = None
    audio_sample_rate: int | None = None
    audio_channel_layout: str | None = None

    @property
    def measured(self) -> bool:
        return self.reason is None


def measure_edition(row, share_root: Path) -> Measurement:
    """Probe one edition's source and return the seven values, or a reason
    it could not be measured.

    `row` is one record from `queries.editions_missing_technical`. The path
    it carries is resolved through the share module's own containment check
    rather than by concatenation -- a stored relative path is data, and the
    add path resolves it the same way for the same reason.
    """
    edition_id = row["edition_id"]
    title_name = row["title_name"]

    def unmeasured(reason: str) -> Measurement:
        return Measurement(edition_id, title_name, reason=reason)

    source_rel = row["source_path"]
    if not source_rel:
        return unmeasured("no source file was recorded for this video")

    try:
        source = safe_join(share_root, source_rel)
    except ShareEscape:
        return unmeasured(
            f"the recorded source path does not sit inside the share: {source_rel}"
        )

    if not source.is_file():
        return unmeasured(f"the source file is no longer on the share: {source_rel}")

    try:
        probe_json = compat.probe(source)
    except compat.ProbeError:
        return unmeasured(f"the source file could not be read: {source_rel}")

    # The same two calls the add path makes, with the same selected track,
    # so the values recorded here and the values recorded for anything
    # added from now on come out of one reader. A second reader of the
    # probe output would drift from the first the day the first learns
    # something.
    verdict = compat.analyze(
        probe_json, selected_audio_index=row["audio_stream_index"]
    )

    # A source with no video stream leaves every video field of the verdict
    # empty. There is nothing to record for it and nothing honest to guess.
    if verdict.pix_fmt is None and verdict.fps is None and verdict.video_profile is None:
        return unmeasured(f"this file carries no video to measure: {source_rel}")

    selected_track = next(
        (t for t in verdict.tracks if t.index == row["audio_stream_index"]), None
    )
    if selected_track is None:
        return unmeasured(
            "the audio track this video was added with is no longer in the source: "
            f"{source_rel}"
        )

    return Measurement(
        edition_id,
        title_name,
        # The four video parameters are taken from the verdict unreconciled
        # and deliberately so: the video stream is always copied, so what
        # the source carries is what the stored object carries. Do not add
        # a reconciliation here.
        fps=verdict.fps,
        video_profile=verdict.video_profile,
        video_level=verdict.video_level,
        pix_fmt=verdict.pix_fmt,
        # The audio codec is the one exception. The remux converts the
        # audio whenever the source is not already the codec it targets, so
        # the probed codec describes the source and nothing else; the
        # stored flag is the only record of what actually happened, and is
        # taken from the row rather than re-derived (compat's own helper
        # makes the same reconciliation for the add path).
        audio_codec=compat.effective_audio_codec(
            selected_track.codec, row["audio_reencoded"]
        ),
        audio_sample_rate=selected_track.sample_rate,
        audio_channel_layout=selected_track.channel_layout,
    )


async def backfill_editions(
    conn, share_root: Path
) -> tuple[list[Measurement], list[Measurement]]:
    """Measure every edition still missing its parameters, write the ones
    that could be measured, and return the two lists.

    The values the row already carries -- size, duration, resolution, the
    audio track's language and title, and the re-encode flag -- are handed
    back to the writer unchanged. This fills in what is missing; it does
    not restate what is already there. In particular the recorded size
    describes the object in storage, which in the re-encoding case is not
    the source file this routine just probed.
    """
    rows = await queries.editions_missing_technical(conn)

    measured: list[Measurement] = []
    unmeasured: list[Measurement] = []

    for row in rows:
        # The probe blocks on file I/O; one edition at a time, off the loop.
        result = await asyncio.to_thread(measure_edition, row, share_root)
        if not result.measured:
            unmeasured.append(result)
            continue

        await queries.set_edition_technical(
            conn,
            result.edition_id,
            size_bytes=row["size_bytes"],
            duration_seconds=row["duration_seconds"],
            width=row["width"],
            height=row["height"],
            audio_language=row["audio_language"],
            audio_title=row["audio_title"],
            audio_reencoded=row["audio_reencoded"],
            fps=result.fps,
            video_profile=result.video_profile,
            video_level=result.video_level,
            pix_fmt=result.pix_fmt,
            audio_codec=result.audio_codec,
            audio_sample_rate=result.audio_sample_rate,
            audio_channel_layout=result.audio_channel_layout,
        )
        measured.append(result)

    return measured, unmeasured
