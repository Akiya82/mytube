"""Alembic environment — async engine against MYTUBE_DATABASE_URL.

Deliberately minimal: this scaffold lets `alembic upgrade head` succeed
as a no-op inside docker-entrypoint.sh, so `docker compose up --wait` can
come up healthy before any schema exists. The revisions under
migrations/versions/ and target_metadata for autogenerate extend this
file, they do not replace it.

MYTUBE_DATABASE_URL is stored as `postgresql://...` (the form asyncpg.connect
accepts directly) — this file is the one place that swaps the scheme to
`postgresql+asyncpg://` for SQLAlchemy's async engine, matching the async
driver Alembic runs against in this project.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    from orchestrator.config import get_settings

    return get_settings().database_url.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
