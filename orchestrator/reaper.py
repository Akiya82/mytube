"""Background reaper for delayed title deletion (D-13) and expired session
cleanup (D-80).

Runs inside the same FastAPI process as everything else -- a title crossing
its `delete_at` needs no dedicated compose service, the orchestrator is
already running continuously. The session sweep extends this same loop
rather than starting a second background task (D-80): a session table with
no janitor would grow forever, and the loop that already runs every thirty
seconds is where `sessions_expires_at_idx` gets used.

Every object delete goes through `asyncio.to_thread`: this loop shares its
event loop with every HTTP request in the process, and `purge_route` awaits
`reap_once` inline, so a bare delete stalls `GET /v/{slug}` and `/healthz`
for as long as storage takes to answer.

Never touches the slug history table: a deleted title's slug stays a
tombstone and keeps answering 410 forever (D-10) -- a dead link is honest,
a link that quietly starts playing something else is not.

The two sweeps run inside their own `try/except` in `run_reaper`'s loop
body, each isolated from the other: a failure in the session sweep must
never stop the title reaper's cycle, and the reverse.
"""
import asyncio
import logging

import asyncpg

from orchestrator.db import queries
from orchestrator.storage import delete_object

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


async def reap_once(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        due = await queries.due_for_reaping(conn)
        for row in due:
            title_id = row["title_id"]
            # D-72: a poster left behind when its video goes is the same
            # orphan class this project has already accumulated once with
            # video objects alone -- deleted here, same per-object
            # exception isolation, so one bad key never stops the rest.
            keys = list(row["object_keys"] or []) + list(row["poster_keys"] or [])
            failed = False
            for object_key in keys:
                try:
                    await asyncio.to_thread(delete_object, object_key)
                except Exception:
                    failed = True
                    logger.exception(
                        "reaper: failed to delete object %s for title %s",
                        object_key,
                        title_id,
                    )
            if failed:
                # The title stays pending_delete so the next cycle retries.
                # Flipping it to 'deleted' with bytes still in the bucket
                # would record the work as done and leave an object nothing
                # will ever point at or clean up again -- the orphan class
                # the comment above says this project has already
                # accumulated once.
                continue
            await queries.finish_delete(conn, title_id)


async def sweep_sessions_once(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        removed = await queries.sweep_expired_sessions(conn)
        if removed:
            logger.info("reaper: swept %d expired session(s)", removed)


async def run_reaper(pool: asyncpg.Pool) -> None:
    while True:
        try:
            await reap_once(pool)
        except Exception:
            logger.exception("reaper: title-deletion cycle failed")
        try:
            await sweep_sessions_once(pool)
        except Exception:
            logger.exception("reaper: session-sweep cycle failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
