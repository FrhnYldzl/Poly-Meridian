"""Polymarket Gamma REST client — markets/events metadata. See §11.1.

Public, read-only API at gamma-api.polymarket.com. Rate limit: 15K/10s.

This module owns only the HTTP I/O. Persistence and normalization happen
in `ingestion/normalize.py` and `storage/`.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.gamma")


class GammaClient:
    def __init__(self, base_url: str | None = None, timeout_sec: float = 15.0) -> None:
        s = get_settings()
        self._base = (base_url or s.polymarket_gamma_host).rstrip("/")
        self._timeout = timeout_sec
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GammaClient":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=self._timeout,
                headers={"User-Agent": "poly-meridian/0.1"},
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert self._client is not None, "call start() first"
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get(path, params=params)
                r.raise_for_status()
                return r.json()

    async def list_active_markets(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """GET /markets?active=true&closed=false. Single page."""
        data = await self._get(
            "/markets",
            params={"active": "true", "closed": "false", "limit": limit, "offset": offset},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return list(data["data"])
        return []

    async def iter_active_markets(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        """Paginate fully through /markets until empty page. Bounded to 50 pages."""
        out: list[dict[str, Any]] = []
        for page in range(50):
            chunk = await self.list_active_markets(limit=page_size, offset=page * page_size)
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < page_size:
                break
        log.info("gamma.iter_active_markets", count=len(out))
        return out

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        return await self._get(f"/markets/{condition_id}")

    async def list_active_events(
        self, *, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "/events",
            params={"active": "true", "closed": "false", "limit": limit, "offset": offset},
        )
        return data if isinstance(data, list) else list((data or {}).get("data", []))

    async def iter_active_events(self, *, page_size: int = 200) -> list[dict[str, Any]]:
        """Paginate fully through /events until empty page. Bounded to 25 pages.
        Used by the category-derivation pipeline — events carry the `tags` array
        that markets reference for canonical Polymarket categories."""
        out: list[dict[str, Any]] = []
        for page in range(25):
            chunk = await self.list_active_events(limit=page_size, offset=page * page_size)
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < page_size:
                break
        log.info("gamma.iter_active_events", count=len(out))
        return out
