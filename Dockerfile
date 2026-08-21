# Orchestrator image. Also serves as the CLI image (D-36): commands run via
# `docker compose exec api python -m orchestrator.cli ...` against the
# already-running container — nothing is installed on the host.
FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY orchestrator ./orchestrator
COPY alembic.ini ./
COPY migrations ./migrations

RUN pip install --no-cache-dir .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
