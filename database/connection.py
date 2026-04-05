"""
database/connection.py
──────────────────────
Async SQLAlchemy engine + session factory.
Redis connection pool.
Both are created once and reused across the app.
"""
from __future__ import annotations

import redis.asyncio as aioredis
import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings

log = structlog.get_logger(__name__)

# ─── SQLAlchemy ───────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


def create_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,        # reconnect on stale connections
        echo=settings.is_dev,      # log SQL in dev mode only
    )


# Module-level singletons — initialised lazily
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db_session() -> AsyncSession:
    """
    Dependency / context manager for a database session.

    Usage:
        async with await get_db_session() as session:
            result = await session.execute(...)
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def init_db() -> None:
    """Create all tables (dev only — production uses Alembic migrations)."""
    from database.models import Base as ModelBase  # noqa: F401 — registers models
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
    log.info("database.init", status="tables_created")


async def close_db() -> None:
    """Dispose the engine (call on app shutdown)."""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None
        log.info("database.close", status="engine_disposed")


# ─── Redis ────────────────────────────────────────────────────────────────────

_redis_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the shared async Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_pool = None
        log.info("redis.close", status="pool_closed")
