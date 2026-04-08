"""
alembic/env.py
──────────────
Alembic migration environment — async SQLAlchemy.

Running migrations:
  alembic upgrade head          # Apply all pending migrations
  alembic downgrade -1          # Roll back one migration
  alembic revision --autogenerate -m "description"  # Generate new migration

First-time setup on an existing database (that used create_all):
  alembic stamp 001             # Mark initial migration as applied
  alembic upgrade head          # Apply only newer migrations (e.g., 002 onwards)
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Load app settings (reads DATABASE_URL from .env)
from config.settings import settings

# Import all models so their metadata is registered
import database.models  # noqa: F401
from database.connection import Base

# Alembic Config object (gives access to alembic.ini values)
config = context.config

# Override sqlalchemy.url from app settings so we have one source of truth
config.set_main_option("sqlalchemy.url", settings.database_url)

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate support
target_metadata = Base.metadata


# ── Offline mode (generate SQL without DB connection) ────────────────────────

def run_migrations_offline() -> None:
    """
    Run migrations in offline mode.
    Generates SQL scripts without requiring a live DB connection.
    Usage: alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url                      = url,
        target_metadata          = target_metadata,
        literal_binds            = True,
        dialect_opts             = {"paramstyle": "named"},
        compare_type             = True,
        compare_server_default   = True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online mode (apply migrations against a live DB) ─────────────────────────

def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection               = connection,
        target_metadata          = target_metadata,
        compare_type             = True,
        compare_server_default   = True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against a live async database."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix          = "sqlalchemy.",
        poolclass       = pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
