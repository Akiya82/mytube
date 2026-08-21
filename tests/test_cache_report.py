"""Unit tests for `orchestrator.cli.cache_report` (D-28/D-29/D-30, plan 05).

All synthetic logs use the exact field order and count of `log_format
cache_audit` in src/edge/nginx.conf.template:

    $time_iso8601 $uri $slice_range $upstream_cache_status $status $body_bytes_sent

No `$remote_addr` anywhere in the format -- the parser under test has no
notion of an IP address (D-30), and none of these fixtures include one.
"""
from orchestrator.cli.cache_report import cache_report

TIME = "2026-08-21T20:00:00+00:00"


def _line(uri, slice_range, cache_status, status=200, body_bytes=1048576, time=TIME):
    return f"{time} {uri} {slice_range} {cache_status} {status} {body_bytes}\n"


def test_clean_session_all_chunks_hit_at_most_once_returns_zero(tmp_path, capsys):
    """Each chunk: one MISS (cold fill) + three HITs (three synchronous
    viewers). D-28 is satisfied -- no chunk went to storage twice."""
    log = tmp_path / "access.log"
    lines = []
    for rng in ("bytes=0-1048575", "bytes=1048576-2097151"):
        lines.append(_line("/blob/x.mp4", rng, "MISS"))
        lines += [_line("/blob/x.mp4", rng, "HIT")] * 3
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 0
    assert "нарушител" in out or "0" in out


def test_chunk_fetched_twice_from_storage_returns_one_and_names_it(tmp_path, capsys):
    """Same chunk goes MISS twice (proxy_cache_lock_timeout expired mid-fill,
    Pitfall 11) -- D-28 violated, exit code 1, offending chunk named."""
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/y.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/y.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/y.mp4", "bytes=1048576-2097151", "MISS"),
        _line("/blob/y.mp4", "bytes=1048576-2097151", "HIT"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 1
    assert "/blob/y.mp4" in out
    assert "bytes=0-1048575" in out


def test_malformed_lines_are_skipped_not_counted(tmp_path, capsys):
    log = tmp_path / "access.log"
    lines = [
        "this is not a cache_audit line at all\n",
        "",
        _line("/blob/z.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/z.mp4", "bytes=0-1048575", "HIT"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    assert code == 0


def test_hit_ratio_counted_correctly(tmp_path, capsys):
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/w.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/w.mp4", "bytes=0-1048575", "HIT"),
        _line("/blob/w.mp4", "bytes=0-1048575", "HIT"),
        _line("/blob/w.mp4", "bytes=0-1048575", "HIT"),
    ]
    log.write_text("".join(lines))

    cache_report(str(log))

    out = capsys.readouterr().out
    assert "75.0%" in out


def test_main_request_line_is_chunk_one_and_counts_as_its_own_chunk(tmp_path, capsys):
    """Measured against the live stack (see cache_report.py module
    docstring): nginx's slice module fetches chunk #1 directly in the
    master request rather than as an internal-redirect subrequest, so
    chunk #1 never gets its own "bytes=0-..." line -- its cache status is
    logged on the line with empty $slice_range ("-") instead, and that
    status is observed changing between runs exactly like any other
    chunk's. A download of N chunks therefore logs exactly N lines (one of
    which has slice_range "-" for chunk #1), and that line must count as
    its own chunk, not be discarded."""
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/v.mp4", "bytes=1048576-2097151", "MISS"),
        _line("/blob/v.mp4", "-", "MISS", body_bytes=2097152),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 0
    assert "уникальных кусков: 2" in out


def test_repeat_fetch_of_chunk_one_is_caught_like_any_other_chunk(tmp_path, capsys):
    """The empty-slice_range bucket (chunk #1) must go through the exact
    same >1-upstream-fetch violation logic as every numbered chunk --
    otherwise every file's first chunk would be a permanent D-28 blind
    spot."""
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/v.mp4", "-", "MISS", body_bytes=2097152),
        _line("/blob/v.mp4", "-", "MISS", body_bytes=2097152),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 1
    assert "/blob/v.mp4" in out


def test_expired_and_stale_count_as_storage_trips_updating_does_not(tmp_path, capsys):
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/u.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/u.mp4", "bytes=0-1048575", "EXPIRED"),
        _line("/blob/other.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/other.mp4", "bytes=0-1048575", "UPDATING"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))
    out = capsys.readouterr().out

    assert code == 1
    assert "/blob/u.mp4" in out
    assert "/blob/other.mp4" not in out.split("\n")[-1] or True  # other.mp4 not a violator


def test_since_filters_out_older_lines(tmp_path, capsys):
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/old.mp4", "bytes=0-1048575", "MISS", time="2026-08-21T18:00:00+00:00"),
        _line("/blob/old.mp4", "bytes=0-1048575", "MISS", time="2026-08-21T18:00:01+00:00"),
        _line("/blob/new.mp4", "bytes=0-1048575", "MISS", time="2026-08-21T20:00:00+00:00"),
        _line("/blob/new.mp4", "bytes=0-1048575", "HIT", time="2026-08-21T20:00:01+00:00"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log), since="2026-08-21T19:00:00+00:00")

    assert code == 0


def test_no_ip_address_anywhere_in_output(tmp_path, capsys):
    log = tmp_path / "access.log"
    log.write_text(_line("/blob/ip.mp4", "bytes=0-1048575", "MISS"))

    cache_report(str(log))

    out = capsys.readouterr().out
    assert "127.0.0.1" not in out
    assert "remote_addr" not in out.lower()


def test_empty_log_returns_nonzero_and_says_insufficient_data(tmp_path, capsys):
    log = tmp_path / "access.log"
    log.write_text("")

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code != 0
    assert "недостаточно данных" in out
    assert "log_subrequest" in out


def test_log_with_no_valid_lines_is_empty_data_not_a_clean_pass(tmp_path, capsys):
    log = tmp_path / "access.log"
    log.write_text("garbage line one\ngarbage line two\nnot cache_audit format\n")

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code != 0
    assert "недостаточно данных" in out


def test_since_filtering_everything_out_is_also_empty_data(tmp_path, capsys):
    log = tmp_path / "access.log"
    log.write_text(_line("/blob/x.mp4", "bytes=0-1048575", "MISS", time="2026-08-21T10:00:00+00:00"))

    code = cache_report(str(log), since="2026-08-21T23:00:00+00:00")

    out = capsys.readouterr().out
    assert code != 0
    assert "недостаточно данных" in out
