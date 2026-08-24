"""CLI entrypoint: `docker compose exec api python -m orchestrator.cli <command>`.

Runs only inside the already-running `api` container (D-36) -- nothing is
installed on the host, credentials and environment are the same ones the
orchestrator itself uses. `add` is the only way the first title/slug gets
created (D-35): the point of routing it through here instead of `mc cp` +
a hand-written `INSERT` is that the very first addresses that leave for
VRChat worlds go through normalize_slug (ADDR-06) and get a slug_history
row (ADDR-05) -- skip this and the first links are the ones a hand-rolled
INSERT would get wrong.
"""
import asyncio
import logging
import uuid
from pathlib import Path

import asyncpg
import typer

from orchestrator.cli.auth import _audit, _create_operator, _reset_password
from orchestrator.cli.cache_report import cache_report as _cache_report
from orchestrator.cli.cache_warm import DEFAULT_BASE_URL, cache_warm as _cache_warm
from orchestrator.cli.storage_audit import run_storage_audit as _storage_audit
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import plan_slug
from orchestrator.slugs import SlugError, normalize_slug, pick_unique_slug
from orchestrator.storage import delete_object, put_object_from_file

app = typer.Typer(add_completion=False, no_args_is_help=True)


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(get_settings().database_url)


def _candidate_slug(explicit: str | None, name: str) -> str:
    try:
        return normalize_slug(explicit) if explicit else normalize_slug(name)
    except SlugError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)


@app.command()
def add(
    file: Path = typer.Option(
        ..., "--file", exists=True, dir_okay=False, readable=True,
        help="Path to a file inside the mounted /ingest",
    ),
    name: str = typer.Option(..., "--name", help="Human-readable title name"),
    slug: str = typer.Option(None, "--slug", help="Explicit slug instead of one derived from the name"),
    reuse_slug: bool = typer.Option(
        False, "--reuse-slug",
        help="Confirm reusing a deleted title's slug",
    ),
) -> None:
    """Add a title from a file already on the mounted share, printing its address."""
    asyncio.run(_add(file, name, slug, reuse_slug))


async def _add(file: Path, name: str, explicit_slug: str | None, reuse: bool) -> None:
    conn = await _connect()
    try:
        # plan_slug (orchestrator/media/add.py) is the extracted version of
        # what this function used to compute inline -- the web add form
        # calls the exact same sequence, in the exact same order, so the
        # two never drift. The tombstone-then-reuse decision below and its
        # printed messages are unchanged from before the extraction.
        async def planned():
            try:
                return await plan_slug(conn, name, explicit_slug)
            except SlugError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)

        def refuse_tombstone(plan) -> None:
            if plan.is_tombstone and not reuse:
                typer.echo(
                    f"Slug {plan.candidate!r} belongs to a deleted title -- the address "
                    "may have already spread across worlds. Repeat with --reuse-slug "
                    "to confirm explicitly.",
                    err=True,
                )
                raise typer.Exit(1)

        # Planned twice on purpose. This first pass is a pre-flight with no
        # lock held: an unusable name, or a tombstone the operator has not
        # confirmed, should be refused before the upload spends minutes on
        # a file that is not going to get a row. It decides nothing -- the
        # plan that reaches the catalog is the one read under the lock.
        refuse_tombstone(await planned())

        edition_id = uuid.uuid4()
        object_key = f"{edition_id}.mp4"
        put_object_from_file(str(file), object_key)

        # The upload deliberately sits *outside* this transaction. The
        # advisory lock is shared with the web add route, and holding it
        # across a multi-gigabyte upload would stall every add made through
        # the interface for the whole duration. What has to be inside it is
        # the read-and-insert pair -- see create_title_with_edition's
        # docstring for the race a slug planned before the lock leaves open.
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
            )
            slug_plan = await planned()
            if slug_plan.is_tombstone and not reuse:
                # Another process tombstoned this slug between the
                # pre-flight and the lock. Rare -- but the object is
                # already in the bucket and no row will ever point at it.
                delete_object(object_key)
                refuse_tombstone(slug_plan)

            await queries.create_title_with_edition(
                conn, name, slug_plan.chosen, edition_id, object_key
            )

        _audit("add", f"created title {name!r} at slug {slug_plan.chosen!r}")

        if slug_plan.is_tombstone:
            # --reuse-slug: use the exact requested slug, no suffixing --
            # the whole point is to get this specific address back.
            typer.echo(
                f"Note: slug {slug_plan.chosen!r} was reused -- the previous address "
                "may have spread across worlds pointing at different video."
            )
        elif slug_plan.chosen != slug_plan.candidate:
            typer.echo(f"Slug {slug_plan.candidate!r} taken, used {slug_plan.chosen!r} instead")

        if slug_plan.similar:
            typer.echo(
                f"Warning: a similar slug is already taken: {', '.join(slug_plan.similar)}"
            )

        typer.echo(f"/v/{slug_plan.chosen}")
    finally:
        await conn.close()


@app.command()
def rename(
    slug: str = typer.Argument(..., help="The title's current slug"),
    to: str = typer.Option(..., "--to", help="New name or slug"),
) -> None:
    """Give a title a new address, leaving the old one working."""
    asyncio.run(_rename(slug, to))


async def _rename(old_slug: str, new_name: str) -> None:
    conn = await _connect()
    try:
        candidate = _candidate_slug(None, new_name)

        # Same lock, same reason, same shape as the web rename route: the
        # taken_slugs read and the slug_history insert have to be one
        # serialized pair, or a concurrent add claims the address this
        # rename has just decided is free.
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
            )
            taken = await queries.taken_slugs(conn)
            chosen, similar = pick_unique_slug(candidate, taken)

            try:
                await queries.rename_slug(conn, old_slug, chosen)
            except LookupError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1)

        _audit("rename", f"renamed {old_slug!r} to {chosen!r}")

        if similar:
            typer.echo(f"Warning: a similar slug is already taken: {', '.join(similar)}")

        typer.echo(f"/v/{old_slug} still works")
        typer.echo(f"/v/{chosen} -- new address")
    finally:
        await conn.close()


@app.command()
def remove(slug: str = typer.Argument(..., help="Slug of the title to remove")) -> None:
    """Delete an already-published title after its grace window."""
    asyncio.run(_remove(slug))


async def _remove(slug: str) -> None:
    settings = get_settings()
    conn = await _connect()
    try:
        row = await queries.resolve_slug(conn, slug)
        if row is None or row["status"] != "active":
            typer.echo(f"Slug {slug!r} not found among active titles", err=True)
            raise typer.Exit(1)

        await queries.mark_for_delete(conn, row["title_id"], settings.delete_grace_seconds)
        _audit("remove", f"marked {slug!r} for delete")

        # The grace window is now reachable from both ends through the web
        # interface's library screen: undo restores the title, and delete
        # now skips the wait. The CLI itself still offers neither -- both
        # live in the interface, not as a fifth CLI command.
        typer.echo(
            f"/v/{slug} already answers 410. In {settings.delete_grace_seconds} "
            "seconds the object will be physically deleted and the space "
            "freed. Undo the deletion or delete it immediately from the "
            "web interface's library page."
        )
    finally:
        await conn.close()


@app.command("cache-warm")
def cache_warm_command(
    slug: str = typer.Argument(..., help="Slug of the title to warm before a viewing session"),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL, "--base-url", help="Edge address; usually the internal one from inside the container"
    ),
    chunk_size: int = typer.Option(
        None, "--chunk-size", help="Step size in bytes; defaults to MYTUBE_SLICE_SIZE"
    ),
    bytes: int = typer.Option(
        None, "--bytes",
        help="Warm-up limit in bytes; defaults to MYTUBE_WARM_BYTES "
        "or half of the cache zone",
    ),
) -> None:
    """Pre-fetch a title's opening chunks so playback starts without a cold-cache stall."""
    code = _cache_warm(slug, base_url=base_url, chunk_size=chunk_size, warm_bytes=bytes)
    raise typer.Exit(code)


@app.command("cache-report")
def cache_report_command(
    log_path: str = typer.Argument(
        None, help="Path to blob_access.log; defaults to MYTUBE_BLOB_ACCESS_LOG from settings"
    ),
    since: str = typer.Option(
        None, "--since", help="Only count lines no older than this moment (ISO 8601)"
    ),
) -> None:
    """Count cache hits and misses since a viewing session, exiting non-zero on a double fetch."""
    path = log_path or get_settings().blob_access_log
    code = _cache_report(path, since=since)
    raise typer.Exit(code)


@app.command(
    "storage-audit",
    help="Cross-check the catalog against what storage actually holds, in both "
    "directions. Read-only -- never deletes or changes anything.",
)
def storage_audit_command() -> None:
    raise typer.Exit(_storage_audit())


@app.command("create-operator")
def create_operator_command(
    username: str = typer.Option(..., "--username", help="Username for the new operator"),
) -> None:
    """Create the one operator account this interface will accept."""
    # Prompted, never a command-line argument and never an environment
    # variable: an argument lands in shell history and in the process
    # list of every other container on the machine.
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    asyncio.run(_create_operator(username, password))


@app.command("reset-password")
def reset_password_command(
    username: str = typer.Option(
        None, "--username", help="Operator to reset; defaults to the single existing one"
    ),
) -> None:
    """Change the operator's password and sign every device out."""
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    asyncio.run(_reset_password(username, password))


if __name__ == "__main__":
    # Nothing configured logging for this process before now -- an INFO
    # line from _audit would go nowhere without this.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app()
