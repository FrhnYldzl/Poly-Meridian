"""Polymarket data-api `/trades` poller — primary Smart Money feed.

Why this instead of on-chain log polling?
  - data-api /trades is already-decoded: proxyWallet, side (BUY/SELL),
    asset/conditionId, outcome (Yes/No), size, price, timestamp,
    transactionHash. No log parsing, no on-chain RPC budget.
  - Leaderboard endpoints are 404 — we have to build wallet-tier
    classification ourselves anyway, and the trade firehose IS that source.
  - Catches ALL whales, not just ones we seeded.

Cadence: poll every ~5s. The endpoint is paginated by `offset` and seems
to return most-recent first. We dedupe by transactionHash so re-polls
don't double-count.

Emits events shaped:
  {
    "source": "polymarket_trades",
    "type": "polymarket_trade",
    "ts": datetime,
    "wallet": proxyWallet (lower-hex),
    "condition_id": str,
    "direction": "YES" | "NO",
    "side": "BUY" | "SELL",
    "size_units": float,
    "price": float,
    "size_usd": float,         # size_units × price
    "asset": str,              # token_id (outcome token)
    "tx_hash": str,
  }
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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

log = structlog.get_logger("poly_meridian.polymarket_trades")

TRADES_URL = "https://data-api.polymarket.com/trades"

# How many recent trades to fetch per poll. data-api caps at 500-ish but
# 100 is plenty at ~5s cadence — Polymarket does ~50-200 trades/min total.
DEFAULT_LIMIT = 100


class PolymarketTradesSource(IngestionSource):
    name = "polymarket_trades"

    def __init__(
        self,
        *,
        poll_sec: int = 5,
        timeout_sec: float = 15.0,
        limit: int = DEFAULT_LIMIT,
        seen_cap: int = 20_000,
    ) -> None:
        self._poll_sec = poll_sec
        self._timeout = timeout_sec
        self._limit = limit
        self._seen_cap = seen_cap
        self._client: httpx.AsyncClient | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=20_000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Dedupe by transactionHash — endpoint returns the same trades
        # multiple times between polls.
        self._seen_tx: set[str] = set()
        # Tally per wallet for tier classification — caller reads
        # `aggregate_stats()` to populate the SmartMoneyStrategy's tier map.
        self._wallet_stats: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": "poly-meridian/0.1", "Accept": "application/json"},
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="polymarket_trades_poll")
        log.info("polymarket_trades.start", poll_sec=self._poll_sec)

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

    def wallet_stats(self) -> dict[str, dict[str, Any]]:
        """Snapshot of per-wallet aggregates so the SmartMoneyStrategy tier
        classifier can read it. Refreshed each poll cycle."""
        return dict(self._wallet_stats)

    # ---------- internals ----------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rows = await self._fetch()
                fresh = self._dedupe_and_enqueue(rows)
                if fresh:
                    log.debug(
                        "polymarket_trades.poll",
                        fetched=len(rows),
                        new=fresh,
                        wallets_tracked=len(self._wallet_stats),
                    )
            except Exception as exc:
                log.warning("polymarket_trades.poll_error", error=str(exc))

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_sec)
                return
            except asyncio.TimeoutError:
                continue

    async def _fetch(self) -> list[dict[str, Any]]:
        assert self._client is not None
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get(TRADES_URL, params={"limit": self._limit})
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    return data
                return []
        return []

    def _dedupe_and_enqueue(self, rows: list[dict[str, Any]]) -> int:
        """Add new transactions to the queue, update per-wallet stats."""
        fresh = 0
        for row in rows:
            tx = row.get("transactionHash") or ""
            if not tx or tx in self._seen_tx:
                continue
            self._seen_tx.add(tx)

            wallet = (row.get("proxyWallet") or "").lower()
            condition_id = row.get("conditionId")
            asset = row.get("asset")
            outcome = (row.get("outcome") or "").upper()    # "YES"/"NO"
            side = (row.get("side") or "").upper()          # "BUY"/"SELL"
            size_units = float(row.get("size") or 0)
            price = float(row.get("price") or 0)
            size_usd = size_units * price
            ts_unix = row.get("timestamp")
            try:
                ts = datetime.fromtimestamp(float(ts_unix), tz=UTC) if ts_unix else datetime.now(UTC)
            except (TypeError, ValueError):
                ts = datetime.now(UTC)

            if not wallet or not condition_id or not asset:
                continue
            # outcome must map cleanly to YES/NO. The endpoint can also use
            # "Up"/"Down" for some markets — treat outcomeIndex as truth.
            outcome_idx = row.get("outcomeIndex")
            if outcome_idx == 0:
                direction = "YES"
            elif outcome_idx == 1:
                direction = "NO"
            elif outcome.startswith("Y"):
                direction = "YES"
            elif outcome.startswith("N"):
                direction = "NO"
            else:
                # Unknown outcome shape — skip rather than misclassify.
                continue

            # Per-wallet rolling tally for tier classification.
            stat = self._wallet_stats.setdefault(
                wallet,
                {
                    "first_seen": ts,
                    "last_seen": ts,
                    "trade_count": 0,
                    "total_volume_usd": 0.0,
                    "buy_volume_usd": 0.0,
                    "sell_volume_usd": 0.0,
                    "name": row.get("name") or row.get("pseudonym") or None,
                },
            )
            stat["last_seen"] = ts
            stat["trade_count"] += 1
            stat["total_volume_usd"] += size_usd
            if side == "BUY":
                stat["buy_volume_usd"] += size_usd
            else:
                stat["sell_volume_usd"] += size_usd

            evt = {
                "source": self.name,
                "type": "polymarket_trade",
                "ts": ts,
                "wallet": wallet,
                "condition_id": str(condition_id),
                "asset": str(asset),
                "direction": direction,
                "side": side,
                "size_units": size_units,
                "price": price,
                "size_usd": size_usd,
                "tx_hash": tx,
            }
            try:
                self._queue.put_nowait(evt)
                fresh += 1
            except asyncio.QueueFull:
                log.warning("polymarket_trades.queue_full")
                break

        # Bounded LRU for seen_tx.
        if len(self._seen_tx) > self._seen_cap:
            self._seen_tx = set(list(self._seen_tx)[-self._seen_cap // 2 :])
        return fresh
