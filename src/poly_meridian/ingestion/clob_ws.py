"""Polymarket CLOB WebSocket consumer — market data channel. §11.3.

Subscribes to asset_ids, reconstructs a local order book per token, and
emits normalized events. Handles reconnect with exponential backoff and
PING heartbeat every 10s.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from poly_meridian.ingestion.base import IngestionSource
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.clob_ws")

HEARTBEAT_SEC = 10


class ClobWebsocketSource(IngestionSource):
    """Streams normalized book/trade events to consumers via `events()`."""

    name = "clob_ws"

    def __init__(self, asset_ids: Iterable[str], url: str | None = None) -> None:
        s = get_settings()
        self._url = url or s.polymarket_ws_url
        self._asset_ids = list(asset_ids)
        self._books: dict[str, LocalBook] = {tid: LocalBook(tid) for tid in self._asset_ids}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="clob_ws_loop")
        log.info("clob_ws.start", n_assets=len(self._asset_ids))

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

    def book(self, token_id: str) -> LocalBook | None:
        return self._books.get(token_id)

    async def _run_forever(self) -> None:
        backoffs = [1, 2, 5, 10, 30]
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._run_once()
                attempt = 0  # successful session — reset
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = backoffs[min(attempt, len(backoffs) - 1)] + random.random()
                log.warning(
                    "clob_ws.reconnect",
                    error=str(exc),
                    attempt=attempt,
                    wait_sec=round(wait, 1),
                )
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    return
                except asyncio.TimeoutError:
                    continue

    async def _run_once(self) -> None:
        async with websockets.connect(self._url, ping_interval=None) as ws:
            await ws.send(
                json.dumps(
                    {
                        "auth": {},
                        "type": "market",
                        "assets_ids": self._asset_ids,
                    }
                )
            )
            log.info("clob_ws.subscribed", n=len(self._asset_ids))

            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    self._handle_message(raw)
            finally:
                heartbeat.cancel()
                with contextlib_suppress():
                    await heartbeat

    async def _heartbeat(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                await ws.send("PING")
            except Exception:
                return

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            log.warning("clob_ws.bad_json", raw=str(raw)[:200])
            return

        # Some venues echo bare strings ("PONG") — ignore.
        if not isinstance(msg, (dict, list)):
            return

        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            self._dispatch(m)

    def _dispatch(self, m: dict[str, Any]) -> None:
        event_type = m.get("event_type") or m.get("type")
        asset_id = m.get("asset_id") or m.get("market")
        if event_type == "book" and asset_id in self._books:
            self._books[asset_id].apply_snapshot(m)
            self._enqueue("book", asset_id, m)
        elif event_type == "price_change" and asset_id in self._books:
            self._books[asset_id].apply_price_change(m)
            self._enqueue("price_change", asset_id, m)
        elif event_type == "last_trade_price" and asset_id in self._books:
            self._enqueue("trade", asset_id, m)
        elif event_type == "tick_size_change":
            self._enqueue("tick_size_change", asset_id, m)
        else:
            log.debug("clob_ws.unhandled", event_type=event_type, asset_id=asset_id)

    def _enqueue(self, kind: str, token_id: str | None, payload: dict[str, Any]) -> None:
        evt = {
            "source": self.name,
            "type": kind,
            "ts": datetime.now(UTC),
            "token_id": token_id,
            "payload": payload,
        }
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            log.warning("clob_ws.queue_full", dropping=kind)


class contextlib_suppress:  # tiny shim to avoid importing contextlib at top
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool:
        return True
