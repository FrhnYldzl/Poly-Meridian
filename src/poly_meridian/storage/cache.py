"""Redis cache wrapper. Used for hot-path reads: last book, last features."""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
import structlog

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.cache")


class Cache:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or get_settings().redis_url
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        log.info("cache.connect", url=self._url)
        self._client = aioredis.from_url(self._url, decode_responses=True)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("Cache.connect() must be called first")
        return self._client

    async def set_json(self, key: str, value: Any, ttl_sec: int | None = None) -> None:
        payload = json.dumps(value, default=str)
        if ttl_sec:
            await self.client.set(key, payload, ex=ttl_sec)
        else:
            await self.client.set(key, payload)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.client.get(key)
        return json.loads(raw) if raw is not None else None

    async def delete(self, key: str) -> None:
        await self.client.delete(key)


_cache_singleton: Cache | None = None


async def get_cache() -> Cache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = Cache()
        await _cache_singleton.connect()
    return _cache_singleton


async def close_cache() -> None:
    global _cache_singleton
    if _cache_singleton is not None:
        await _cache_singleton.disconnect()
        _cache_singleton = None
