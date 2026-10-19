#!/bin/bash
# backup.sh -- pg_dump on a loop, running straight inside the postgres:16-
# alpine image already used by service `db`: no scheduler daemon,
# no extra image, `pg_dump` is already on this image's PATH. Runs as the
# `db-backup` service in docker-compose.yml.
#
# Why this exists from the start, not "later": video files can be
# re-uploaded from source; the slug history (which address plays what,
# and which addresses are tombstoned) cannot -- it only exists in
# this database. Losing it breaks every link already handed out to friends.
#
# Usage:
#   backup.sh          -- loop forever, one dump every 24h
#   backup.sh --once    -- do a single dump and exit (used by the compose
#                          verify step, and by hand for an on-demand backup)
set -euo pipefail

BACKUP_DIR="${MYTUBE_BACKUP_DIR:-/backups}"
KEEP_DAYS="${MYTUBE_BACKUP_KEEP_DAYS:-7}"

dump_once() {
  mkdir -p "$BACKUP_DIR"
  local ts out
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  out="$BACKUP_DIR/mytube-${ts}.sql.gz"

  # --clean --if-exists makes the dump self-sufficient: it emits DROP ... IF
  # EXISTS before every CREATE, so it restores into a live database as well as
  # an empty one. Without it the dump only loads into a freshly created database,
  # and restoring over an existing one fails on the first CREATE TABLE — which is
  # the case that actually matters, since you restore when the data is there and
  # wrong, not when it is absent.
  #
  # The flag below drops only the *rows* of the session table, not the table
  # itself: a session is a login, and restoring last week's dump should not
  # resurrect logins that should have died days ago. The table's own
  # definition stays in the dump, which is what keeps the restore's drop
  # order coherent -- a live database already has a sessions row referencing
  # operator_credential by foreign key (migrations run on every container
  # start), and the table-omitting form of this flag would drop both the
  # CREATE and the DROP TABLE IF EXISTS for it, leaving that foreign key
  # standing when the dump reaches DROP TABLE IF EXISTS
  # public.operator_credential and aborting the whole restore under
  # --clean --if-exists's own ON_ERROR_STOP requirement. The credential row
  # itself is deliberately not excluded: a real-cost password hash is
  # exactly what a backup is for, and a restore that left nobody able to
  # sign in would be the worse outcome.
  if pg_dump --clean --if-exists --exclude-table-data=public.sessions "$MYTUBE_DATABASE_URL" | gzip > "${out}.tmp"; then
    mv "${out}.tmp" "$out"
    echo "backup: ok -- $out ($(wc -c < "$out") bytes)"
  else
    rm -f "${out}.tmp"
    echo "backup: FAILED at $ts" >&2
    return 1
  fi

  # `find -delete` on zero matches is a no-op, not an error -- safe under
  # `set -e`, unlike a `grep` with no match (a shell trap).
  find "$BACKUP_DIR" -name 'mytube-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete
}

if [[ "${1:-}" == "--once" ]]; then
  dump_once
  exit $?
fi

while true; do
  dump_once || echo "backup: run failed, will retry after the usual interval" >&2
  sleep 86400
done
