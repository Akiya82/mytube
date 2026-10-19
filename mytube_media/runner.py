"""Running a conversion while reading its progress, and stopping it
without leaving a process holding the card.
Stdlib only, matching the rest of this package's rule.

Every earlier ffmpeg invocation in this project (`remux.run_ffmpeg`,
`compat.probe`) was blocking and read both output streams internally via
`subprocess.run(capture_output=True)`. This module is the first one that
has to read progress *while* a long process runs, and that changes the
rules: both output streams have to be drained concurrently, always. A
reader that drains one pipe and leaves the other unread stops the moment
the operating system's own pipe buffer fills -- which a long conversion
with a chatty subtitle renderer reliably reaches -- and the symptom is a
job that hangs at some percent with no error anywhere, because nothing
ever raises. `asyncio.gather` over both readers plus the cancellation
watcher is what this module uses instead of a hand-written single-stream
read loop.

This is also the first process this project has to stop mid-run. A
conversion that is cancelled has to actually end -- terminate, then kill
after a grace period, then wait for the exit to be reaped -- because a
process left alive holds the card's NVENC encoder session open for
whatever claims the next job.
"""

import asyncio
from dataclasses import dataclass

# How long a polite `terminate()` gets before `kill()` follows. Named
# once and reused by `_terminate_then_kill` (the sequence this module
# exposes for its own cancellation path) and by whatever hosts this
# runner and has to perform the identical sequence on its own shutdown --
# two implementations of "terminate, then kill" would eventually differ.
TERMINATE_GRACE_SECONDS = 5.0

# How often the cancellation predicate is polled while the process runs,
# so a process that has gone quiet on both pipes is still stoppable
# promptly rather than only at the next progress block.
_CANCEL_POLL_INTERVAL_SECONDS = 0.5

# How many trailing stderr lines `FfmpegFailed` carries -- enough for a
# log line, never the whole stream (a chatty failure must not
# turn a bounded log tail into an unbounded one).
_STDERR_TAIL_LINES = 20


class FfmpegFailed(Exception):
    """The process exited non-zero. Carries only a bounded tail of
    stderr -- no catalog key here, mapping this to a sentence an operator
    reads happens above this module, exactly as it already does for the
    copy-only path (`remux.RemuxError`).
    """

    def __init__(self, stderr_tail: str):
        self.stderr_tail = stderr_tail
        super().__init__(stderr_tail)


@dataclass
class Progress:
    """One parsed `-progress` block. `out_time_seconds`/`speed` are
    `None` when the corresponding key was absent or malformed -- never a
    fabricated zero. `done` is `True` only for the block whose own
    `progress` value was `end`.
    """

    out_time_seconds: float | None
    speed: float | None
    done: bool


@dataclass
class RunResult:
    """What `run_ffmpeg_with_progress` returns on any non-raising exit.
    `cancelled` is `True` only when the caller's own `should_cancel`
    predicate asked for a stop -- a cancellation is an operator's
    decision, not a fault, and the two must never share a code path (a
    cancelled run never raises `FfmpegFailed`, whatever the process's own
    exit code happened to be once killed).
    """

    cancelled: bool
    returncode: int | None = None


class ProgressParser:
    """Accumulates ffmpeg's own `-progress pipe:1` key=value lines and
    emits one `Progress` only when a block closes (the `progress=`
    line -- `continue` for every block but the last, `end` for the
    final one), never before. Percent is not this class's business: it
    needs a duration the caller has and this class does not.
    """

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}

    def feed(self, line: str) -> Progress | None:
        line = line.strip()
        if not line or "=" not in line:
            return None
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()

        if key != "progress":
            self._pending[key] = value
            return None

        out_time_us = self._pending.get("out_time_us")
        out_time_seconds = None
        if out_time_us is not None:
            try:
                out_time_seconds = int(out_time_us) / 1_000_000
            except ValueError:
                out_time_seconds = None

        speed_raw = self._pending.get("speed")
        speed = None
        if speed_raw is not None:
            try:
                speed = float(speed_raw.rstrip("x"))
            except ValueError:
                speed = None

        progress = Progress(
            out_time_seconds=out_time_seconds, speed=speed, done=value == "end"
        )
        self._pending = {}
        return progress


async def _terminate_then_kill(process: "asyncio.subprocess.Process") -> None:
    """Ask politely first (`terminate()`), and only insist (`kill()`)
    once `TERMINATE_GRACE_SECONDS` has passed with the process still
    alive. Waits for the process to actually be reaped before returning
    either way -- a process left alive holds the card's encoder session
    open for whatever claims the next job, so "signalled" is not good
    enough here, only "reaped" is. Exposed as its own function because
    the process hosting this runner performs the identical sequence on
    its own shutdown, and two implementations of it would eventually
    differ.
    """
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass

    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def run_ffmpeg_with_progress(
    argv: list[str],
    *,
    duration_seconds: float | None,
    on_progress,
    should_cancel,
    timeout: float,
) -> RunResult:
    """Start `argv` with no shell, both output streams as pipes, and read
    them concurrently: one reader parsing progress, one reader keeping a
    bounded tail of stderr and nothing more. Never a single-stream read
    loop -- see the module docstring for why.

    `on_progress(progress, percent)` is awaited on every emitted block;
    `percent` is `None` whenever `duration_seconds` is `None` or the
    block carried no elapsed time, never a division by zero and never a
    fabricated hundred. `should_cancel()` is a plain synchronous
    predicate, polled between blocks and on `_CANCEL_POLL_INTERVAL_SECONDS`'s
    own timer so a process that has gone quiet on both pipes is still
    stoppable. On cancellation the run ends via `_terminate_then_kill`
    and returns with `cancelled=True` rather than raising. The whole run
    is bounded by `timeout`, generous enough for a long film and finite,
    so a genuinely wedged process cannot occupy the converter forever.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    parser = ProgressParser()
    stderr_tail: list[str] = []
    cancelled = False

    async def read_stdout() -> None:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                return
            progress = parser.feed(raw.decode("utf-8", errors="replace"))
            if progress is None:
                continue
            percent = None
            if duration_seconds and progress.out_time_seconds is not None:
                percent = min(
                    100.0, max(0.0, progress.out_time_seconds / duration_seconds * 100)
                )
            await on_progress(progress, percent)

    async def read_stderr() -> None:
        assert process.stderr is not None
        while True:
            raw = await process.stderr.readline()
            if not raw:
                return
            stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
            del stderr_tail[:-_STDERR_TAIL_LINES]

    async def watch_cancel() -> None:
        nonlocal cancelled
        while process.returncode is None:
            if should_cancel():
                cancelled = True
                await _terminate_then_kill(process)
                return
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), watch_cancel()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # A genuinely wedged process: the converter must not occupy the
        # slot forever waiting on it.
        await _terminate_then_kill(process)
        raise FfmpegFailed("\n".join(stderr_tail)) from None

    returncode = await process.wait()

    if cancelled:
        return RunResult(cancelled=True, returncode=returncode)
    if returncode != 0:
        raise FfmpegFailed("\n".join(stderr_tail))
    return RunResult(cancelled=False, returncode=returncode)
