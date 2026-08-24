"""The credential CLI commands: `create-operator`, `reset-password`.

The repair path for the account this interface's own `/setup` page
normally creates -- reachable only from inside the already-running `api`
container (D-36), the same trust boundary every other CLI command in this
package already relies on. Neither command gains any authentication of
its own: whoever can run `docker compose exec` already holds Docker and
the environment file, and removing them would delete the one way back in
when the interface itself will not come up. What they gain instead is a
source word on every mutating call, through `_audit` below, so a captured
transcript or log line is distinguishable from an interface line even
when both end up in the same place.
"""
import logging
import uuid

import asyncpg
import typer

from orchestrator.auth import hash_password
from orchestrator.config import get_settings
from orchestrator.db import queries

logger = logging.getLogger(__name__)


async def _connect() -> asyncpg.Connection:
    """Its own copy of `orchestrator.cli.__main__`'s `_connect` -- that
    module imports `_create_operator`/`_reset_password` from here, so
    importing the other way round would be circular. Two lines either way;
    not worth a shared-helper module for.
    """
    return await asyncpg.connect(get_settings().database_url)


def _audit(action: str, detail: str) -> None:
    """One INFO line per mutating CLI call, always starting with the
    literal word `cli` so a captured transcript reads apart from an
    interface log line sharing the same destination (T-02.1-32).

    This process is not the one serving requests -- its output lands
    wherever the caller's own terminal or log capture puts it, never in
    the interface's own stream. The source word is what makes that
    capture distinguishable after the fact; it is not, by itself, a
    unified log. Never pass a password, or any part of one, as `detail`.
    """
    logger.info("cli %s: %s", action, detail)


async def _create_operator(username: str, password: str) -> None:
    """Insert the one operator row. Refuses when a row already exists --
    the interface's own /setup page is the normal way in, and this
    command is the repair path, so silently creating a second row would
    be worse than refusing.

    The check above the insert is a fast path; the unique constraints
    (username, and the one-row singleton index) are the truth. Both mean
    the same thing to a person -- this database already has its operator
    -- and the message does not try to tell them apart.
    """
    conn = await _connect()
    try:
        if await queries.operator_exists(conn):
            typer.echo(
                "An operator account already exists. Use reset-password "
                "to change it instead.",
                err=True,
            )
            raise typer.Exit(1)

        password_hash = await hash_password(password)
        try:
            await queries.create_operator(conn, uuid.uuid4(), username, password_hash)
        except asyncpg.exceptions.UniqueViolationError:
            typer.echo(
                "An operator account already exists. Use reset-password "
                "to change it instead.",
                err=True,
            )
            raise typer.Exit(1)

        _audit("create-operator", f"created operator {username!r}")
        typer.echo(f"Operator {username!r} created. Sign in on the interface address.")
    finally:
        await conn.close()


async def _reset_password(username: str | None, password: str) -> None:
    """Change the stored hash and end every live session in one
    transaction (D-86, `queries.set_password_and_revoke_sessions`) -- a
    password is changed when someone believes it was taken, so leaving
    live sessions alive would leave exactly the access the change was
    meant to remove.

    With no `username`, resolves the single existing operator -- printed
    back always, including then, since with no --username this is the
    one fact the output would otherwise not carry: which account's
    password just changed.
    """
    conn = await _connect()
    try:
        if username is None:
            row = await conn.fetchrow(
                "SELECT id, username FROM operator_credential LIMIT 1"
            )
        else:
            row = await conn.fetchrow(
                "SELECT id, username FROM operator_credential WHERE username = $1",
                username,
            )
        if row is None:
            typer.echo("No operator account exists to reset.", err=True)
            raise typer.Exit(1)

        password_hash = await hash_password(password)
        await queries.set_password_and_revoke_sessions(conn, row["id"], password_hash)

        _audit("reset-password", f"reset password for operator {row['username']!r}")
        typer.echo(
            f"Password for {row['username']!r} changed. Every signed-in device "
            "was signed out."
        )
    finally:
        await conn.close()
