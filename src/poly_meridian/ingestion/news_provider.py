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
        # Default broad-spectrum queries covering all Polymarket categories.
        # Overridden by main loop with dynamic per-category queries built from
        # active markets — that's what makes the news *relevant*.
        self._queries = queries or [
            # Politics + macro
            "politics OR election OR senate OR congress",
            "federal reserve OR inflation OR interest rate OR CPI OR unemployment",
            "geopolitics OR war OR sanctions OR diplomacy OR treaty",
            # Crypto
            "bitcoin OR ethereum OR solana OR cryptocurrency OR ETF",
            # Sports — huge Polymarket category
            "NBA OR NFL OR MLB OR NHL OR Premier League",
            "FIFA OR World Cup OR Champions League",
            "UFC OR MMA OR boxing OR tennis",
            # Tech + Mentions
            "OpenAI OR ChatGPT OR Anthropic OR Tesla OR SpaceX",
            "Apple OR Google OR Microsoft OR Meta OR NVIDIA",
            # Markets / finance
            "stock market OR S&P 500 OR NASDAQ OR Dow Jones",
            # Weather / culture
            "hurricane OR earthquake OR weather",
            "Grammys OR Oscars OR Eurovision OR awards",
        ]
        self._dynamic_queries: list[str] = []
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

    def set_dynamic_queries(self, queries: list[str]) -> None:
        """Replace queries with dynamic ones built from active market keywords."""
        if queries:
            self._dynamic_queries = queries

    # GDELT enforces "one request per 5 seconds" — without this gap they
    # return a plaintext rate-limit notice ("Please limit requests to one
    # every 5 seconds…") that fails our JSON guard and silently drops the
    # cycle. We use 5.5s for a small safety margin.
    _MIN_QUERY_GAP_SEC = 5.5

    async def _loop(self) -> None:
        while not self._stop.is_set():
            queries = self._dynamic_queries or self._queries
            n_articles = 0
            n_empty = 0
            try:
                for i, q in enumerate(queries):
                    if self._stop.is_set():
                        break
                    if i > 0:
                        # Stay below GDELT's 1-req-per-5s ceiling.
                        try:
                            await asyncio.wait_for(
                                self._stop.wait(), timeout=self._MIN_QUERY_GAP_SEC
                            )
                            return
                        except asyncio.TimeoutError:
                            pass
                    articles = await self._fetch_query(q)
                    if not articles:
                        n_empty += 1
                    n_articles += len(articles)
                    for a in articles:
                        self._enqueue(a)
                log.info(
                    "news.poll_cycle",
                    n_queries=len(queries),
                    n_articles=n_articles,
                    n_empty_queries=n_empty,
                )
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
                # GDELT sometimes returns an empty body or non-JSON HTML on
                # rate limit / no results. Guard against both.
                body = r.text or ""
                if not body.strip().startswith("{"):
                    # Surface rate-limit hits — they used to be silent.
                    if "limit requests" in body.lower() or "5 seconds" in body:
                        log.warning(
                            "news.gdelt.rate_limited",
                            query=query[:60],
                            hint="increase _MIN_QUERY_GAP_SEC",
                        )
                    return []
                try:
                    data = r.json()
                except Exception:
                    return []
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
