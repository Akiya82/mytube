"""The progress reader and the cancellation path for
`mytube_media.runner` -- no ffmpeg anywhere. Where a real child process is
needed, spawns the running interpreter with an inline program rather than
any external binary, so the test is as portable as the suite it joins.

RED state: `mytube_media` does not exist at all yet -- every subject below
is imported inside its own test body (mirrors
`tests/test_playlist_build.py` and `tests/test_resolve_playlist.py`), so
the module still collects cleanly and each test fails as its own
`ModuleNotFoundError` rather than one collection error hiding the whole
file.
"""

import asyncio
import sys

import pytest

TIMEOUT_SECONDS = 10.0


def _progress_lines(*, out_time_us=5946667, speed="0.995x", final=False):
    """One block of ffmpeg's own documented `-progress pipe:1` output --
    a fixed key set repeated every progress update, `progress=end` on the
    final line of the stream signalling completion.
    """
    return [
        "frame=142",
        "fps=23.87",
        "bitrate=5987.3kbits/s",
        "total_size=18874368",
        f"out_time_us={out_time_us}",
        f"out_time_ms={out_time_us}",
        "out_time=00:00:05.946667",
        "dup_frames=0",
        "drop_frames=0",
        f"speed={speed}",
        f"progress={'end' if final else 'continue'}",
    ]


# --- The progress parser --------------------------------------------


async def test_a_progress_block_is_emitted_only_when_the_block_ends():
    from mytube_media.runner import ProgressParser

    parser = ProgressParser()
    lines = _progress_lines()
    results = [parser.feed(line) for line in lines[:-1]]
    assert all(result is None for result in results)

    final = parser.feed(lines[-1])
    assert final is not None


async def test_the_elapsed_time_is_read_from_the_microsecond_key():
    from mytube_media.runner import ProgressParser

    parser = ProgressParser()
    progress = None
    for line in _progress_lines(out_time_us=5946667):
        progress = parser.feed(line)

    assert progress is not None
    assert progress.out_time_seconds == pytest.approx(5.946667, abs=0.001)


async def test_the_final_block_is_marked_done():
    from mytube_media.runner import ProgressParser

    parser = ProgressParser()
    progress = None
    for line in _progress_lines(final=True):
        progress = parser.feed(line)

    assert progress is not None
    assert progress.done is True


async def test_the_realtime_factor_is_read_from_the_speed_key():
    from mytube_media.runner import ProgressParser

    parser = ProgressParser()
    progress = None
    for line in _progress_lines(speed="1.42x"):
        progress = parser.feed(line)

    assert progress is not None
    assert progress.speed == pytest.approx(1.42, abs=0.001)

    parser_malformed = ProgressParser()
    malformed = None
    for line in _progress_lines(speed="N/A"):
        malformed = parser_malformed.feed(line)

    assert malformed is not None
    assert malformed.speed is None


@pytest.mark.parametrize(
    "garbled_line", ["not-a-key-value-line", "", "totally_unknown_key=5"]
)
async def test_a_partial_or_garbled_line_is_ignored(garbled_line):
    from mytube_media.runner import ProgressParser

    parser = ProgressParser()
    result = parser.feed(garbled_line)
    assert result is None


async def test_percent_needs_a_known_duration():
    """With `duration_seconds` of `None` the callback still receives
    elapsed time and the percent it computes is `None`, never a division
    by zero and never a fabricated hundred.
    """
    from mytube_media.runner import run_ffmpeg_with_progress

    printed = "\n".join(f"print({line!r})" for line in _progress_lines(final=True))
    argv = [sys.executable, "-c", printed]

    calls = []

    async def _on_progress(progress, percent):
        calls.append((progress, percent))

    await asyncio.wait_for(
        run_ffmpeg_with_progress(
            argv,
            duration_seconds=None,
            on_progress=_on_progress,
            should_cancel=lambda: False,
            timeout=TIMEOUT_SECONDS,
        ),
        timeout=TIMEOUT_SECONDS,
    )

    assert calls
    last_progress, last_percent = calls[-1]
    assert last_progress.out_time_seconds is not None
    assert last_percent is None


# --- Concurrent pipe draining -----------------------------------------


async def test_a_process_that_writes_heavily_to_both_pipes_still_completes():
    """A reader that drains one pipe and leaves the other as an unread
    pipe deadlocks once the operating system's buffer fills, and this
    codebase has never had a live-progress read before, so nothing else
    would catch it.
    """
    from mytube_media.runner import run_ffmpeg_with_progress

    printed = "\n".join(f"print({line!r})" for line in _progress_lines(final=True))
    program = (
        "import sys\n"
        "for _ in range(20000):\n"
        "    print('x' * 40, file=sys.stderr)\n"
        f"{printed}\n"
    )
    argv = [sys.executable, "-c", program]

    calls = []

    async def _on_progress(progress, percent):
        calls.append(progress)

    await asyncio.wait_for(
        run_ffmpeg_with_progress(
            argv,
            duration_seconds=10.0,
            on_progress=_on_progress,
            should_cancel=lambda: False,
            timeout=TIMEOUT_SECONDS,
        ),
        timeout=TIMEOUT_SECONDS,
    )

    assert calls


# --- Cancellation --------------------------------------------------------


async def test_a_cancelled_run_is_reported_as_cancelled_rather_than_failed():
    """A cancellation is an operator's decision, not a fault, and the two
    must not share a code path.
    """
    from mytube_media.runner import FfmpegFailed, run_ffmpeg_with_progress

    program = "import time\ntime.sleep(30)\n"
    argv = [sys.executable, "-c", program]

    try:
        result = await asyncio.wait_for(
            run_ffmpeg_with_progress(
                argv,
                duration_seconds=None,
                on_progress=lambda *a: None,
                should_cancel=lambda: True,
                timeout=TIMEOUT_SECONDS,
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except FfmpegFailed:
        pytest.fail("a cancellation must not be reported as FfmpegFailed")

    assert result.cancelled is True


async def test_a_child_that_ignores_the_polite_signal_is_killed_after_the_grace_period():
    """Assert the process is actually reaped, not merely signalled: an
    ffmpeg left alive holds the card's encoder session open for whatever
    claims the next job.
    """
    from mytube_media.runner import TERMINATE_GRACE_SECONDS, run_ffmpeg_with_progress

    # SIG_IGN on SIGTERM (POSIX) -- terminate() alone must not be enough to
    # end this child, so only the follow-up kill() after the grace period
    # can.
    program = (
        "import signal, time\n"
        "try:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "except Exception:\n"
        "    pass\n"
        "time.sleep(60)\n"
    )
    argv = [sys.executable, "-c", program]

    result = await asyncio.wait_for(
        run_ffmpeg_with_progress(
            argv,
            duration_seconds=None,
            on_progress=lambda *a: None,
            should_cancel=lambda: True,
            timeout=TIMEOUT_SECONDS,
        ),
        timeout=TERMINATE_GRACE_SECONDS + TIMEOUT_SECONDS,
    )

    assert result.cancelled is True


async def test_a_failing_process_raises_a_typed_error_carrying_only_a_log_tail():
    """The mapping from an exception to a reason the operator reads
    happens above this module, as it already does for the remux path.
    """
    from mytube_media.runner import FfmpegFailed, run_ffmpeg_with_progress

    program = "import sys\nsys.stderr.write('boom\\n' * 500)\nsys.exit(1)\n"
    argv = [sys.executable, "-c", program]

    with pytest.raises(FfmpegFailed) as exc_info:
        await asyncio.wait_for(
            run_ffmpeg_with_progress(
                argv,
                duration_seconds=None,
                on_progress=lambda *a: None,
                should_cancel=lambda: False,
                timeout=TIMEOUT_SECONDS,
            ),
            timeout=TIMEOUT_SECONDS,
        )

    err = exc_info.value
    assert len(err.stderr_tail) < 500 * 5
    assert not hasattr(err, "reason_key")
