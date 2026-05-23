"""Polygon on-chain provider — smart wallet activity tracker. §11.6 + §14.3.

Phase 3 implementation:
  - Polls a seeded list of smart wallets via Polygon JSON-RPC
  - For each wallet, fetches recent USDC transfers + Polymarket
    ConditionalTokens transfers
  - Emits normalized events for the SmartMoneyStrategy

The list of smart wallets is seeded in `config/smart_wallets.yaml`. Later
phases will scrape the Polymarket leaderboard to refresh the seeds.
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
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.onchain")

# Polymarket Conditional Tokens contract on Polygon mainnet.
POLYMARKET_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# USDC.e on Polygon
POLYGON_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


class PolygonOnchainSource(IngestionSource):
    name = "onchain"

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        smart_wallets: list[str] | None = None,
        poll_sec: int = 60,
        timeout_sec: float = 30.0,
    ) -> None:
        s = get_settings()
        self._rpc_url = rpc_url or s.polygon_rpc_url
        if not self._rpc_url and s.alchemy_api_key.get_secret_value():
            self._rpc_url = f"https://polygon-mainnet.g.alchemy.com/v2/{s.alchemy_api_key.get_secret_value()}"
        self._wallets = [w.lower() for w in (smart_wallets or [])]
        self._poll_sec = poll_sec
        self._timeout = timeout_sec
        self._client: httpx.AsyncClient | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Per-wallet cursor (last-seen block) so we only fetch new tx since last poll.
        self._cursors: dict[str, int] = {}

    async def start(self) -> None:
        if not self._rpc_url:
            log.warning("onchain.disabled", reason="no_rpc_url")
            return
        if not self._wallets:
            log.warning("onchain.disabled", reason="no_wallets_seeded")
            return
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=self._timeout, headers={"User-Agent": "poly-meridian/0.1"}
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="onchain_poll")
        log.info(
            "onchain.start",
            n_wallets=len(self._wallets),
            poll_sec=self._poll_sec,
        )

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

    # ---------- internals ----------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                latest = await self._latest_block()
                for w in self._wallets:
                    if self._stop.is_set():
                        break
                    await self._poll_wallet(w, latest)
            except Exception as exc:
                log.warning("onchain.poll_error", error=str(exc))

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_sec)
                return
            except asyncio.TimeoutError:
                continue

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        assert self._client is not None and self._rpc_url
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
        ):
            with attempt:
                r = await self._client.post(self._rpc_url, json=payload)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RuntimeError(f"rpc_error:{data['error']}")
                return data.get("result")
        return None

    async def _latest_block(self) -> int:
        result = await self._rpc("eth_blockNumber", [])
        return int(result, 16) if isinstance(result, str) else 0

    async def _poll_wallet(self, wallet: str, latest_block: int) -> None:
        # First poll: only fetch the last ~5min of blocks (~150 blocks @ 2s).
        start = self._cursors.get(wallet, max(0, latest_block - 150))
        if start >= latest_block:
            return

        # Topic for ERC1155 TransferSingle is the Polymarket CTF event;
        # we filter only on the wallet address (from OR to topic).
        try:
            logs = await self._rpc(
                "eth_getLogs",
                [
                    {
                        "address": POLYMARKET_CTF_ADDRESS,
                        "fromBlock": hex(start),
                        "toBlock": hex(latest_block),
                        # Topic[2] = from, Topic[3] = to — match either == wallet.
                        # JSON-RPC supports per-topic OR via nested arrays.
                        "topics": [
                            None,
                            None,
                            [None, _addr_topic(wallet)],
                            [None, _addr_topic(wallet)],
                        ],
                    }
                ],
            )
        except Exception as exc:
            log.warning("onchain.getLogs_failed", error=str(exc), wallet=wallet)
            return

        if not isinstance(logs, list):
            return

        for entry in logs:
            evt = {
                "source": self.name,
                "type": "ctf_transfer",
                "ts": datetime.now(UTC),
                "wallet": wallet,
                "payload": entry,
            }
            try:
                self._queue.put_nowait(evt)
            except asyncio.QueueFull:
                log.warning("onchain.queue_full")

        self._cursors[wallet] = latest_block
        log.debug("onchain.poll_done", wallet=wallet, n=len(logs), to_block=latest_block)


def _addr_topic(addr: str) -> str:
    """Zero-pad a 0x-address to a 32-byte topic value."""
    addr_clean = addr.lower().removeprefix("0x")
    return "0x" + addr_clean.rjust(64, "0")
