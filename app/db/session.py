"""Async engine, session factory, and the FastAPI session dependency.

The engine is built lazily on first use rather than at import time. Importing
the models -- which is all Alembic's env.py and the test suite need -- must not
construct a connection pool or require a reachable database.
"""

from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()

    connect_args: dict[str, Any] = {}
    if settings.DB_USE_PGBOUNCER:
        # pgbouncer in transaction mode cannot reuse asyncpg prepared statements.
        connect_args |= {"statement_cache_size": 0, "prepared_statement_cache_size": 0}

    return create_async_engine(
        settings.database_url,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
        connect_args=connect_args,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """The process-wide engine, created on first call."""
    return build_engine()


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session per request; rolls back on error, always closes."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the engine, but only if one was ever created."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
