"""The card's free memory, read from the vendor's own query tool.

No card library, no bindings -- one small subprocess call and a parse, in
the same "run it, parse the output, wrap failures in a typed error"
discipline `mytube_media.compat.probe` already uses. Failure to run the
tool at all (`GpuQueryError`) is a distinct outcome from the tool
reporting a genuinely low number: one means the converter is
misconfigured or the card is missing, the other means wait a moment --
`worker.__main__`'s loop treats them differently.

The query tool prints one line per card, not one line total: a host with
two cards produces a multi-line reading, and taking the first line is
enough for this converter, which only ever runs one encode on one device.
Parsing only the first line is not the same fallback a missing card gets
-- a missing card still raises `GpuQueryError` from the tool failing to
run at all, and only a reading the tool *did* produce gets this narrower
first-line reading.
"""
import subprocess

_QUERY_TIMEOUT_SECONDS = 10
_BYTES_PER_MIB = 1024 * 1024


class GpuQueryError(Exception):
    """`nvidia-smi` could not be run at all, or its output could not be
    parsed -- a misconfiguration or a missing card, not "the card is
    busy". Carries a log-only message, never shown to an operator.
    """


def free_vram_bytes() -> int:
    """The card's free memory in bytes, as `nvidia-smi` reports it right
    now. Synchronous and blocking on purpose -- `worker.__main__` runs it
    through `asyncio.to_thread` like every other blocking call in that
    loop, the same discipline `orchestrator.media.queue` already keeps
    for its own blocking calls.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise GpuQueryError(f"nvidia-smi could not be run: {exc}") from exc

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise GpuQueryError(f"nvidia-smi produced no output: {result.stdout!r}")
    try:
        return int(lines[0]) * _BYTES_PER_MIB
    except ValueError as exc:
        raise GpuQueryError(
            f"nvidia-smi produced unparsable output: {result.stdout!r}"
        ) from exc
