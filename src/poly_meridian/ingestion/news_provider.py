"""News ingestion — GDELT 2.0 DOC API polling. See §11.4.

GDELT is free, refreshes every 15min, covers 100+ countries. We poll
the DOC API for the last 15min window per category, dedupe by article
URL hash, and emit normalized news events.

API: https://api.gdeltproject.org/api/v2/doc/doc
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from poly_meridian.ingestion.base import IngestionSource

log = structlog.get_logger("poly_meridian.news")

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"


class GdeltNewsSource(IngestionSource):
    name = "gdelt"

    def __init__(
        self,
        *,
        queries: list[str] | None = None,
        poll_sec: int = 900,
        timeout_sec: float = 30.0,
    ) -> None:
        # Sensible defaults — categories that prediction markets care about.
        self._queries = queries or [
            "politics OR election",
            "cryptocurrency OR bitcoin OR ethereum",
            "federal reserve OR inflation OR rate",
            "geopolitics OR war OR sanctions",
        ]
        self._poll_sec = poll_sec
        self._timeout = timeout_sec
        self._client: httpx.AsyncClient | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._seen_ids: set[str] = set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=self._timeout, headers={"User-Agent": "poly-meridian/0.1"}
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="gdelt_poll")
        log.info("news.start", queries=len(self._queries), poll_sec=self._poll_sec)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def healthcheck(self) -> bool:
        return self._task is not None and not self._task.done()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield evt

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for q in self._queries:
                    if self._stop.is_set():
                        break
                    articles = await self._fetch_query(q)
                    for a in articles:
                        self._enqueue(a)
            except Exception as exc:
                log.warning("news.poll_error", error=str(exc))

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_sec)
                return
            except asyncio.TimeoutError:
                pass

    async def _fetch_query(self, query: str) -> list[dict[str, Any]]:
        assert self._client is not None
        params: dict[str, str | int] = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": 75,
            "timespan": "15min",
        }
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get(GDELT_DOC_API, params=params)
                r.raise_for_status()
                data = r.json() if r.text else {}
                return list(data.get("articles", []))
        return []

    def _enqueue(self, article: dict[str, Any]) -> None:
        url = article.get("url") or ""
        if not url:
            return
        article_id = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()
        if article_id in self._seen_ids:
            return
        self._seen_ids.add(article_id)
        # bounded LRU-ish cap
        if len(self._seen_ids) > 50_000:
            self._seen_ids = set(list(self._seen_ids)[-25_000:])

        evt = {
            "source": self.name,
            "type": "news_article",
            "ts": datetime.now(UTC),
            "payload": {
                "article_id": article_id,
                "url": url,
                "title": article.get("title"),
                "source": article.get("domain") or article.get("sourcecountry"),
                "seendate": article.get("seendate"),
                "language": article.get("language"),
            },
        }
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            log.warning("news.queue_full")


def parse_gdelt_seendate(s: str | None) -> datetime | None:
    """GDELT 'seendate' is `YYYYMMDDTHHMMSSZ`."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def fallback_window_now() -> tuple[datetime, datetime]:
    """For backfill cursors: (start, end) of the last 15-minute window."""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    minute = (now.minute // 15) * 15
    end = now.replace(minute=minute)
    start = end - timedelta(minutes=15)
    return start, end
