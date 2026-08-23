"""asyncpg pool lifecycle, shared by main.py's lifespan, the reaper, and the CLI.

No ORM here or in `db.queries` -- five tables total (D-14's deliberately
small skeleton), raw SQL over asyncpg is less code than wiring an ORM for
this size of schema.
"""
import asyncpg

from orchestrator.config import get_settings

# `asyncpg.Pool.acquire()` waits forever by default. Until this phase that
# was invisible, because the only things holding connections were operator
# actions on a LAN-only interface. From this phase on, every gated request
# takes a connection to look up a session, and the interface is on the
# public internet -- so a stall on the database, or simply more operator
# traffic than the pool has connections, turns into viewer requests
# queuing behind it with no bound at all. A viewer would sit on an open
# socket rather than getting an answer, and the stub-clip mechanism at the
# edge never fires because nothing ever returns. The gate's session lookup
# and the delivery routes pass this timeout at their own acquire sites, so
# a saturated pool becomes a fast, deliberate answer instead of an
# unbounded wait.
ACQUIRE_TIMEOUT_SECONDS = 5.0


async def create_pool() -> asyncpg.Pool:
    settings = get_settings()
    return await asyncpg.create_pool(settings.database_url)
