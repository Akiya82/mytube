"""backfill-technical: measure the stream parameters of videos that were
added before the catalog recorded them.

Read-only against storage and against the share -- it opens source files
to probe them and writes nothing but catalog columns that were empty.

The exit code is the same contract `storage-audit` uses: zero when there
is nothing left unmeasured, non-zero when there is, so this can be dropped
into a check later without changing its shape. Anything that could not be
measured is named with a reason in words rather than counted, because an
operator who is only told a number will later be told, confusingly, that
two of their clips cannot be played together.
"""
import asyncio
from pathlib import Path

import asyncpg
import typer

from orchestrator.config import get_settings
from orchestrator.media.backfill import backfill_editions

OK_EXIT_CODE = 0
UNMEASURED_EXIT_CODE = 1


async def backfill_technical() -> int:
    """Run the backfill and print the report. Returns the exit code."""
    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        measured, unmeasured = await backfill_editions(
            conn, Path(settings.share_root)
        )
    finally:
        await conn.close()

    if not measured and not unmeasured:
        typer.echo(
            "nothing to measure -- every video in the library already carries "
            "the stream parameters that decide whether it can be joined to another"
        )
        return OK_EXIT_CODE

    typer.echo(f"measured: {len(measured)}")
    for result in measured:
        typer.echo(
            f"  {result.title_name} -- {result.fps} fps, {result.video_profile} "
            f"level {result.video_level}, {result.pix_fmt}, "
            f"audio {result.audio_codec} {result.audio_sample_rate} Hz "
            f"{result.audio_channel_layout}"
        )

    if not unmeasured:
        typer.echo("")
        typer.echo("nothing was left unmeasured")
        return OK_EXIT_CODE

    typer.echo("")
    typer.echo("could not measure, left as unknown rather than guessed:")
    for result in unmeasured:
        typer.echo(f"  {result.title_name} -- {result.reason}")
    typer.echo("")
    typer.echo(
        "these videos will be refused as members of a playlist until their "
        "source is back on the share and this command has been run again"
    )
    return UNMEASURED_EXIT_CODE


def run_backfill_technical() -> int:
    """Sync entry point for the command registration -- the routine above
    stays async so tests can await it directly with the connection and the
    query stubbed, no event loop management on their side.
    """
    return asyncio.run(backfill_technical())
