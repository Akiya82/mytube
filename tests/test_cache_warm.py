"""Unit tests for `orchestrator.cli.cache_warm` -- no edge, no network:
`httpx.Client` is replaced with a stand-in that answers a scripted sequence
of Range requests. The two cases that matter are the ones the loop
condition used to get wrong.
"""

import httpx
import pytest

from orchestrator.cli import cache_warm as cw

CHUNK = 1024


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes, headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class _FakeClient:
    """Answers `/v/{slug}` with a 302 and then serves `responses` in order,
    raising once the script is exhausted -- a runaway loop fails loudly
    instead of hanging the test run forever, which is what the bug under
    test would otherwise do.
    """

    script: list = []
    requested: list = []

    def __init__(self, *args, **kwargs):
        self._remaining = list(self.script)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, headers=None):
        if url.endswith("/v/warm-me"):
            return _FakeResponse(302, b"", {"location": "http://edge/blob/x.mp4"})
        type(self).requested.append(headers["Range"])
        if not self._remaining:
            raise AssertionError(
                f"cache_warm made more range requests than the script allows "
                f"({len(type(self).requested)}) -- it is not terminating"
            )
        return self._remaining.pop(0)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.script = []
    _FakeClient.requested = []
    monkeypatch.setattr(cw.httpx, "Client", _FakeClient)
    return _FakeClient


def test_an_origin_that_ignores_range_stops_instead_of_looping_forever(
    fake_http, capsys
):
    """A misconfigured edge answers 200 with the whole body and no
    Content-Range. The total size then stays unknown -- which used to keep
    the loop condition permanently true and the byte limit never consulted,
    so the command re-downloaded the file until someone killed it.
    """
    fake_http.script = [_FakeResponse(200, b"x" * CHUNK) for _ in range(40)]

    code = cw.cache_warm(
        "warm-me", base_url="http://edge", chunk_size=CHUNK, warm_bytes=4 * CHUNK
    )

    assert code == 1
    assert len(fake_http.requested) == 1
    assert "Range" in capsys.readouterr().err


def test_the_byte_limit_is_honoured_when_the_origin_serves_ranges(fake_http, capsys):
    total = 100 * CHUNK
    fake_http.script = [
        _FakeResponse(
            206,
            b"x" * CHUNK,
            {"content-range": f"bytes 0-{CHUNK - 1}/{total}"},
        )
        for _ in range(40)
    ]

    code = cw.cache_warm(
        "warm-me", base_url="http://edge", chunk_size=CHUNK, warm_bytes=3 * CHUNK
    )

    assert code == 0
    assert len(fake_http.requested) == 3
    assert f"bytes warmed: {3 * CHUNK}" in capsys.readouterr().out


def test_a_file_shorter_than_the_limit_ends_on_the_short_read(fake_http, capsys):
    total = CHUNK + 10
    fake_http.script = [
        _FakeResponse(
            206, b"x" * CHUNK, {"content-range": f"bytes 0-{CHUNK - 1}/{total}"}
        ),
        _FakeResponse(
            206, b"x" * 10, {"content-range": f"bytes {CHUNK}-{total - 1}/{total}"}
        ),
    ]

    code = cw.cache_warm(
        "warm-me", base_url="http://edge", chunk_size=CHUNK, warm_bytes=50 * CHUNK
    )

    assert code == 0
    assert len(fake_http.requested) == 2
    assert f"bytes warmed: {total}" in capsys.readouterr().out


def test_a_slug_that_does_not_resolve_exits_one(fake_http, monkeypatch, capsys):
    class _NoRedirectClient(_FakeClient):
        def get(self, url, headers=None):
            return _FakeResponse(404, b"")

    monkeypatch.setattr(cw.httpx, "Client", _NoRedirectClient)

    assert cw.cache_warm("warm-me", base_url="http://edge") == 1
    assert "404" in capsys.readouterr().err


def test_the_real_httpx_client_is_what_the_module_uses():
    # Guards the monkeypatch target above: if this module ever stops
    # building its client through `httpx.Client`, every test in this file
    # silently stops exercising the real code path.
    assert cw.httpx is httpx
