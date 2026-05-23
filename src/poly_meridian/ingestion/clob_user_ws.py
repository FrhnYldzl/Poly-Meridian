"""Polymarket CLOB user-channel WebSocket. §16.4.

Streams events scoped to *our* wallet:
  - `order` — status changes on our orders (LIVE, PARTIAL, FILLED, CANCELLED)
  - `trade` — fills attributed to us
  - `balance` — wallet balance updates

URL: `wss://ws-subscriptions-clob.polymarket.com/ws/user`
Auth: HMAC headers signed with API key/secret/passphrase.

LiveExecutor.on_event() is the dispatch entrypoint — it updates local
`Order` state, applies fills to the ledger via the on_fill callback.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Callable

import structlog
import websockets

from poly_meridian.ingestion.base import IngestionSource
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.clob_user_ws")

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class ClobUserChannel(IngestionSource):
    """User-scoped WS consumer. Auths via API key/secret/passphrase."""

    name = "clob_user_ws"

    def __init__(
        self,
        *,
        on_order_update: Callable[[dict[str, Any]], None] | None = None,
        on_trade: Callable[[dict[str, Any]], None] | None = None,
        url: str | None = None,
    ) -> None:
        s = get_settings()
        self._url = url or USER_WS_URL
        self._api_key = s.polymarket_api_key.get_secret_value()
        self._api_secret = s.polymarket_api_secret.get_secret_value()
        self._passphrase = s.polymarket_passphrase.get_secret_value()
        self._on_order = on_order_update
        self._on_trade = on_trade
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5_000)

    async def start(self) -> None:
        if not (self._api_key and self._api_secret and self._passphrase):
            log.warning("user_ws.disabled", reason="api creds missing")
            return
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="clob_user_ws")
        log.info("user_ws.start")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def healthcheck(self) -> bool:
        return self._task is not None and not self._task.done()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                evt = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield evt

    # ---------- internals ----------

    async def _run_forever(self) -> None:
        backoffs = [1, 2, 5, 10, 30]
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._stream_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = backoffs[min(attempt, len(backoffs) - 1)] + random.random()
                log.warning("user_ws.reconnect", error=str(exc), attempt=attempt, wait=round(wait, 1))
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    return
                except asyncio.TimeoutError:
                    continue

    async def _stream_once(self) -> None:
        async with websockets.connect(self._url, ping_interval=None) as ws:
            # Subscribe payload per Polymarket docs.
            await ws.send(json.dumps({
                "type": "user",
                "auth": {
                    "apiKey": self._api_key,
                    "secret": self._api_secret,
                    "passphrase": self._passphrase,
                },
            }))
            log.info("user_ws.subscribed")
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._dispatch(raw)

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            log.warning("user_ws.bad_json", raw=str(raw)[:200])
            return
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if not isinstance(m, dict):
                continue
            event_type = m.get("event_type") or m.get("type")
            if event_type in ("order", "order_update"):
                if self._on_order is not None:
                    try:
                        self._on_order(m)
                    except Exception as exc:
                        log.warning("user_ws.on_order_error", error=str(exc))
            elif event_type in ("trade", "trade_update", "fill"):
                if self._on_trade is not None:
                    try:
                        self._on_trade(m)
                    except Exception as exc:
                        log.warning("user_ws.on_trade_error", error=str(exc))
            evt = {
                "source": self.name,
                "type": str(event_type or "unknown"),
                "ts": datetime.now(UTC),
                "payload": m,
            }
            try:
                self._queue.put_nowait(evt)
            except asyncio.QueueFull:
                log.warning("user_ws.queue_full")
