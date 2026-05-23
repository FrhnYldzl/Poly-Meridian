"""AgentStateBroker — in-process pub/sub for the operator dashboard.

The trading pipeline pushes events here; SSE subscribers receive them.
Decoupled from the trading loop so a slow/disconnected dashboard never
blocks order flow.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

log = structlog.get_logger("poly_meridian.api.state")

MAX_RECENT = 200


@dataclass
class Snapshot:
    """One-shot agent state for the dashboard bootstrap."""

    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    mode: str = "paper"
    nav_usd: float = 0.0
    cash_usd: float = 0.0
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    open_position_count: int = 0
    daily_pnl_pct: float = 0.0
    total_exposure_pct: float = 0.0
    kill_switch_engaged: bool = False
    kill_switch_reason: str | None = None
    strategies_enabled: list[str] = field(default_factory=list)
    last_orders: list[dict[str, Any]] = field(default_factory=list)
    last_signals: list[dict[str, Any]] = field(default_factory=list)
    smart_money_clusters: list[dict[str, Any]] = field(default_factory=list)
    markets_watched: int = 0
    uptime_sec: float = 0.0

    def asdict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "mode": self.mode,
            "nav_usd": self.nav_usd,
            "cash_usd": self.cash_usd,
            "open_positions": self.open_positions,
            "open_position_count": self.open_position_count,
            "daily_pnl_pct": self.daily_pnl_pct,
            "total_exposure_pct": self.total_exposure_pct,
            "kill_switch_engaged": self.kill_switch_engaged,
            "kill_switch_reason": self.kill_switch_reason,
            "strategies_enabled": self.strategies_enabled,
            "last_orders": self.last_orders,
            "last_signals": self.last_signals,
            "smart_money_clusters": self.smart_money_clusters,
            "markets_watched": self.markets_watched,
            "uptime_sec": self.uptime_sec,
        }


class AgentStateBroker:
    """Holds the latest snapshot + rolling event log; multiplexes to subscribers."""

    def __init__(self) -> None:
        self._snapshot = Snapshot()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT)
        self._started_at = time.time()

    @property
    def snapshot(self) -> Snapshot:
        self._snapshot.uptime_sec = time.time() - self._started_at
        return self._snapshot

    def update_portfolio(
        self,
        *,
        nav_usd: float | Decimal,
        cash_usd: float | Decimal,
        open_position_count: int,
        daily_pnl_pct: float,
        total_exposure_pct: float,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._snapshot.ts = datetime.now(UTC)
        self._snapshot.nav_usd = float(nav_usd)
        self._snapshot.cash_usd = float(cash_usd)
        self._snapshot.open_position_count = open_position_count
        self._snapshot.daily_pnl_pct = daily_pnl_pct
        self._snapshot.total_exposure_pct = total_exposure_pct
        if open_positions is not None:
            self._snapshot.open_positions = open_positions

    def update_kill_switch(self, *, engaged: bool, reason: str | None = None) -> None:
        prev = self._snapshot.kill_switch_engaged
        self._snapshot.kill_switch_engaged = engaged
        self._snapshot.kill_switch_reason = reason
        if engaged != prev:
            self._enqueue({"type": "kill_switch", "engaged": engaged, "reason": reason})

    def update_mode(self, mode: str) -> None:
        self._snapshot.mode = mode

    def update_strategies(self, names: list[str]) -> None:
        self._snapshot.strategies_enabled = names

    def update_markets_watched(self, count: int) -> None:
        self._snapshot.markets_watched = count

    def push_signal(self, signal: dict[str, Any]) -> None:
        signal = {**signal, "ts": signal.get("ts") or datetime.now(UTC).isoformat()}
        self._snapshot.last_signals.insert(0, signal)
        self._snapshot.last_signals = self._snapshot.last_signals[:50]
        self._enqueue({"type": "signal", "data": signal})

    def push_order(self, order: dict[str, Any]) -> None:
        order = {**order, "ts": order.get("ts") or datetime.now(UTC).isoformat()}
        self._snapshot.last_orders.insert(0, order)
        self._snapshot.last_orders = self._snapshot.last_orders[:50]
        self._enqueue({"type": "order", "data": order})

    def push_cluster(self, cluster: dict[str, Any]) -> None:
        # Dedup by condition_id — most-recent wins.
        cid = cluster.get("condition_id")
        if cid:
            self._snapshot.smart_money_clusters = [
                c for c in self._snapshot.smart_money_clusters if c.get("condition_id") != cid
            ]
        self._snapshot.smart_money_clusters.insert(0, cluster)
        self._snapshot.smart_money_clusters = self._snapshot.smart_money_clusters[:30]
        self._enqueue({"type": "cluster", "data": cluster})

    def heartbeat(self) -> None:
        self._enqueue({"type": "heartbeat", "ts": datetime.now(UTC).isoformat()})

    # ---------- subscription plumbing ----------

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers.add(q)
        # Replay recent events so the new client gets context.
        for evt in list(self._recent_events)[-50:]:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                break
        return q

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    def _enqueue(self, event: dict[str, Any]) -> None:
        self._recent_events.append(event)
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
            except Exception:
                dead.append(q)
        for d in dead:
            self._subscribers.discard(d)
