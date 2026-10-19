"""storage-audit: compare the catalog against the bucket in both
directions, read-only.

Two things go wrong independently, and this command is the one place that
checks for both: an object sits in the bucket with no catalog row pointing
at it (space nobody can reclaim without looking), and a catalog row points
at an object the bucket no longer has (an address that resolves, signs and
redirects to nothing -- the viewer gets the stub clip and the product never
says why). `storage.reconcile` is the pure function that tells the two
apart; this module supplies its two arguments and formats the result for a
terminal. It does not define what "unreferenced" means -- the library
header shows the same two counts from the same function, and two
definitions of the same word would eventually disagree.

The catalog side goes through `queries.referenced_objects`, which carries
each key's kind, title name and current slug -- a missing object is named
by the address it breaks, not only by a key nobody recognises. Two
address families reach this report, `/v/` and `/p/`, and `format_address`
below picks between them from the kind the row carries: an operator sent
to a video link that works cannot act on the report, and the playlist
link that actually broke stays hidden.
The bucket side goes through `storage.storage_usage()`, the same
sixty-second cache the library header reads, rather than a second full
enumeration of the bucket -- this module never lists the bucket itself.

This command deletes nothing and aborts nothing. Two decisions make that a
property of the module, not just a habit: no storage-mutating call is
imported here at all, and every branch below only ever prints. When
objects are found unreferenced, the exact removal command is printed for
the operator to run through their own storage client -- a command that
could reclaim space on the strength of a comparison run against the wrong
database pairing is exactly the kind of command this phase's own history
argues against (the active storage endpoint changed once already, mid
phase).
"""
import asyncio

import asyncpg
import typer

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.storage import reconcile, storage_usage

OK_EXIT_CODE = 0
DISCREPANCY_EXIT_CODE = 1

_ADDRESS_PREFIXES = {"playlist": "/p/"}
_DEFAULT_ADDRESS_PREFIX = "/v/"


def format_address(kind: str, slug: str | None) -> str:
    """The public address a referenced object's kind actually lives under.

    Pure, so the report's wording is testable with no database and no
    bucket. A video and its poster share the title's `/v/` address -- the
    poster is not separately addressable and the thing that breaks is the
    title's link either way. A playlist artifact belongs to `/p/`.
    """
    if slug is None:
        return "address unknown (no current slug)"
    return _ADDRESS_PREFIXES.get(kind, _DEFAULT_ADDRESS_PREFIX) + slug


async def storage_audit() -> int:
    """Run the reconciliation and print a three-section report.

    Returns 0 when every referenced object is present and every stored
    object is referenced, 1 when either list is non-empty -- so this can be
    dropped into a smoke script later without changing its shape.
    """
    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        referenced = await queries.referenced_objects(conn)
    finally:
        await conn.close()

    usage = storage_usage()
    result = reconcile(usage.listing, referenced)

    present = len(referenced) - len(result.missing)
    typer.echo(f"present and accounted for in the catalog: {present}")

    typer.echo("")
    if result.unreferenced:
        total = sum(obj.size_bytes for obj in result.unreferenced)
        typer.echo("objects nothing references:")
        for obj in result.unreferenced:
            typer.echo(f"  {obj.key}  {obj.size_bytes} bytes")
        typer.echo(f"total unreferenced: {total} bytes")
        typer.echo("")
        typer.echo(
            "this command deletes nothing; to reclaim this space, "
            "run this for each key:"
        )
        for obj in result.unreferenced:
            typer.echo(
                f"  aws --endpoint-url {settings.s3_endpoint} s3api "
                f"delete-object --bucket {settings.s3_bucket} --key {obj.key}"
            )
    else:
        typer.echo("no unreferenced objects")

    typer.echo("")
    if result.missing:
        typer.echo("addresses whose object is missing from storage:")
        for obj in result.missing:
            address = format_address(obj.kind, obj.slug)
            typer.echo(f"  {address} is broken -- {obj.title_name} ({obj.kind}), key {obj.key}")
    else:
        typer.echo("no broken addresses")

    return DISCREPANCY_EXIT_CODE if (result.unreferenced or result.missing) else OK_EXIT_CODE


def run_storage_audit() -> int:
    """Sync entry point for the typer command -- `storage_audit()` itself
    stays async so tests can await it directly with the connection and the
    catalog query stubbed, no event loop management needed on their side.
    """
    return asyncio.run(storage_audit())
