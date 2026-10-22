"""CLI entrypoint: `docker compose exec api python -m orchestrator.cli <command>`.

Runs only inside the already-running `api` container -- nothing is
installed on the host, credentials and environment are the same ones the
orchestrator itself uses. `add` is the only way the first title/slug gets
created: the point of routing it through here instead of `mc cp` +
a hand-written `INSERT` is that the very first addresses that leave for
VRChat worlds go through normalize_slug and get a slug_history
row -- skip this and the first links are the ones a hand-rolled
INSERT would get wrong.
"""

import asyncio
import logging
import uuid
from pathlib import Path

import asyncpg
import typer

from orchestrator.cli.auth import _audit, _create_operator, _reset_password
from orchestrator.cli.backfill_technical import (
    run_backfill_technical as _backfill_technical,
)
from orchestrator.cli.cache_report import cache_report as _cache_report
from orchestrator.cli.cache_warm import DEFAULT_BASE_URL
from orchestrator.cli.cache_warm import cache_warm as _cache_warm
from orchestrator.cli.repack import run_repack_layout as _repack_layout
from orchestrator.cli.storage_audit import run_storage_audit as _storage_audit
from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.media.add import (
    EditionSlugTaken,
    plan_edition_slug,
    plan_slug,
    suggest_edition_slug,
)
from orchestrator.slugs import (
    AddressRefused,
    EditionSlugRefused,
    SlugError,
    normalize_slug,
    parse_public_address,
    pick_unique_slug,
)
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
        ...,
        "--file",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a file inside the mounted /ingest",
    ),
    name: str = typer.Option(
        None,
        "--name",
        help="Human-readable title name; required when this file creates a new title",
    ),
    slug: str = typer.Option(
        None, "--slug", help="Explicit slug instead of one derived from the name"
    ),
    reuse_slug: bool = typer.Option(
        False,
        "--reuse-slug",
        help="Confirm reusing a deleted title's slug",
    ),
    title: str = typer.Option(
        None,
        "--title",
        help="Address of an already-added title to add this file to, instead of creating a new one",
    ),
    episode: int = typer.Option(
        None,
        "--episode",
        help="Episode number: this new title's own single episode, a new episode of "
        "--title, or the number of the episode --edition belongs to",
    ),
    edition: str = typer.Option(
        None,
        "--edition",
        help="Slug for a new alternative edition of the episode named by --title and --episode",
    ),
    make_default: bool = typer.Option(
        False,
        "--make-default",
        help="Make the new edition the episode's default address once it exists",
    ),
) -> None:
    """Add a file from the mounted share, printing the address it will answer at.

    With neither --title nor --episode, adds a brand new standalone title,
    unchanged. --episode alone gives the new title's own single episode
    that number. --title and --episode attach a new episode to an
    already-added title. --title, --episode and --edition add a new
    alternative edition of an episode that already exists.
    """
    asyncio.run(
        _add(file, name, slug, reuse_slug, title, episode, edition, make_default)
    )


def _refuse_impossible_option_combination(
    title: str | None, episode: int | None, edition: str | None, make_default: bool
) -> None:
    """Every combination of the new options that cannot mean anything,
    refused before the file is read -- an edition always belongs to an
    episode of a title that already exists (there is nothing to be an
    alternative *of* the moment either is missing), the default flag is
    meaningless without an edition to make default, and naming a title
    with no episode number leaves this file's place in it undecided.
    """
    if edition is not None and (title is None or episode is None):
        typer.echo(
            "--edition needs --title and --episode naming the existing episode "
            "it belongs to.",
            err=True,
        )
        raise typer.Exit(1)
    if make_default and edition is None:
        typer.echo("--make-default needs --edition naming the new edition.", err=True)
        raise typer.Exit(1)
    if title is not None and episode is None:
        typer.echo(
            "--title needs --episode naming which episode of that title this "
            "file becomes.",
            err=True,
        )
        raise typer.Exit(1)


async def _add(
    file: Path,
    name: str | None,
    explicit_slug: str | None,
    reuse: bool,
    title: str | None = None,
    episode: int | None = None,
    edition: str | None = None,
    make_default: bool = False,
) -> None:
    _refuse_impossible_option_combination(title, episode, edition, make_default)

    conn = await _connect()
    try:
        if title is not None and edition is not None:
            await _add_edition_to_existing_episode(
                conn, file, title, episode, edition, make_default
            )
        elif title is not None:
            await _add_new_episode_of_existing_title(conn, file, title, episode)
        else:
            if not name or not name.strip():
                typer.echo("--name is required when creating a new title.", err=True)
                raise typer.Exit(1)
            await _add_new_title(
                conn,
                file,
                name,
                explicit_slug,
                reuse,
                number=episode if episode is not None else 1,
            )
    finally:
        await conn.close()


async def _add_new_title(
    conn: asyncpg.Connection,
    file: Path,
    name: str,
    explicit_slug: str | None,
    reuse: bool,
    number: int,
) -> None:
    """Standalone-title creation -- the CLI's original `add`, unchanged
    except for threading `number` through to `create_title_with_edition`
    (`--episode` alone gives this new title's own single episode a
    real number, so its numbered address works from the first day).
    """

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
            conn, name, slug_plan.chosen, edition_id, object_key, number=number
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
        typer.echo(
            f"Slug {slug_plan.candidate!r} taken, used {slug_plan.chosen!r} instead"
        )

    if slug_plan.similar:
        typer.echo(
            f"Warning: a similar slug is already taken: {', '.join(slug_plan.similar)}"
        )

    typer.echo(f"/v/{slug_plan.chosen}")
    if number != 1:
        typer.echo(f"/v/{slug_plan.chosen}/{number:02d} -- this episode's own address")


async def _add_new_episode_of_existing_title(
    conn: asyncpg.Connection, file: Path, title_slug: str, episode_number: int
) -> None:
    """`--title` and `--episode`, no `--edition`: a new episode of an
    already-added title, through the exact transaction the form's own
    attach offer calls.
    """
    title_row = await queries.title_by_current_slug(conn, title_slug)
    if title_row is None:
        typer.echo(f"No active title currently at /v/{title_slug}", err=True)
        raise typer.Exit(1)
    title_id = title_row["title_id"]

    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    put_object_from_file(str(file), object_key)

    try:
        await queries.attach_episode_to_title(
            conn, title_id, episode_number, edition_id, object_key
        )
    except LookupError as exc:
        delete_object(object_key)
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    except queries.EpisodeNumberTaken:
        delete_object(object_key)
        # Reuse the exact suggestion the form computes rather than
        # inventing a second one here -- this command probes nothing about
        # the file it was about to attach, so the suggestion will usually
        # come back empty (nothing to compare against), and an empty
        # suggestion is itself the honest answer: choose your own slug.
        episode_row = await queries.episode_by_number(conn, title_id, episode_number)
        existing_technical = None
        if episode_row is not None:
            existing_technical = await queries.default_edition_technical(
                conn, episode_row["episode_id"]
            )
        suggestion = suggest_edition_slug(
            existing=dict(existing_technical) if existing_technical else {},
            candidate={},
        )
        hint = f" (suggested: {suggestion!r})" if suggestion else ""
        typer.echo(
            f"Episode {episode_number} of /v/{title_slug} already exists -- add an "
            f"alternate edition instead with --edition{hint}.",
            err=True,
        )
        raise typer.Exit(1)

    _audit("add", f"attached episode {episode_number} to title at /v/{title_slug}")
    typer.echo(f"/v/{title_slug}/{episode_number:02d}")


async def _add_edition_to_existing_episode(
    conn: asyncpg.Connection,
    file: Path,
    title_slug: str,
    episode_number: int,
    edition_slug: str,
    make_default: bool,
) -> None:
    """`--title`, `--episode` and `--edition`: a new alternative edition of
    an episode that already exists, through the exact transaction the
    form's own edition offer calls.
    """
    title_row = await queries.title_by_current_slug(conn, title_slug)
    if title_row is None:
        typer.echo(f"No active title currently at /v/{title_slug}", err=True)
        raise typer.Exit(1)

    episode_row = await queries.episode_by_number(
        conn, title_row["title_id"], episode_number
    )
    if episode_row is None:
        typer.echo(
            f"No episode numbered {episode_number} exists yet for /v/{title_slug} -- "
            f"attach it first with --title {title_slug} --episode {episode_number} "
            "(without --edition).",
            err=True,
        )
        raise typer.Exit(1)
    episode_id = episode_row["episode_id"]

    edition_id = uuid.uuid4()
    object_key = f"{edition_id}.mp4"
    put_object_from_file(str(file), object_key)

    # Same lock, same reason as attach_episode_to_title's own docstring:
    # plan_edition_slug's uniqueness read and add_edition_to_episode's own
    # write have to be one serialized pair, or two operators adding "the
    # Russian dub" at once both see the slug free.
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1)", queries.SLUG_CLAIM_LOCK_KEY
        )
        try:
            chosen_slug = await plan_edition_slug(conn, episode_id, edition_slug)
        except (SlugError, EditionSlugRefused, EditionSlugTaken) as exc:
            delete_object(object_key)
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        try:
            await queries.add_edition_to_episode(
                conn, episode_id, chosen_slug, edition_id, object_key
            )
        except LookupError as exc:
            delete_object(object_key)
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        if make_default:
            await queries.set_default_edition(conn, edition_id)

    _audit(
        "add",
        f"added edition {chosen_slug!r} to episode {episode_number} of /v/{title_slug}",
    )
    typer.echo(f"/v/{title_slug}/{episode_number:02d} -- the episode's own address")
    typer.echo(f"/v/{title_slug}/{episode_number:02d}/{chosen_slug} -- the new edition")


@app.command()
def rename(
    slug: str = typer.Argument(
        ..., help="The title's current address (a bare title only -- see --help)"
    ),
    to: str = typer.Option(..., "--to", help="New name or slug"),
) -> None:
    """Give a title a new address, leaving the old one working.

    Only a bare title address can be renamed. A numbered or an edition
    address is refused -- an episode's own number is changed by the
    renumber form, and an edition's slug is fixed once the edition exists.
    """
    asyncio.run(_rename(slug, to))


async def _rename(address_text: str, new_name: str) -> None:
    conn = await _connect()
    try:
        try:
            address = parse_public_address(address_text)
        except AddressRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        if address.number is not None or address.edition_slug is not None:
            typer.echo(
                "Only a title address can be renamed. An episode's own number "
                "is changed by the renumber form, not by renaming it, and an "
                "edition's slug is fixed once the edition exists.",
                err=True,
            )
            raise typer.Exit(1)
        old_slug = address.slug

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
            typer.echo(
                f"Warning: a similar slug is already taken: {', '.join(similar)}"
            )

        typer.echo(f"/v/{old_slug} still works")
        typer.echo(f"/v/{chosen} -- new address")
    finally:
        await conn.close()


@app.command()
def remove(
    slug: str = typer.Argument(
        ..., help="Address to remove: a bare title, a numbered episode, or an edition"
    ),
) -> None:
    """Delete an already-published title, episode or edition after its
    grace window.

    A bare title address removes the whole title. A numbered address
    removes that one episode with all its editions. An edition address
    removes that one edition, or the whole episode when it was the only
    one left.
    """
    asyncio.run(_remove(slug))


async def _remove(address_text: str) -> None:
    settings = get_settings()
    conn = await _connect()
    try:
        try:
            address = parse_public_address(address_text)
        except AddressRefused as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

        if address.edition_slug is not None:
            await _remove_edition(conn, settings, address)
        elif address.number is not None:
            await _remove_episode(conn, settings, address)
        else:
            await _remove_title(conn, settings, address.slug)
    finally:
        await conn.close()


async def _remove_title(conn: asyncpg.Connection, settings, slug: str) -> None:
    """A bare title address: exactly today's whole-title removal,
    unchanged.
    """
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


async def _episode_for_removal(conn: asyncpg.Connection, address) -> tuple:
    """The title id and the target episode row a numbered or an edition
    address names -- shared by `_remove_episode` and `_remove_edition`,
    since both start by resolving exactly the same thing: an active title,
    then the episode `address` actually points at (by number when it gave
    one, the title's own lowest episode otherwise -- the two-segment
    edition address).
    """
    title_row = await queries.resolve_slug(conn, address.slug)
    if title_row is None or title_row["status"] != "active":
        typer.echo(f"Slug {address.slug!r} not found among active titles", err=True)
        raise typer.Exit(1)

    if address.number is not None:
        episode_row = await queries.episode_by_number(
            conn, title_row["title_id"], address.number
        )
    else:
        episode_row = await queries.lowest_episode_of_title(conn, title_row["title_id"])

    if episode_row is None or episode_row["episode_status"] != "active":
        typer.echo(f"No active episode found at /v/{address.slug}", err=True)
        raise typer.Exit(1)

    return title_row["title_id"], episode_row


def _canonical_address(address) -> str:
    parts = [f"/v/{address.slug}"]
    if address.number is not None:
        parts.append(f"{address.number:02d}")
    if address.edition_slug is not None:
        parts.append(address.edition_slug)
    return "/".join(parts)


async def _remove_episode(conn: asyncpg.Connection, settings, address) -> None:
    """A numbered address: that one episode, with every edition it holds,
    on the episode-scoped countdown -- the exact twin of
    `_remove_title`, one level down.
    """
    _title_id, episode_row = await _episode_for_removal(conn, address)
    canonical = _canonical_address(address)
    typer.echo(canonical)

    await queries.mark_episode_for_delete(
        conn, episode_row["episode_id"], settings.delete_grace_seconds
    )
    _audit("remove", f"marked episode at {canonical} for delete")

    typer.echo(
        f"{canonical} already answers 410. In {settings.delete_grace_seconds} "
        "seconds its objects will be physically deleted and the space "
        "freed. Undo the deletion or delete it immediately from the web "
        "interface's library page."
    )


async def _remove_edition(conn: asyncpg.Connection, settings, address) -> None:
    """An edition address: that one edition, refusing when it is the
    episode's default and other editions exist, and removing the whole
    episode along with it when it was the only edition left -- the same
    thing the interface does.
    """
    _title_id, episode_row = await _episode_for_removal(conn, address)
    edition_row = await queries.edition_by_slug(
        conn, episode_row["episode_id"], address.edition_slug
    )
    if edition_row is None:
        typer.echo(
            f"No edition {address.edition_slug!r} found on that episode", err=True
        )
        raise typer.Exit(1)

    canonical = _canonical_address(address)
    typer.echo(canonical)

    if edition_row["is_default"] and edition_row["edition_count"] > 1:
        typer.echo(
            f"{canonical} is the episode's default edition -- make another "
            "edition the default first.",
            err=True,
        )
        raise typer.Exit(1)

    if edition_row["edition_count"] <= 1:
        # The only edition left: remove the whole episode with it, on the
        # same episode-scoped countdown a numbered address's own removal
        # already uses -- no address of this episode should keep answering
        # once its one and only edition is gone.
        await queries.mark_episode_for_delete(
            conn, episode_row["episode_id"], settings.delete_grace_seconds
        )
        _audit(
            "remove",
            f"marked the only edition at {canonical} for delete, with its episode",
        )
        typer.echo(
            f"{canonical} was the only edition -- its episode is now scheduled for "
            f"delete too. In {settings.delete_grace_seconds} seconds its objects "
            "will be physically deleted and the space freed. Undo the deletion or "
            "delete it immediately from the web interface's library page."
        )
        return

    # A non-default edition with a sibling still standing: no grace window
    # exists for a single edition (the schema carries no per-edition
    # delete_at, unlike a whole title or episode) -- removed immediately.
    if edition_row["object_key"]:
        delete_object(edition_row["object_key"])
    if edition_row["poster_key"]:
        delete_object(edition_row["poster_key"])
    await queries.delete_edition(conn, edition_row["edition_id"])
    _audit("remove", f"deleted edition at {canonical}")

    typer.echo(f"{canonical} deleted -- its episode's other editions are unaffected.")


@app.command("cache-warm")
def cache_warm_command(
    slug: str = typer.Argument(
        ..., help="Slug of the title to warm before a viewing session"
    ),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL,
        "--base-url",
        help="Edge address; usually the internal one from inside the container",
    ),
    chunk_size: int = typer.Option(
        None, "--chunk-size", help="Step size in bytes; defaults to MYTUBE_SLICE_SIZE"
    ),
    bytes: int = typer.Option(
        None,
        "--bytes",
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
        None,
        help="Path to blob_access.log; defaults to MYTUBE_BLOB_ACCESS_LOG from settings",
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


@app.command(
    "backfill-technical",
    help="Measure the stream parameters of videos added before the catalog "
    "recorded them, so they can be checked for joinability. Reads their "
    "sources on the share; changes no file and no stored object.",
)
def backfill_technical_command() -> None:
    raise typer.Exit(_backfill_technical())


@app.command(
    "repack-layout",
    help="Rewrite stored videos audio first and without a chapter track, the "
    "layout VRChat's player fetches over one connection. Copies streams, never "
    "re-encodes; newest additions first. A video watched within the last 70 "
    "minutes is left for a later run.",
)
def repack_layout_command() -> None:
    raise typer.Exit(_repack_layout())


@app.command("create-operator")
def create_operator_command(
    username: str = typer.Option(
        ..., "--username", help="Username for the new operator"
    ),
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
        None,
        "--username",
        help="Operator to reset; defaults to the single existing one",
    ),
) -> None:
    """Change the operator's password and sign every device out."""
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    asyncio.run(_reset_password(username, password))


if __name__ == "__main__":
    # Nothing configured logging for this process before now -- an INFO
    # line from _audit would go nowhere without this.
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    app()
