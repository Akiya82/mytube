#!/usr/bin/env sh
# Entrypoint for the orchestrator image (verdicts M1, m2). Runs before uvicorn
# on every container start: alembic upgrade head, then ensure_bucket(). Both
# are idempotent, so a restart is safe. There is deliberately no separate
# one-shot migration/init service in docker-compose.yml: `docker compose up
# --wait` treats a container exit as failure, and a one-shot service would
# break the top-level SETUP command. With a single `api` replica in this
# phase there is no migration race; if a second replica is ever added,
# migrations need to move out of here — recorded so a future reader doesn't
# hit this silently.
set -eu

echo "docker-entrypoint: alembic upgrade head"
alembic upgrade head

echo "docker-entrypoint: ensure_bucket"
python -c "from orchestrator.storage import ensure_bucket; ensure_bucket()"

echo "docker-entrypoint: ensure_multipart_lifecycle_rule"
python -c "from orchestrator.storage import ensure_multipart_lifecycle_rule; ensure_multipart_lifecycle_rule()"

echo "docker-entrypoint: exec $*"
exec "$@"
