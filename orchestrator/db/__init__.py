"""asyncpg pool lifecycle, shared by main.py's lifespan, the reaper, and the CLI.

No ORM here or in `db.queries` -- five tables total (D-14's deliberately
small skeleton), raw SQL over asyncpg is less code than wiring an ORM for
this size of schema.
"""
import asyncpg

from orchestrator.config import get_settings


async def create_pool() -> asyncpg.Pool:
    settings = get_settings()
    return await asyncpg.create_pool(settings.database_url)
