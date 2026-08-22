"""Background reaper for delayed title deletion (D-13).

Runs inside the same FastAPI process as everything else -- a title crossing
its `delete_at` needs no dedicated compose service, the orchestrator is
already running continuously.

Never touches the slug history table: a deleted title's slug stays a
tombstone and keeps answering 410 forever (D-10) -- a dead link is honest,
a link that quietly starts playing something else is not.
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
                    delete_object(object_key)
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


async def run_reaper(pool: asyncpg.Pool) -> None:
    while True:
        try:
            await reap_once(pool)
        except Exception:
            logger.exception("reaper: cycle failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
