"""cache-report: turn the D-28 cache criterion into a command with an exit
code (D-29), instead of a phrase someone eyeballs at 3am after movie night.

The regex below is synchronized field-for-field, in the same order, with
the `log_format cache_audit` directive in src/edge/nginx.conf.template:

    log_format cache_audit '$time_iso8601 $uri $slice_range '
                            '$upstream_cache_status $status $body_bytes_sent '
                            '$client_range_start';

If that directive's field list or order ever changes, this regex must
change with it -- a silent mismatch does not error, it just parses zero
lines, which is exactly the failure mode the zero-parsed-lines guard below
exists to catch instead of quietly reporting "no violations found". The
seventh field is matched as OPTIONAL: a real `blob_access.log` spans the
edge recreate that introduced it, so the same file legitimately contains
both six-field lines (recorded before) and seven-field lines (recorded
after), and a mandatory group would make every pre-recreate line
unparseable and quietly shrink the measured window instead of just leaving
those lines unattributed.

No viewer IP anywhere in the format or in this module (D-30): the report's
output is meant to be safe to paste into an issue on the public repo.

**What "empty slice_range" actually means, and what changed here.** Live
measurement against this exact stack (two full downloads of the same
title, cold then warm) established that nginx's slice module serves the
first slice a client asks for directly inside the master request, not as
an internal-redirect subrequest -- so a master-request line never gets its
own `bytes=N-...` line, and a download of N chunks therefore produces
exactly N log lines, not N+1 (matching the 64-vs-65 discrepancy already
recorded in src/docs/experiments/07-object-storage.md; that observation is
still correct). What was wrong was the inference drawn from it: an earlier
version of this module bucketed every master-request line under the
literal `-`, on the assumption that "the first slice a client asks for" is
always slice #1. It is not -- it is whichever slice the client's own
request started in, and a client that opens with a mid-file Range request
starts there, not at byte 0. Bucketing every such line under one literal
key meant every viewer's opening request collided in the same phantom
bucket regardless of where each one actually seeked to, so the reported
chunk count grew with the number of viewers instead of measuring anything
about storage.

The fix: `$client_range_start` (added to `log_format cache_audit` in the
same change as this module) records the offset the client's request
itself began at. A master-request line is now attributed by flooring that
offset to a `MYTUBE_SLICE_SIZE` boundary and building the same key shape
nginx writes into `$slice_range` for a subrequest -- so a master line and
a subrequest line for the same slice merge into one bucket, and a genuine
double fetch of that slice is still caught, named by its real byte range.
A line whose offset cannot be trusted -- absent (it predates this change)
or not a plain digit string (the nginx map's own "unknown" fallback, e.g.
a multi-range request) -- is counted as unattributed and never guessed
into chunk zero; see `_align_master_line_range` and the unattributed-count
reporting below.
"""
import os
import re
from collections import Counter
from datetime import datetime

from orchestrator.cli.cache_warm import _parse_size

LINE_RE = re.compile(
    r"(?P<time>\S+) (?P<uri>\S+) (?P<slice_range>\S+) "
    r"(?P<cache_status>\S+) (?P<status>\d+) (?P<bytes>\d+)"
    r"(?: (?P<client_range_start>\S+))?"
)

# Statuses that mean nginx went to storage for this chunk (D-28). HIT and
# UPDATING mean it did not -- UPDATING is served stale while a background
# refresh happens (proxy_cache_use_stale updating, D-26), not a new fetch
# on this request.
UPSTREAM_STATUSES = {"MISS", "EXPIRED", "STALE"}

INSUFFICIENT_DATA_EXIT_CODE = 2
VIOLATION_EXIT_CODE = 1
OK_EXIT_CODE = 0


def _parse_since(value: str) -> datetime:
    # Accepts the same $time_iso8601 shape nginx logs (e.g.
    # "2026-08-21T20:00:00+00:00"), plus a bare "Z" suffix for convenience.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _align_master_line_range(offset_str: str | None, slice_size: int) -> str | None:
    """Resolve the slice a master-request line (empty `$slice_range`)
    actually fetched, from the client's own start offset.

    Returns the aligned range in the exact shape nginx writes into
    `$slice_range` for a subrequest ("bytes=<start>-<end>"), or None when
    the offset cannot be trusted: absent (the log line predates
    `$client_range_start`) or not a plain digit string (the nginx map's
    own "-" fallback for a header it could not parse, e.g. a multi-range
    request). Callers must count a None result as unattributed, never as
    chunk zero.
    """
    if offset_str is None or not offset_str.isdigit():
        return None
    offset = int(offset_str)
    aligned = (offset // slice_size) * slice_size
    return f"bytes={aligned}-{aligned + slice_size - 1}"


def cache_report(log_path: str, since: str | None = None) -> int:
    """Parse `log_path` (cache_audit format), report on storage re-fetches.

    Returns the process exit code: 0 if no chunk was fetched from storage
    more than once, 1 if D-28 was violated, and 2 (verdict B2) if there was
    not enough data to say either way. Insufficient data covers three
    cases, all reported the same way rather than as a false "0 violations":
    zero parsed lines (empty log, no line matched the format, or `--since`
    filtered everything out); lines parsed but none of them attributable to
    any chunk (a log window recorded entirely before the edge picked up
    `$client_range_start`, or entirely multi-range requests); and -- as
    before Task 1 of this change -- a misconfigured edge (missing
    `log_subrequest on;`) or a format drift between this regex and
    `cache_audit`.
    """
    read_count = 0
    since_dt = _parse_since(since) if since else None
    slice_size = _parse_size(os.environ.get("MYTUBE_SLICE_SIZE", "1m"))

    per_slice: Counter[tuple[str, str]] = Counter()
    upstream_hits: Counter[tuple[str, str]] = Counter()
    parsed_count = 0
    unattributed_count = 0

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            read_count += 1
            m = LINE_RE.match(line)
            if not m:
                continue

            if since_dt is not None:
                try:
                    line_time = datetime.fromisoformat(m["time"])
                except ValueError:
                    continue
                if line_time < since_dt:
                    continue

            parsed_count += 1

            if m["slice_range"] != "-":
                # Subrequest line: nginx already names the exact slice it
                # fetched, nothing to attribute.
                key = (m["uri"], m["slice_range"])
            else:
                aligned = _align_master_line_range(m["client_range_start"], slice_size)
                if aligned is None:
                    unattributed_count += 1
                    continue
                key = (m["uri"], aligned)

            per_slice[key] += 1
            if m["cache_status"] in UPSTREAM_STATUSES:
                upstream_hits[key] += 1

    if parsed_count == 0:
        print(
            "not enough data: read "
            f"{read_count} line(s), 0 of them parsed -- "
            "there is nothing to measure, which does not mean there are no violations"
        )
        print(
            "likely cause: src/edge/nginx.conf.template's "
            "location ~ ^/blob/ does not have `log_subrequest on;` enabled, "
            "or access_log's format has drifted from log_format cache_audit"
        )
        return INSUFFICIENT_DATA_EXIT_CODE

    if not per_slice:
        print(
            "not enough data: read "
            f"{read_count} line(s), parsed {parsed_count}, but none of them "
            "attributable to any chunk -- there is nothing to measure, "
            "which does not mean there are no violations"
        )
        print(
            "likely cause: the whole log window was recorded before the "
            "client offset field existed (edge has not been recreated "
            "yet), or it is entirely multi-range requests"
        )
        return INSUFFICIENT_DATA_EXIT_CODE

    total_slices = len(per_slice)
    total_requests = sum(per_slice.values())
    total_upstream = sum(upstream_hits.values())
    hit_ratio = 1 - (total_upstream / total_requests) if total_requests else 0.0
    worst = sorted(
        (k for k, v in upstream_hits.items() if v > 1),
        key=lambda k: upstream_hits[k],
        reverse=True,
    )

    print(f"lines read: {read_count}, parsed: {parsed_count}")
    print(f"unique chunks: {total_slices}, HIT ratio: {hit_ratio:.1%}")
    print(f"chunks with more than one request to storage: {len(worst)}")
    for uri, rng in worst[:10]:
        print(f"  {uri} {rng}: {upstream_hits[(uri, rng)]} requests to storage")
    if unattributed_count:
        print(
            f"not attributable to any chunk: {unattributed_count} line(s) -- "
            "likely cause: the lines were recorded before the client "
            "offset field existed, or they are multi-range requests"
        )

    return VIOLATION_EXIT_CODE if worst else OK_EXIT_CODE
