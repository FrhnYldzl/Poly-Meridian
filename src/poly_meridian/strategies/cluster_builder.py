"""Cluster state builder — converts on-chain CTF transfer events into
per-condition `ClusterState` snapshots consumed by `SmartMoneyStrategy`.

See MASTER_SPEC v1.1 §14.3.

Lifecycle:
  - `start(onchain_source)` — spawns a consumer task on the source's events
  - `attach_to_strategy(strategy)` — pushes fresh ClusterState whenever it
    crosses the strategy's cluster threshold
  - `stop()` — clean shutdown
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from poly_meridian.strategies.smart_money import (
    ClusterState,
    SmartMoneyStrategy,
    WalletFlow,
)

log = structlog.get_logger("poly_meridian.cluster_builder")


class ClusterStateBuilder:
    """Aggregates on-chain CTF transfer events into per-condition flows.

    Notes on event interpretation (Phase 5a simplification):
      - Each CTF TransferSingle has from/to + token_id (= outcome token).
      - If `to == wallet` → wallet acquired the token → BUY of that outcome.
      - If `from == wallet` → wallet disposed → SELL (treated as opposite-side
        signal for the cluster).
      - We don't (yet) know USD notional from the topic data alone; Phase 5b
        will pair with USDC transfers from the same wallet at the same tx
        for true notional. For now we use the raw token size as a proxy.

    The actual `token_id → condition_id` mapping requires a lookup table —
    main loop attaches it via `register_token_to_condition()`.
    """

    def __init__(
        self,
        *,
        window_sec: int = 3600,
        decay_sec: int = 1800,        # latency decay per §14.3 — 30min freshness
    ) -> None:
        self._window_sec = window_sec
        self._decay_sec = decay_sec
        # per condition_id → list of WalletFlow rows
        self._flows: dict[str, list[WalletFlow]] = defaultdict(list)
        self._wallet_tier: dict[str, int] = {}       # wallet → tier
        self._token_to_condition: dict[str, tuple[str, str]] = {}
        # ^ token_id → (condition_id, direction "YES" or "NO")
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._strategy: SmartMoneyStrategy | None = None

    def register_token_to_condition(
        self, *, token_id: str, condition_id: str, direction: str
    ) -> None:
        self._token_to_condition[token_id] = (condition_id, direction.upper())

    def register_wallet_tier(self, wallet: str, tier: int) -> None:
        self._wallet_tier[wallet.lower()] = tier

    def attach_to_strategy(self, strategy: SmartMoneyStrategy) -> None:
        self._strategy = strategy

    async def start(self, events: AsyncIterator[dict[str, Any]]) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._consume(events), name="cluster_builder")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def snapshot_state(self, condition_id: str) -> ClusterState | None:
        """Build a fresh ClusterState for `condition_id` from current flows.

        Returns None if nothing recent."""
        flows = self._flows.get(condition_id, [])
        if not flows:
            return None
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self._window_sec)
        latency_cutoff = now - timedelta(seconds=self._decay_sec)

        # Drop stale.
        flows = [f for f in flows if f.last_update >= cutoff]
        self._flows[condition_id] = flows
        if not flows:
            return None

        # Apply latency decay: only events within `decay_sec` count toward
        # fresh cluster (per §14.3).
        fresh = [f for f in flows if f.last_update >= latency_cutoff]
        if not fresh:
            return None

        yes_flows = [f for f in fresh if f.direction == "YES"]
        no_flows = [f for f in fresh if f.direction == "NO"]

        return ClusterState(
            condition_id=condition_id,
            yes_flows=yes_flows,
            no_flows=no_flows,
            last_update=max(f.last_update for f in fresh),
        )

    # ---------- internals ----------

    async def _consume(self, events: AsyncIterator[dict[str, Any]]) -> None:
        try:
            async for evt in events:
                if self._stop.is_set():
                    break
                if evt.get("type") != "ctf_transfer":
                    continue
                self._apply_event(evt)
                # Update strategy with fresh cluster snapshot if available.
                cid = self._condition_from_event(evt)
                if cid and self._strategy is not None:
                    cs = self.snapshot_state(cid)
                    if cs is not None:
                        self._strategy.attach_cluster_state(cs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("cluster_builder.consume_error", error=str(exc))

    def _condition_from_event(self, evt: dict[str, Any]) -> str | None:
        token_id = _parse_token_id(evt.get("payload", {}))
        if token_id is None:
            return None
        mapping = self._token_to_condition.get(token_id)
        return mapping[0] if mapping else None

    def _apply_event(self, evt: dict[str, Any]) -> None:
        wallet = (evt.get("wallet") or "").lower()
        if not wallet:
            return
        token_id = _parse_token_id(evt.get("payload", {}))
        if token_id is None:
            return
        mapping = self._token_to_condition.get(token_id)
        if mapping is None:
            return
        condition_id, side_direction = mapping

        # BUY of YES-token = YES direction. BUY of NO-token = NO direction.
        # We only count receipts (`to == wallet`) as buys for cluster purposes.
        payload = evt.get("payload", {})
        is_buy = _is_receipt(payload, wallet)
        if not is_buy:
            return

        # Notional proxy: token quantity. Phase 5b refines using USDC pair.
        qty = _parse_value(payload)
        usd_proxy = float(qty)

        flow = WalletFlow(
            wallet=wallet,
            direction=side_direction,
            net_usd=usd_proxy,
            last_update=evt.get("ts", datetime.now(UTC)),
        )
        self._flows[condition_id].append(flow)

        # Trim window.
        cutoff = datetime.now(UTC) - timedelta(seconds=self._window_sec)
        self._flows[condition_id] = [
            f for f in self._flows[condition_id] if f.last_update >= cutoff
        ]


def _parse_token_id(payload: dict[str, Any]) -> str | None:
    """ERC1155 TransferSingle: topic[3] is the token id (uint256, hex)."""
    topics = payload.get("topics", [])
    if not isinstance(topics, list) or len(topics) < 4:
        return None
    raw = topics[3]
    if not isinstance(raw, str):
        return None
    try:
        return str(int(raw, 16))
    except ValueError:
        return None


def _is_receipt(payload: dict[str, Any], wallet: str) -> bool:
    """True iff topic[3] (to) matches wallet (32-byte padded)."""
    topics = payload.get("topics", [])
    if not isinstance(topics, list) or len(topics) < 4:
        return False
    to_topic = topics[3] if len(topics) > 3 else None
    if not isinstance(to_topic, str):
        return False
    return wallet.lower().removeprefix("0x") in to_topic.lower()


def _parse_value(payload: dict[str, Any]) -> float:
    """TransferSingle data = (id, value). Quick parse — Phase 5b makes this
    rigorous via abi-decoding."""
    data = payload.get("data", "")
    if not isinstance(data, str) or len(data) < 130:
        return 0.0
    try:
        # data is 0x-prefixed hex, 64 chars for id, 64 chars for value.
        value_hex = data[2 + 64:2 + 128]
        return float(int(value_hex, 16))
    except (ValueError, TypeError):
        return 0.0
