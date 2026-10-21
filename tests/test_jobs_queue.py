"""The durable queue's whole lifecycle -- enqueue, claim under concurrency,
heartbeat, finish, cancel, requeue, the attempts cap, worker check-in, and
per-kind counting -- driven against a real migrated Postgres, because the
behaviour under test (row visibility across two connections, a check
constraint, a stale-heartbeat sweep) is not something a connection double
can exhibit.

Only the six claiming tests are marked `integration`:
they are the ones that genuinely need two independent, committed
connections racing against the real `FOR UPDATE SKIP LOCKED` query --
a rollback-wrapped `db_conn` cannot exhibit cross-connection visibility at
all. Every other test here also happens to need the real migrated
Postgres this session's `db_conn` fixture wraps, but is testing one
connection's own view of one row rather than a race -- unmarked, per this
module's own contract, so `-m "not integration"` still exercises the bulk
of this file's failing assertions.
"""

import asyncio
import re
import uuid

import asyncpg
import pytest

from orchestrator.db import queries

CLAIM_TIMEOUT_SECONDS = 5.0


def _slug(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


async def _title_and_edition(conn, tag: str = "jobq") -> tuple:
    """A real title+edition row -- `enqueue_job`'s `title_id`/`edition_id`
    are real foreign keys once the schema exists, so setup builds them the
    same way `tests/test_queries.py` does.
    """
    edition_id = uuid.uuid4()
    title_id = await queries.create_title_with_edition(
        conn,
        f"Job Queue {tag}",
        _slug(tag),
        edition_id,
        f"{edition_id}.mp4",
        status="preparing",
    )
    return title_id, edition_id


async def _playlist(conn, tag: str = "jobq") -> uuid.UUID:
    return await queries.create_playlist(
        conn, f"Job Queue Playlist {tag}", _slug(f"{tag}-pl")
    )


async def _reset_jobs_tables(conn) -> None:
    """Truncated before and after: this file's tests share the one
    session-scoped Postgres every other test module in this run shares,
    and a committed claim/heartbeat row from one test is a precondition
    the next test does not expect.
    """
    try:
        await conn.execute("TRUNCATE jobs, worker_checkins CASCADE")
    except asyncpg.PostgresError:
        pass


@pytest.fixture(autouse=True)
async def _clean_jobs_before_and_after(migrated_db_url):
    conn = await asyncpg.connect(migrated_db_url)
    try:
        await _reset_jobs_tables(conn)
        yield
        await _reset_jobs_tables(conn)
    finally:
        await conn.close()


@pytest.fixture
async def worker_conns(migrated_db_url):
    """Two independent, autocommitting connections to the same migrated
    Postgres -- what the claiming tests need to prove the claim
    is visible across sessions, which the suite's rollback-wrapped
    `db_conn` cannot exhibit.
    """
    conn_a = await asyncpg.connect(migrated_db_url)
    conn_b = await asyncpg.connect(migrated_db_url)
    try:
        yield conn_a, conn_b
    finally:
        await conn_a.close()
        await conn_b.close()


# --- Enqueue and shape -------------------------------------------------


async def test_a_queued_job_starts_with_no_worker_and_no_heartbeat(db_conn):
    title_id, edition_id = await _title_and_edition(db_conn)
    job_id = await queries.enqueue_job(
        db_conn,
        kind="transcode",
        title_id=title_id,
        edition_id=edition_id,
        source_path="Movies/source.mkv",
        audio_stream_index=1,
        subtitle_kind="embedded_text",
        subtitle_stream_index=2,
        stereo_layout="2d",
    )

    row = await db_conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    assert row["status"] == "queued"
    assert row["stage"] == "queued"
    assert row["attempts"] == 0
    assert row["worker_name"] is None
    assert row["heartbeat_at"] is None
    assert row["cancel_requested"] is False


async def test_the_kind_column_refuses_a_kind_the_application_does_not_know(db_conn):
    """The check constraint is the thing that stops a typo in a future
    call site from creating a job nothing will ever consume.
    """
    title_id, edition_id = await _title_and_edition(db_conn, "bad-kind")
    with pytest.raises(asyncpg.CheckViolationError):
        await db_conn.execute(
            """
            INSERT INTO jobs (kind, status, stage, title_id, edition_id)
            VALUES ('upload', 'queued', 'queued', $1, $2)
            """,
            title_id,
            edition_id,
        )


async def test_a_transcode_job_carries_the_operator_s_choices_and_nothing_derived(
    db_conn,
):
    """The worker re-probes the file as it is at claim time and builds its
    own command, so shipping a command across the network would only
    guarantee the two disagree -- this row stores only what the operator
    chose, nothing ffmpeg-shaped.
    """
    title_id, edition_id = await _title_and_edition(db_conn, "choices")
    job_id = await queries.enqueue_job(
        db_conn,
        kind="transcode",
        title_id=title_id,
        edition_id=edition_id,
        source_path="Movies/choices.mkv",
        audio_stream_index=2,
        subtitle_kind="external",
        subtitle_path="Movies/choices.ru.srt",
        stereo_layout="sbs_full",
    )

    row = await db_conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    columns = set(row.keys())
    assert row["source_path"] == "Movies/choices.mkv"
    assert row["audio_stream_index"] == 2
    assert row["subtitle_kind"] == "external"
    assert row["subtitle_path"] == "Movies/choices.ru.srt"
    assert row["stereo_layout"] == "sbs_full"
    assert not columns & {"argv", "filter_chain", "encoder_flags", "command"}


# --- Claiming -----------------------------------------------------------


@pytest.mark.integration
async def test_two_concurrent_claims_never_get_the_same_job(worker_conns):
    conn_a, conn_b = worker_conns
    title_x = await _title_and_edition(conn_a, "claim-x")
    title_y = await _title_and_edition(conn_a, "claim-y")
    job_x = await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title_x[0], edition_id=title_x[1]
    )
    job_y = await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title_y[0], edition_id=title_y[1]
    )

    claimed_a, claimed_b = await asyncio.wait_for(
        asyncio.gather(
            queries.claim_job(conn_a, kind="transcode", worker_name="worker-a"),
            queries.claim_job(conn_b, kind="transcode", worker_name="worker-b"),
        ),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )

    assert claimed_a is not None
    assert claimed_b is not None
    assert claimed_a["id"] != claimed_b["id"]
    assert {claimed_a["id"], claimed_b["id"]} == {job_x, job_y}


@pytest.mark.integration
async def test_a_second_claim_finds_nothing_when_only_one_job_is_queued(worker_conns):
    conn_a, conn_b = worker_conns
    title = await _title_and_edition(conn_a, "claim-solo")
    await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title[0], edition_id=title[1]
    )

    first, second = await asyncio.wait_for(
        asyncio.gather(
            queries.claim_job(conn_a, kind="transcode", worker_name="worker-a"),
            queries.claim_job(conn_b, kind="transcode", worker_name="worker-b"),
        ),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )

    claims = [first, second]
    assert sum(1 for c in claims if c is not None) == 1
    assert sum(1 for c in claims if c is None) == 1


@pytest.mark.integration
async def test_a_claim_does_not_hold_the_row_lock_while_the_caller_works(worker_conns):
    conn_a, conn_b = worker_conns
    title_a = await _title_and_edition(conn_a, "claim-lock-a")
    title_b = await _title_and_edition(conn_a, "claim-lock-b")
    await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title_a[0], edition_id=title_a[1]
    )
    await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title_b[0], edition_id=title_b[1]
    )

    claimed_a = await asyncio.wait_for(
        queries.claim_job(conn_a, kind="transcode", worker_name="worker-a"),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )
    assert claimed_a is not None
    # conn_a never commits/closes/does anything else here -- if the claim's
    # own transaction were still open around this "work", the second claim
    # below would either see it (skip-locked hands B a different row
    # regardless of whether A committed) or the read after it would still
    # see the pre-claim, uncommitted 'queued' state. Both are checked.

    claimed_b = await asyncio.wait_for(
        queries.claim_job(conn_b, kind="transcode", worker_name="worker-b"),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )
    assert claimed_b is not None
    assert claimed_b["id"] != claimed_a["id"]

    # The discriminating assertion: a write visible from another session is
    # a write that committed. A claim implemented as one open transaction
    # spanning the whole encode passes every assertion above -- only this
    # cross-session read of A's own claimed row fails it.
    seen_from_b = await asyncio.wait_for(
        conn_b.fetchrow(
            "SELECT status, worker_name, attempts FROM jobs WHERE id = $1",
            claimed_a["id"],
        ),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )
    assert seen_from_b["status"] == "running"
    assert seen_from_b["worker_name"] == "worker-a"
    assert seen_from_b["attempts"] == 1


@pytest.mark.integration
async def test_a_claim_is_filtered_by_kind(worker_conns):
    conn_a, _ = worker_conns
    title = await _title_and_edition(conn_a, "claim-kind")
    playlist_id = await _playlist(conn_a, "claim-kind")
    remux_id = await queries.enqueue_job(
        conn_a, kind="remux", title_id=title[0], edition_id=title[1]
    )
    playlist_job_id = await queries.enqueue_job(
        conn_a, kind="playlist", playlist_id=playlist_id
    )
    transcode_id = await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title[0], edition_id=title[1]
    )

    claimed = await asyncio.wait_for(
        queries.claim_job(conn_a, kind="transcode", worker_name="worker-a"),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )
    assert claimed["id"] == transcode_id

    remaining = await conn_a.fetch(
        "SELECT id, status FROM jobs WHERE id = ANY($1::uuid[])",
        [remux_id, playlist_job_id],
    )
    assert all(row["status"] == "queued" for row in remaining)

    claimed_remux = await asyncio.wait_for(
        queries.claim_job(conn_a, kind="remux", worker_name="worker-a"),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )
    assert claimed_remux["id"] == remux_id


@pytest.mark.integration
async def test_claiming_records_the_worker_name_and_raises_the_attempt(worker_conns):
    conn_a, _ = worker_conns
    title = await _title_and_edition(conn_a, "claim-record")
    await queries.enqueue_job(
        conn_a, kind="transcode", title_id=title[0], edition_id=title[1]
    )

    claimed = await asyncio.wait_for(
        queries.claim_job(conn_a, kind="transcode", worker_name="ml-server"),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )

    assert claimed["worker_name"] == "ml-server"
    assert claimed["attempts"] == 1
    assert claimed["status"] == "running"
    assert claimed["stage"] == "working"
    assert claimed["heartbeat_at"] is not None


@pytest.mark.integration
async def test_claiming_an_empty_queue_returns_nothing_rather_than_raising(
    worker_conns,
):
    conn_a, _ = worker_conns
    claimed = await asyncio.wait_for(
        queries.claim_job(conn_a, kind="transcode", worker_name="ml-server"),
        timeout=CLAIM_TIMEOUT_SECONDS,
    )
    assert claimed is None


# --- Heartbeat and cancellation -----------------------------------------


@pytest.mark.parametrize("stage", ["working", "uploading"])
async def test_a_heartbeat_advances_the_timestamp_the_progress_and_the_stage(
    db_conn, stage
):
    title = await _title_and_edition(db_conn, f"hb-{stage}")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    updated = await queries.heartbeat_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        stage=stage,
        progress_pct=42.5,
        eta_seconds=120,
        realtime_factor=0.98,
    )

    assert updated is not None
    assert updated["stage"] == stage
    assert updated["progress_pct"] == pytest.approx(42.5)
    assert updated["eta_seconds"] == 120
    assert updated["heartbeat_at"] is not None


async def test_a_heartbeat_from_a_superseded_attempt_is_refused(db_conn):
    title = await _title_and_edition(db_conn, "hb-superseded")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    before = await db_conn.fetchrow(
        "SELECT progress_pct, heartbeat_at FROM jobs WHERE id = $1", job_id
    )

    result = await queries.heartbeat_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"] - 1,
        stage="working",
        progress_pct=99.0,
    )

    assert result is None
    after = await db_conn.fetchrow(
        "SELECT progress_pct, heartbeat_at FROM jobs WHERE id = $1", job_id
    )
    assert after["progress_pct"] == before["progress_pct"]
    assert after["heartbeat_at"] == before["heartbeat_at"]


async def test_a_heartbeat_reports_the_cancel_flag_back_to_the_caller(db_conn):
    title = await _title_and_edition(db_conn, "hb-cancel-flag")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    before = await queries.heartbeat_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        stage="working",
    )
    assert before["cancel_requested"] is False

    await queries.request_job_cancel(db_conn, job_id)

    after = await queries.heartbeat_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        stage="working",
    )
    assert after["cancel_requested"] is True


async def test_requesting_cancellation_on_a_queued_job_can_finish_it_immediately(
    db_conn,
):
    """A queued job has no worker to tell -- cancelling it finishes it on
    the spot, distinct from the running case which only sets the flag.
    """
    title = await _title_and_edition(db_conn, "cancel-queued")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )

    result = await queries.request_job_cancel(db_conn, job_id)

    assert result["status"] == "cancelled"
    row = await db_conn.fetchrow(
        "SELECT status, worker_name FROM jobs WHERE id = $1", job_id
    )
    assert row["status"] == "cancelled"
    assert row["worker_name"] is None


# --- Finishing ----------------------------------------------------------


@pytest.mark.parametrize("outcome", ["done", "failed"])
async def test_finishing_a_job_records_the_outcome_and_the_realtime_factor(
    db_conn, outcome
):
    title = await _title_and_edition(db_conn, f"finish-{outcome}")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    reason_key = "failed.reason.upload" if outcome == "failed" else None
    ok = await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        outcome=outcome,
        reason_key=reason_key,
        realtime_factor=1.4,
    )

    assert ok is True
    row = await db_conn.fetchrow(
        "SELECT status, finished_at, failure_reason, realtime_factor FROM jobs WHERE id = $1",
        job_id,
    )
    assert row["status"] == outcome
    assert row["finished_at"] is not None
    if outcome == "failed":
        assert row["failure_reason"] == reason_key
    else:
        assert row["failure_reason"] is None


async def test_a_report_from_a_superseded_attempt_is_refused(db_conn):
    """Both halves are asserted, because the dangerous failure is the
    silent write, not the false return.
    """
    title = await _title_and_edition(db_conn, "finish-superseded")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    before = await db_conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)

    ok = await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"] - 1,
        outcome="done",
    )

    assert ok is False
    after = await db_conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)
    assert after["status"] == before["status"]


async def test_a_late_report_against_an_already_finished_job_changes_nothing(db_conn):
    """The guard the superseded-attempt test above pins (a wrong attempt)
    is not the only way a stale report can reach this function: the same
    worker and the same attempt, reporting a second time after the job
    has already finished, must be refused too -- a duplicate `done`, or a
    `failed` that arrives after a `done` already committed. Only the
    added `status = 'running'` condition catches this one; the worker/
    attempt guard alone would let it through.
    """
    title = await _title_and_edition(db_conn, "finish-already-done")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    first = await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        outcome="done",
    )
    assert first is True

    before = await db_conn.fetchrow(
        "SELECT status, failure_reason FROM jobs WHERE id = $1", job_id
    )

    replay = await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        outcome="failed",
        reason_key="failed.reason.unknown",
    )

    assert replay is False
    after = await db_conn.fetchrow(
        "SELECT status, failure_reason FROM jobs WHERE id = $1", job_id
    )
    assert after["status"] == before["status"] == "done"
    assert after["failure_reason"] == before["failure_reason"] is None


async def test_a_progress_write_against_a_job_that_is_no_longer_running_returns_nothing(
    db_conn,
):
    title = await _title_and_edition(db_conn, "heartbeat-already-done")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )
    await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        outcome="done",
    )

    result = await queries.heartbeat_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        stage="working",
        progress_pct=50.0,
    )

    assert result is None


async def test_a_raise_after_the_terminal_write_inside_one_transaction_leaves_the_job_running(
    db_conn,
):
    """The primitive every atomic-write caller (the success
    route, the add consumer, the playlist consumer) relies on: a failure
    anywhere else inside the same `conn.transaction()` rolls the terminal
    write back along with it, so a job never ends up `done` with its own
    catalog row left behind by a write that failed a moment later.
    """
    title = await _title_and_edition(db_conn, "atomic-rollback")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    with pytest.raises(RuntimeError):
        async with db_conn.transaction():
            wrote = await queries.finish_job(
                db_conn,
                job_id,
                worker_name="ml-server",
                attempt=claimed["attempts"],
                outcome="done",
            )
            assert wrote is True
            raise RuntimeError("the catalog write that follows exploded")

    row = await db_conn.fetchrow(
        "SELECT status, finished_at FROM jobs WHERE id = $1", job_id
    )
    assert row["status"] == "running"
    assert row["finished_at"] is None


async def test_a_failure_reason_is_stored_as_a_catalog_key(db_conn):
    title = await _title_and_edition(db_conn, "finish-catalog-key")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )

    await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        outcome="failed",
        reason_key="failed.reason.crashed_converter",
    )

    row = await db_conn.fetchrow(
        "SELECT failure_reason FROM jobs WHERE id = $1", job_id
    )
    reason = row["failure_reason"]
    assert re.match(r"^[a-z]+(\.[a-z_]+)+$", reason)
    assert " " not in reason


# --- Requeue and the attempts cap ---------------------------------------


async def test_a_running_job_past_the_stale_threshold_is_due_for_requeue(db_conn):
    title_stale = await _title_and_edition(db_conn, "stale-due")
    stale_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title_stale[0], edition_id=title_stale[1]
    )
    await queries.claim_job(db_conn, kind="transcode", worker_name="ml-server")
    await db_conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
        stale_id,
    )

    title_fresh = await _title_and_edition(db_conn, "fresh-not-due")
    fresh_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title_fresh[0], edition_id=title_fresh[1]
    )
    await queries.claim_job(db_conn, kind="transcode", worker_name="ml-server")

    title_queued = await _title_and_edition(db_conn, "still-queued")
    queued_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title_queued[0], edition_id=title_queued[1]
    )
    await db_conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
        queued_id,
    )

    due = await queries.due_for_requeue(db_conn, stale_after_seconds=60)
    due_ids = {row["id"] for row in due}

    assert stale_id in due_ids
    assert fresh_id not in due_ids
    assert queued_id not in due_ids


async def test_a_requeued_job_returns_to_the_queue_with_its_worker_cleared(db_conn):
    title = await _title_and_edition(db_conn, "requeue-clear")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )
    await queries.heartbeat_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        stage="working",
        progress_pct=55.0,
    )

    await queries.requeue_job(db_conn, job_id)

    row = await db_conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    assert row["status"] == "queued"
    assert row["stage"] == "queued"
    assert row["worker_name"] is None
    assert row["progress_pct"] is None
    assert row["attempts"] == claimed["attempts"]


async def test_the_fourth_attempt_is_a_named_failure_rather_than_a_fifth_requeue(
    db_conn,
):
    title = await _title_and_edition(db_conn, "attempts-cap")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )

    for attempt in range(1, queries.JOB_MAX_ATTEMPTS + 1):
        claimed = await queries.claim_job(
            db_conn, kind="transcode", worker_name="ml-server"
        )
        assert claimed["attempts"] == attempt
        await db_conn.execute(
            "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
            job_id,
        )
        due = await queries.due_for_requeue(db_conn, stale_after_seconds=60)
        assert any(row["id"] == job_id for row in due)
        if attempt < queries.JOB_MAX_ATTEMPTS:
            await queries.requeue_job(db_conn, job_id)
        else:
            await queries.fail_job_exhausted(
                db_conn, job_id, reason_key="failed.reason.crashed_converter"
            )

    final = await db_conn.fetchrow(
        "SELECT status, attempts, failure_reason FROM jobs WHERE id = $1", job_id
    )
    assert final["status"] == "failed"
    assert final["attempts"] == queries.JOB_MAX_ATTEMPTS
    assert final["failure_reason"] == "failed.reason.crashed_converter"


async def test_a_stale_job_that_was_already_cancelled_finishes_as_cancelled_not_requeued(
    db_conn,
):
    """A cancellation has two endings -- the converter
    confirming it, or the reaper giving up on the converter. Left out, an
    operator's cancellation silently becomes a requeue and the job they
    stopped starts again on another attempt.
    """
    title = await _title_and_edition(db_conn, "stale-cancelled")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )
    await queries.request_job_cancel(db_conn, job_id)
    await db_conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1", job_id
    )

    due = await queries.due_for_requeue(db_conn, stale_after_seconds=60)
    assert any(row["id"] == job_id for row in due)

    row = await db_conn.fetchrow(
        "SELECT cancel_requested FROM jobs WHERE id = $1", job_id
    )
    assert row["cancel_requested"] is True

    # The reaper's own sweep (the reaper's concern, orchestrator.reaper)
    # reads cancel_requested off exactly this row and calls finish_job with
    # outcome="cancelled" instead of requeue_job -- asserted directly
    # against the query primitive the sweep will call.
    ok = await queries.finish_job(
        db_conn,
        job_id,
        worker_name="ml-server",
        attempt=claimed["attempts"],
        outcome="cancelled",
    )
    assert ok is True

    final = await db_conn.fetchrow(
        "SELECT status, attempts, failure_reason FROM jobs WHERE id = $1", job_id
    )
    assert final["status"] == "cancelled"
    assert final["attempts"] == claimed["attempts"]
    assert final["failure_reason"] is None


# --- The exhausted branch also fails the catalog row --------------------


class _SingleAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class _SingleConnPool:
    """The one `.acquire()` shape `reaper.sweep_stale_jobs` asks an
    `asyncpg.Pool` for, wrapping this file's own single, rollback-at-
    teardown `db_conn` -- so the reaper's real sweep runs against exactly
    the same row every other test in this file reads and writes.
    """

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _SingleAcquireCtx(self._conn)


async def test_three_stale_sweeps_leave_a_transcode_job_s_title_failed_with_a_working_retry(
    db_conn,
):
    from orchestrator import reaper

    title = await _title_and_edition(db_conn, "exhausted-title")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    pool = _SingleConnPool(db_conn)

    for attempt in range(1, queries.JOB_MAX_ATTEMPTS + 1):
        claimed = await queries.claim_job(
            db_conn, kind="transcode", worker_name="ml-server"
        )
        assert claimed["attempts"] == attempt
        await db_conn.execute(
            "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
            job_id,
        )
        await reaper.sweep_stale_jobs(pool)

        job_row = await db_conn.fetchrow(
            "SELECT status, attempts FROM jobs WHERE id = $1", job_id
        )
        if attempt < queries.JOB_MAX_ATTEMPTS:
            assert job_row["status"] == "queued"
            assert job_row["attempts"] == attempt
        else:
            assert job_row["status"] == "failed"

    title_row = await db_conn.fetchrow(
        "SELECT status, failure_reason FROM titles WHERE id = $1", title[0]
    )
    assert title_row["status"] == "failed"
    assert title_row["failure_reason"] == "failed.reason.crashed_converter"

    # The same two checks `routes_titles.retry_route` itself performs
    # before accepting a retry request -- a failed status, and a last job
    # to copy the payload from. Both holding is what "accepted, not
    # answered not-found" means at this layer, without standing up the
    # whole app to make the HTTP call.
    library_row = await queries.library_row(db_conn, title[0])
    assert library_row["title_status"] == "failed"
    last_job = await queries.last_job_for_title(db_conn, title[0])
    assert last_job is not None


async def test_three_stale_sweeps_leave_a_playlist_build_carrying_a_failure(db_conn):
    from orchestrator import reaper

    playlist_id = await _playlist(db_conn, "exhausted-playlist")
    job_id = await queries.enqueue_job(
        db_conn, kind="playlist", playlist_id=playlist_id
    )
    pool = _SingleConnPool(db_conn)

    for _attempt in range(1, queries.JOB_MAX_ATTEMPTS + 1):
        await queries.claim_job(db_conn, kind="playlist", worker_name="ml-server")
        await db_conn.execute(
            "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
            job_id,
        )
        await reaper.sweep_stale_jobs(pool)

    job_row = await db_conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)
    assert job_row["status"] == "failed"

    playlist_row = await db_conn.fetchrow(
        "SELECT build_failure_reason FROM playlists WHERE id = $1", playlist_id
    )
    assert playlist_row["build_failure_reason"] == "playlist.refused.assembly"


async def test_a_cancelled_job_past_the_attempt_cap_still_ends_cancelled_not_failed(
    db_conn,
):
    """The cancelled check must keep winning even for a row past the cap
    -- an operator who pressed cancel and then lost the converter must
    not see the job start over, and must not see it reported as a crash
    it never had either.
    """
    from orchestrator import reaper

    title = await _title_and_edition(db_conn, "cancelled-past-cap")
    job_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title[0], edition_id=title[1]
    )
    pool = _SingleConnPool(db_conn)

    for attempt in range(1, queries.JOB_MAX_ATTEMPTS):
        await queries.claim_job(db_conn, kind="transcode", worker_name="ml-server")
        await db_conn.execute(
            "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1",
            job_id,
        )
        await reaper.sweep_stale_jobs(pool)

    claimed = await queries.claim_job(
        db_conn, kind="transcode", worker_name="ml-server"
    )
    assert claimed["attempts"] == queries.JOB_MAX_ATTEMPTS
    await queries.request_job_cancel(db_conn, job_id)
    await db_conn.execute(
        "UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = $1", job_id
    )

    await reaper.sweep_stale_jobs(pool)

    job_row = await db_conn.fetchrow(
        "SELECT status, failure_reason FROM jobs WHERE id = $1", job_id
    )
    assert job_row["status"] == "cancelled"
    assert job_row["failure_reason"] is None


# --- Worker visibility --------------------------------------------------


async def test_a_check_in_is_recorded_even_when_the_claim_finds_nothing(db_conn):
    await queries.record_worker_seen(db_conn, worker_name="ml-server", status="idle")
    first_seen = await db_conn.fetchval(
        "SELECT last_seen_at FROM worker_checkins WHERE worker_name = $1", "ml-server"
    )

    await asyncio.sleep(0.01)
    await queries.record_worker_seen(db_conn, worker_name="ml-server", status="idle")
    second_seen = await db_conn.fetchval(
        "SELECT last_seen_at FROM worker_checkins WHERE worker_name = $1", "ml-server"
    )

    rows = await db_conn.fetch(
        "SELECT worker_name FROM worker_checkins WHERE worker_name = $1", "ml-server"
    )
    assert len(rows) == 1  # upsert, not a second row
    assert second_seen > first_seen


async def test_the_last_seen_worker_is_reported_with_its_name_time_and_status(db_conn):
    none_yet = await queries.last_worker_seen(db_conn)
    assert none_yet is None

    await queries.record_worker_seen(
        db_conn, worker_name="ml-server-old", status="idle"
    )
    await asyncio.sleep(0.01)
    await queries.record_worker_seen(
        db_conn, worker_name="ml-server", status="waiting_for_gpu"
    )

    latest = await queries.last_worker_seen(db_conn)
    assert latest["worker_name"] == "ml-server"
    assert latest["status"] == "waiting_for_gpu"
    assert latest["last_seen_at"] is not None


@pytest.mark.parametrize("status", ["idle", "waiting_for_gpu", "working"])
async def test_a_converter_below_the_memory_floor_checks_in_as_waiting_for_the_card(
    db_conn, status
):
    """Without this the only visible state is silence, the row says it is
    waiting for a converter, and the owner goes looking for a converter
    that is switched on and healthy.
    """
    await queries.record_worker_seen(db_conn, worker_name="ml-server", status=status)
    row = await db_conn.fetchrow(
        "SELECT status FROM worker_checkins WHERE worker_name = $1", "ml-server"
    )
    assert row["status"] == status

    await queries.record_worker_seen(db_conn, worker_name="ml-server", status="idle")
    replaced = await db_conn.fetchrow(
        "SELECT status FROM worker_checkins WHERE worker_name = $1", "ml-server"
    )
    assert replaced["status"] == "idle"

    with pytest.raises(asyncpg.CheckViolationError):
        await db_conn.execute(
            """
            INSERT INTO worker_checkins (worker_name, last_seen_at, status)
            VALUES ($1, now(), $2)
            ON CONFLICT (worker_name) DO UPDATE SET status = EXCLUDED.status
            """,
            "ml-server-bogus",
            "not-a-real-status",
        )


# --- Per-kind counting --------------------------------------------------


async def test_the_queue_position_is_counted_within_a_kind(db_conn):
    """A count that mixed kinds would tell an operator waiting on the card
    that a playlist assembly is ahead of them, which is not true of
    anything.
    """
    playlist_a = await _playlist(db_conn, "count-a")
    playlist_b = await _playlist(db_conn, "count-b")
    await queries.enqueue_job(db_conn, kind="playlist", playlist_id=playlist_a)
    await queries.enqueue_job(db_conn, kind="playlist", playlist_id=playlist_b)

    title_t = await _title_and_edition(db_conn, "count-transcode")
    transcode_id = await queries.enqueue_job(
        db_conn, kind="transcode", title_id=title_t[0], edition_id=title_t[1]
    )

    ahead = await queries.count_queued_ahead(db_conn, transcode_id)
    assert ahead == 0


# --- Queue order across a one-transaction batch (migration 0008) --------


async def test_eleven_jobs_enqueued_in_one_transaction_get_distinct_positions_through_library_rows(
    db_conn,
):
    """A folder save enqueues every episode's job inside one transaction,
    which used to tie every job's `created_at` to the same frozen instant
    -- "created earlier" then counted nothing, for any of them, and the
    position rendered on the screen read zero on every row. Asserted
    through `library_rows`, the query the screen actually reads:
    `count_queued_ahead` has no production caller at all, and a
    regression aimed only at it can pass while the screen still shows
    zeros. `db_conn`'s own wrapping transaction is what freezes the clock
    here -- the exact condition being regressed, not something extra
    arranged for the test.
    """
    title_ids = []
    for i in range(11):
        title_id, edition_id = await _title_and_edition(db_conn, f"batch-{i}")
        title_ids.append(title_id)
        await queries.enqueue_job(
            db_conn, kind="transcode", title_id=title_id, edition_id=edition_id
        )

    rows = await queries.library_rows(db_conn)
    positions = [
        row["job_queued_ahead"] for row in rows if row["title_id"] in title_ids
    ]

    assert len(positions) == 11
    assert sorted(positions) == list(range(11))


async def test_count_queued_ahead_also_resolves_ties_on_the_two_column_key(db_conn):
    """Companion to the row-level test above: nothing in production calls
    `count_queued_ahead`, but its own tie-breaking on `(created_at, id)`
    is exercised directly too, against the identical one-transaction tie.
    """
    job_ids = []
    for i in range(4):
        title_id, edition_id = await _title_and_edition(db_conn, f"count-tie-{i}")
        job_id = await queries.enqueue_job(
            db_conn, kind="transcode", title_id=title_id, edition_id=edition_id
        )
        job_ids.append(job_id)

    ahead = [await queries.count_queued_ahead(db_conn, job_id) for job_id in job_ids]
    assert sorted(ahead) == [0, 1, 2, 3]


async def test_claim_order_matches_the_promised_positions(db_conn):
    """The claim order is asserted behaviourally, never by matching source
    text: with tied rows seeded (the same one-transaction condition as the
    tests above), repeated `claim_job` calls return them in exactly the
    sequence the promised positions predicted. Claims until the queue is
    empty, on the same single connection that seeded the batch -- a second
    connection would open a second transaction and un-freeze the clock,
    making the tie this test needs unreproducible (see this module's own
    `worker_conns` fixture docstring for why the claiming tests need two
    connections for a different reason: neither reason applies here).
    """
    entries = []
    for i in range(5):
        title_id, edition_id = await _title_and_edition(db_conn, f"claim-order-{i}")
        job_id = await queries.enqueue_job(
            db_conn, kind="transcode", title_id=title_id, edition_id=edition_id
        )
        entries.append((title_id, job_id))

    ahead = {
        job_id: await queries.count_queued_ahead(db_conn, job_id)
        for _, job_id in entries
    }
    expected_order = [
        job_id for job_id, _ in sorted(ahead.items(), key=lambda kv: kv[1])
    ]

    claimed_order = []
    for _ in entries:
        claimed = await queries.claim_job(
            db_conn, kind="transcode", worker_name="order-checker"
        )
        assert claimed is not None
        claimed_order.append(claimed["id"])

    assert claimed_order == expected_order
