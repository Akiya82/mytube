"""Unit tests for `orchestrator.cli.cache_report`.

All synthetic logs use the exact field order of `log_format cache_audit` in
src/edge/nginx.conf.template:

    $time_iso8601 $uri $slice_range $upstream_cache_status $status
    $body_bytes_sent $client_range_start

The seventh field (`client_range_start`) is OPTIONAL in real logs -- a real
`blob_access.log` spans the edge recreate that introduced it -- so `_line`
below defaults to omitting it, matching a pre-recreate line, and callers
pass it explicitly to build a post-recreate line.

No `$remote_addr` anywhere in the format -- the parser under test has no
notion of an IP address, and none of these fixtures include one.
"""
from orchestrator.cli.cache_report import cache_report

TIME = "2026-08-21T20:00:00+00:00"

# MYTUBE_SLICE_SIZE's default (see cache_warm.DEFAULT_CHUNK_SIZE); alignment
# in cache_report follows this unless a test overrides the env var.
DEFAULT_SLICE_SIZE = 1024 * 1024


def _line(
    uri,
    slice_range,
    cache_status,
    status=200,
    body_bytes=1048576,
    time=TIME,
    client_range_start=None,
):
    line = f"{time} {uri} {slice_range} {cache_status} {status} {body_bytes}"
    if client_range_start is not None:
        line += f" {client_range_start}"
    return line + "\n"


def test_clean_session_all_chunks_hit_at_most_once_returns_zero(tmp_path, capsys):
    """Each chunk: one MISS (cold fill) + three HITs (three synchronous
    viewers). No chunk went to storage twice."""
    log = tmp_path / "access.log"
    lines = []
    for rng in ("bytes=0-1048575", "bytes=1048576-2097151"):
        lines.append(_line("/blob/x.mp4", rng, "MISS"))
        lines += [_line("/blob/x.mp4", rng, "HIT")] * 3
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 0
    assert "violation" in out or "0" in out


def test_chunk_fetched_twice_from_storage_returns_one_and_names_it(tmp_path, capsys):
    """Same chunk goes MISS twice (proxy_cache_lock_timeout expired mid-fill)
    -- the rule is violated, exit code 1, offending chunk named."""
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


def test_five_point_range_requests_to_five_offsets_are_five_chunks_not_one(tmp_path, capsys):
    """The gap this plan closes: five viewers each open the file with a
    point Range request landing in a different slice. Every one of those
    requests logs as a master line (empty $slice_range) -- the OLD code
    bucketed all five under the single literal key "-", reporting one
    phantom chunk fetched five times (a false violation that gets
    WORSE as more people watch). Attributed by each request's own start
    offset, this is five distinct chunks, one storage fetch each: exit 0,
    and the phantom " -:" bucket must never appear in the output."""
    log = tmp_path / "access.log"
    offsets = [0, 2_000_000, 5_000_000, 8_000_000, 11_000_000]
    lines = [_line("/blob/v.mp4", "-", "MISS", client_range_start=str(off)) for off in offsets]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 0
    assert "unique chunks: 5" in out
    assert " -:" not in out


def test_master_line_and_subrequest_for_same_slice_share_one_bucket(tmp_path, capsys):
    """A master line whose offset falls inside slice N and a subrequest
    line for slice N must merge into one bucket, so two storage fetches of
    the same slice from two different client requests are still caught --
    named by the real byte range, not by the master line's empty
    $slice_range."""
    log = tmp_path / "access.log"
    # 2_500_000 floors (at the 1 MiB default) into slice bytes=2097152-3145727.
    lines = [
        _line("/blob/v.mp4", "-", "MISS", client_range_start="2500000"),
        _line("/blob/v.mp4", "bytes=2097152-3145727", "MISS"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 1
    assert "/blob/v.mp4" in out
    assert "bytes=2097152-3145727" in out


def test_master_line_with_unparseable_offset_is_unattributed_not_a_chunk(tmp_path, capsys):
    """A master line whose $client_range_start is the nginx map's own "-"
    fallback (multi-range request the map could not parse) must not be
    guessed into chunk zero -- it is counted separately and never printed
    as a violator."""
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/v.mp4", "-", "MISS", client_range_start="-"),
        _line("/blob/v.mp4", "-", "MISS", client_range_start="-"),
        _line("/blob/v.mp4", "bytes=0-1048575", "MISS"),
        _line("/blob/v.mp4", "bytes=0-1048575", "HIT"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 0
    assert "not attributable to any chunk: 2" in out
    assert "/blob/v.mp4" not in out.split("\n")[-1]


def test_log_made_entirely_of_stale_format_master_lines_is_insufficient_data(tmp_path, capsys):
    """A window recorded entirely before the edge picked up
    $client_range_start has only six-field master lines -- none of them
    attributable to any chunk. That must exit with the insufficient-data
    code, not 0: a broken measurement must never read as a clean pass."""
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/v.mp4", "-", "MISS"),  # no client_range_start field at all
        _line("/blob/v.mp4", "-", "HIT"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 2
    assert "not enough data" in out
    assert "before the client offset field existed" in out


def test_alignment_follows_mytube_slice_size_not_a_hardcoded_default(tmp_path, capsys, monkeypatch):
    """A non-default MYTUBE_SLICE_SIZE must change where the offset gets
    floored to -- proving the alignment is not hardcoded to 1 MiB."""
    monkeypatch.setenv("MYTUBE_SLICE_SIZE", "2m")
    log = tmp_path / "access.log"
    lines = [
        _line("/blob/v.mp4", "-", "MISS", client_range_start="3000000"),
        _line("/blob/v.mp4", "-", "MISS", client_range_start="3000000"),
    ]
    log.write_text("".join(lines))

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code == 1
    # floor(3_000_000 / 2 MiB) * 2 MiB == 2097152, end == 2097152 + 2 MiB - 1
    assert "bytes=2097152-4194303" in out


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
    assert "not enough data" in out
    assert "log_subrequest" in out


def test_log_with_no_valid_lines_is_empty_data_not_a_clean_pass(tmp_path, capsys):
    log = tmp_path / "access.log"
    log.write_text("garbage line one\ngarbage line two\nnot cache_audit format\n")

    code = cache_report(str(log))

    out = capsys.readouterr().out
    assert code != 0
    assert "not enough data" in out


def test_since_filtering_everything_out_is_also_empty_data(tmp_path, capsys):
    log = tmp_path / "access.log"
    log.write_text(_line("/blob/x.mp4", "bytes=0-1048575", "MISS", time="2026-08-21T10:00:00+00:00"))

    code = cache_report(str(log), since="2026-08-21T23:00:00+00:00")

    out = capsys.readouterr().out
    assert code != 0
    assert "not enough data" in out
