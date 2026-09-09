"""Background reaper for delayed title deletion (D-13), delayed per-episode
deletion (plan 10) and expired session cleanup (D-80).

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

Also sweeps stale job heartbeats (D-118, D-130): a running job whose
converter went silent comes back to the queue on its own, up to a named
attempt cap, after which it fails with a reason a person can read -- and,
since exhausting that cap is the one outcome an operator otherwise has no
way to see or act on, the catalog row (a title or a playlist) is marked
failed alongside the job itself (CR-02).

The four sweeps run inside their own `try/except` in `run_reaper`'s loop
body, each isolated from the other three: a failure in the session sweep
must never stop the title reaper's cycle, nor the playlist reaper's, nor
the stale-job sweep's, and in any other pairing of the four.
"""
import asyncio
import logging

import asyncpg

from orchestrator.config import get_settings
from orchestrator.db import queries
from orchestrator.storage import delete_object

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


async def reap_once(pool: asyncpg.Pool) -> None:
    """Sweep both a title's own countdown and every episode's (plan 10) in
    one pass -- `purge_route` (both the title-level and the per-episode
    one, orchestrator.web.routes_titles/routes_episodes) awaits this
    inline so the object is actually gone before the response returns,
    and reusing one function for both grace windows means a purge can
    never race a title's sweep against a separately-scheduled episode
    sweep.
    """
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

        # An episode's own countdown (plan 02/10), collected the same
        # cycle rather than a second background loop: due_for_reaping_episodes
        # names only identifiers (an episode can carry an arbitrary number
        # of editions, the same reason due_for_reaping_playlists gives),
        # episode_object_keys walks them at the point the objects are
        # actually about to go, and finish_delete_episode itself decides
        # tombstone-or-drop from whether any edition of the episode was
        # ever active -- nothing here has to ask that question a second
        # time. Nothing about the title-level sweep above changes.
        for row in await queries.due_for_reaping_episodes(conn):
            episode_id = row["episode_id"]
            keys = await queries.episode_object_keys(conn, episode_id)
            failed = False
            for object_key in keys:
                try:
                    await asyncio.to_thread(delete_object, object_key)
                except Exception:
                    failed = True
                    logger.exception(
                        "reaper: failed to delete object %s for episode %s",
                        object_key,
                        episode_id,
                    )
            if failed:
                # Same retry-next-cycle rule as the title sweep above: an
                # episode still pending_delete with an object left in the
                # bucket must never be recorded as reaped.
                continue
            await queries.finish_delete_episode(conn, episode_id)


async def reap_playlists_once(pool: asyncpg.Pool) -> None:
    """Reclaim what a deleted playlist made.

    Reads `playlist_artifacts` rather than one key per playlist: one
    build mechanism produces a single assembled file and the other a
    segment per member, so "the playlist's object" is not a thing that
    exists. A playlist that never built anything has no artifact rows and
    is finished with no storage call at all.

    Per-object exception isolation and the same finish-only-on-success
    rule the title sweep above states, for the same named reason: a row
    recorded as deleted with bytes still in the bucket leaves an object
    nothing will ever point at or clean up again.

    Two jobs, not one. The second is the generation a rebuild replaced: a
    rebuild marks the previous artifacts superseded rather than deleting
    them inline, so a viewer holding an address handed out before the
    rebuild still gets bytes. That address stays valid for exactly the
    signing lifetime, which is why that is the age passed below --
    reclaiming sooner takes the file out from under somebody mid-evening,
    and never reclaiming means every rebuild leaks a full copy of the list
    onto a stack that has run out of space twice.
    """
    async with pool.acquire() as conn:
        for row in await queries.due_for_reaping_playlists(conn):
            playlist_id = row["playlist_id"]
            failed = False
            for object_key in await queries.playlist_artifact_keys(conn, playlist_id):
                try:
                    await asyncio.to_thread(delete_object, object_key)
                except Exception:
                    failed = True
                    logger.exception(
                        "reaper: failed to delete object %s for playlist %s",
                        object_key,
                        playlist_id,
                    )
            if failed:
                # Stays pending_delete so the next cycle retries.
                continue
            await queries.finish_delete_playlist(conn, playlist_id)

        sign_ttl_seconds = get_settings().sign_ttl_seconds
        for artifact in await queries.due_for_reclaim_artifacts(conn, sign_ttl_seconds):
            try:
                await asyncio.to_thread(delete_object, artifact["object_key"])
            except Exception:
                logger.exception(
                    "reaper: failed to delete superseded artifact %s",
                    artifact["object_key"],
                )
                continue
            await queries.delete_playlist_artifact(conn, artifact["id"])


async def sweep_sessions_once(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        removed = await queries.sweep_expired_sessions(conn)
        if removed:
            logger.info("reaper: swept %d expired session(s)", removed)


# Chosen against the largest of three lower bounds (see the migration's
# own constraint statement in queries.due_for_requeue's caller here): it
# has to be comfortably longer than the interval between a heartbeat and
# the next (every consumer now heartbeats on the order of a few seconds,
# through every stage including a blocking transfer -- CR-01), and
# comfortably longer than the time this process takes to restart. The
# third bound is now the binding one: strictly longer than the
# converter's own report retry window (`worker.client.
# REPORT_RETRY_WINDOW_SECONDS`, 300 seconds -- not imported here, since
# `orchestrator` never imports `worker`; a test asserts this module's own
# figure against that constant directly). Without this bound, a converter
# genuinely still retrying the final report of a finished encode -- the
# one case that retry window exists to survive -- could be requeued
# underneath itself by this same sweep, discarding a completed encode for
# a service outage its own retry was built to tolerate.
#
# The cost is real and is paid on the other failure mode: recovery from a
# genuinely dead converter now takes correspondingly longer. The phase
# measured that recovery once, at the previous 180-second threshold, at
# roughly three and a half minutes end to end (threshold plus one reaper
# poll cycle). At this threshold the same recovery is expected to take
# roughly six and a half minutes -- a slower recovery from a rare failure
# is the right trade against discarding a finished encode during an
# ordinary upload.
JOB_STALE_SECONDS = 360


async def sweep_stale_jobs(pool: asyncpg.Pool) -> None:
    """Return, fail or finish every `running` job whose heartbeat has gone
    stale (D-116, D-130, D-131).

    Three branches per row, and the cancelled check runs first because it
    must win over the other two: a row already flagged for cancellation
    (D-129) ends `cancelled`, never requeued and never failed, even when
    it is also past the attempt cap. This is the case where the operator
    pressed cancel and the converter then died before it could confirm --
    with only the other two branches, that cancellation would silently
    become a requeue and the job the operator stopped would start over on
    a fresh attempt, which looks exactly like the cancel button simply not
    working.

    Composes three queries and isolates their per-row failures; it holds
    no other knowledge of the schema, talks to no converter, and holds no
    lock beyond each individual statement.
    """
    async with pool.acquire() as conn:
        due = await queries.due_for_requeue(
            conn, stale_after_seconds=JOB_STALE_SECONDS
        )
        for row in due:
            job_id = row["id"]
            try:
                if row["cancel_requested"]:
                    await queries.finish_job(
                        conn,
                        job_id,
                        worker_name=row["worker_name"],
                        attempt=row["attempts"],
                        outcome="cancelled",
                    )
                    # A title's address never went live when the worker
                    # that would have finished it died first (T-04-32) --
                    # hard-deleted with no tombstone, the same call the
                    # converter's own /cancelled report and the in-process
                    # remux consumer's cancellation cleanup both use.
                    # Playlist jobs carry no title_id at all and need no
                    # equivalent: a cancelled build simply leaves the
                    # playlist where it was.
                    if row["title_id"] is not None:
                        try:
                            keys = await queries.never_active_title_object_keys(
                                conn, row["title_id"]
                            )
                        except (LookupError, ValueError):
                            keys = []
                        for object_key in keys:
                            await asyncio.to_thread(delete_object, object_key)
                        try:
                            await queries.delete_never_active_title(conn, row["title_id"])
                        except (LookupError, ValueError):
                            pass
                elif row["attempts"] >= queries.JOB_MAX_ATTEMPTS:
                    reason = "failed.reason.crashed_converter"
                    await queries.fail_job_exhausted(conn, job_id, reason_key=reason)
                    # The job row alone is not what an operator sees --
                    # without also writing the catalog row's own failure,
                    # a title stays 'preparing' forever with no live job,
                    # which the library query's join reads as "waiting",
                    # not "failed": no Retry, no Dismiss, just a Cancel
                    # button that answers not-found (CR-02). A playlist
                    # build in the same state says "Building..." forever
                    # with no way to build it again.
                    if row["title_id"] is not None:
                        await queries.mark_title_failed(conn, row["title_id"], reason)
                    elif row["playlist_id"] is not None:
                        # The playlist refusal vocabulary already has a
                        # key for exactly this: a build that did not
                        # finish, previous version still playing. A
                        # converter-specific key would say the same thing
                        # a second way for a build that runs in this same
                        # process.
                        await queries.set_playlist_build_failed(
                            conn, row["playlist_id"], "playlist.refused.assembly", {}
                        )
                else:
                    await queries.requeue_job(conn, job_id)
            except Exception:
                logger.exception("reaper: failed to sweep stale job %s", job_id)


async def run_reaper(pool: asyncpg.Pool) -> None:
    while True:
        try:
            await reap_once(pool)
        except Exception:
            logger.exception("reaper: title-deletion cycle failed")
        try:
            await reap_playlists_once(pool)
        except Exception:
            logger.exception("reaper: playlist-deletion cycle failed")
        try:
            await sweep_sessions_once(pool)
        except Exception:
            logger.exception("reaper: session-sweep cycle failed")
        try:
            await sweep_stale_jobs(pool)
        except Exception:
            logger.exception("reaper: stale-job sweep cycle failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
