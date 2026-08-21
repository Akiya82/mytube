"""cache-report: turn the D-28 cache criterion into a command with an exit
code (D-29), instead of a phrase someone eyeballs at 3am after movie night.

The regex below is synchronized field-for-field, in the same order, with
the `log_format cache_audit` directive in src/edge/nginx.conf.template:

    log_format cache_audit '$time_iso8601 $uri $slice_range '
                            '$upstream_cache_status $status $body_bytes_sent';

If that directive's field list or order ever changes, this regex must
change with it -- a silent mismatch does not error, it just parses zero
lines, which is exactly the failure mode the zero-parsed-lines guard below
exists to catch instead of quietly reporting "no violations found".

No viewer IP anywhere in the format or in this module (D-30): the report's
output is meant to be safe to paste into an issue on the public repo.

**Measured deviation from the plan's original assumption.** The plan this
module was written against assumed a full download of N chunks produces
N+1 log lines: N per-chunk subrequest lines plus one separate "main
request" line, the latter excluded from chunk statistics entirely. Live
measurement against this exact stack (two full downloads of the same
title, cold then warm) shows that assumption does not hold: nginx's slice
module fetches chunk #1 directly within the master request, not as an
internal-redirect subrequest, so chunk #1 never gets its own `bytes=0-...`
log line -- its `$upstream_cache_status` is logged on the "main" line
instead, which is why that line's cache status is genuinely observed
flipping from MISS (cold) to HIT (warm) across two runs of the same file.
A download of N chunks therefore produces exactly N log lines, not N+1
(matching the 64-vs-65 discrepancy already recorded in
src/docs/experiments/07-object-storage.md). Treating the empty-`slice_range`
line as "not a chunk" and discarding it, as the plan originally specified,
would silently drop chunk #1's D-28 measurement on every single file --
this module instead treats it as its own bucket (keyed on the literal `-`
slice_range value nginx always logs for it), so a chunk-1 repeat fetch is
caught exactly like any other chunk's.
"""
import re
from collections import Counter
from datetime import datetime

LINE_RE = re.compile(
    r"(?P<time>\S+) (?P<uri>\S+) (?P<slice_range>\S+) "
    r"(?P<cache_status>\S+) (?P<status>\d+) (?P<bytes>\d+)"
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


def cache_report(log_path: str, since: str | None = None) -> int:
    """Parse `log_path` (cache_audit format) and print the D-28 report.

    Returns the process exit code: 0 if no chunk was fetched from storage
    more than once, 1 if D-28 was violated, and 2 (verdict B2) if there was
    not enough data to say either way -- zero parsed lines, whether because
    the log is empty, no line matched the format, or `--since` filtered
    everything out. Zero parsed lines must never read as "zero violations":
    nginx does not log slice subrequests by default, so a misconfigured
    edge (missing `log_subrequest on;` in `location ~ ^/blob/`) or a format
    drift between this regex and cache_audit would otherwise make a broken
    cache report as "passed".
    """
    read_count = 0
    since_dt = _parse_since(since) if since else None

    per_slice: Counter[tuple[str, str]] = Counter()
    upstream_hits: Counter[tuple[str, str]] = Counter()
    parsed_count = 0

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

            # slice_range == "-" is chunk #1: nginx's slice module fetches
            # it directly in the master request rather than as an internal
            # subrequest, so it never gets a "bytes=0-..." line of its own
            # -- its cache status is logged right here instead (see module
            # docstring for the live measurement that established this).
            # Bucketing on the literal "-" is safe and sufficient: every
            # repeat fetch of chunk #1 for this uri logs the same "-".
            key = (m["uri"], m["slice_range"])
            per_slice[key] += 1
            if m["cache_status"] in UPSTREAM_STATUSES:
                upstream_hits[key] += 1

    if parsed_count == 0:
        print(
            "недостаточно данных: прочитано "
            f"{read_count} строк(и), из них разобрано 0 -- "
            "мерить нечем, это не значит, что нарушителей нет"
        )
        print(
            "вероятная причина: в src/edge/nginx.conf.template для "
            "location ~ ^/blob/ не включён `log_subrequest on;`, "
            "либо формат access_log разошёлся с log_format cache_audit"
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

    print(f"прочитано строк: {read_count}, разобрано: {parsed_count}")
    print(f"уникальных кусков: {total_slices}, доля HIT: {hit_ratio:.1%}")
    print(f"кусков с более чем одним запросом к хранилищу: {len(worst)}")
    for uri, rng in worst[:10]:
        print(f"  {uri} {rng}: {upstream_hits[(uri, rng)]} запросов к хранилищу")

    return VIOLATION_EXIT_CODE if worst else OK_EXIT_CODE
