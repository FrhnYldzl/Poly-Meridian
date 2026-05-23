"""Async Postgres pool + SQLAlchemy async engine. See MASTER_SPEC §12."""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.storage")


class Database:
    """Wraps both an asyncpg pool (for hot-path writes) and a SQLAlchemy
    async engine (for ORM + migrations). One process, one Database."""

    def __init__(self, dsn: str | None = None) -> None:
        s = get_settings()
        self._sa_dsn = dsn or s.postgres_url
        self._pg_dsn = self._sa_dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._pool: asyncpg.Pool | None = None

    async def connect(self, *, min_size: int = 2, max_size: int = 10) -> None:
        if self._engine is not None:
            return
        log.info("db.connect", dsn=self._safe_dsn())
        self._engine = create_async_engine(self._sa_dsn, pool_pre_ping=True, future=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._pool = await asyncpg.create_pool(
            self._pg_dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
        )

    async def disconnect(self) -> None:
        log.info("db.disconnect")
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self._sessionmaker = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() must be called first")
        return self._pool

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database.connect() must be called first")
        return self._engine

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("Database.connect() must be called first")
        async with self._sessionmaker() as s:
            yield s

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn:
            yield conn

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    def _safe_dsn(self) -> str:
        # Hide password from logs.
        if "@" not in self._sa_dsn:
            return self._sa_dsn
        prefix, rest = self._sa_dsn.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0] if ":" in creds else creds
        return f"{prefix}://{user}:***@{host}"


_db_singleton: Database | None = None


async def get_db() -> Database:
    global _db_singleton
    if _db_singleton is None:
        _db_singleton = Database()
        await _db_singleton.connect()
    return _db_singleton


async def close_db() -> None:
    global _db_singleton
    if _db_singleton is not None:
        await _db_singleton.disconnect()
        _db_singleton = None
