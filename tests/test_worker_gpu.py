"""`worker.gpu.free_vram_bytes` against a stubbed `nvidia-smi` -- no real
card, no real subprocess. What is under test is the parse: the vendor's
own query tool prints one line per card, and a host with two cards must
not be misread as a host whose reading could not be parsed at all.
"""
import subprocess

from worker import gpu


class _FakeCompletedProcess:
    def __init__(self, stdout: str):
        self.stdout = stdout


def test_a_two_card_reading_returns_the_first_card_s_free_memory(monkeypatch):
    """The vendor tool prints one line per card -- taking the first line
    is what lets the free-memory floor keep working on a two-card host
    instead of degrading to the "no card at all" fallback on every poll.
    """

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess("8000\n4000\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gpu.free_vram_bytes() == 8000 * gpu._BYTES_PER_MIB


def test_a_single_card_reading_still_works(monkeypatch):
    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess("5000\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gpu.free_vram_bytes() == 5000 * gpu._BYTES_PER_MIB


def test_a_reading_that_cannot_be_parsed_raises_rather_than_claiming_blindly(monkeypatch):
    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess("not a number\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    import pytest

    with pytest.raises(gpu.GpuQueryError):
        gpu.free_vram_bytes()


def test_the_tool_failing_to_run_at_all_still_raises(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)

    import pytest

    with pytest.raises(gpu.GpuQueryError):
        gpu.free_vram_bytes()
