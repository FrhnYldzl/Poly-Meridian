"""AgentStateBroker — in-process pub/sub for the operator dashboard.

The trading pipeline pushes events here; SSE subscribers receive them.
Decoupled from the trading loop so a slow/disconnected dashboard never
blocks order flow.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
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
    # Pipeline activity counters so the UI shows life even when no signals fire.
    pipeline_ticks_total: int = 0
    strategies_evaluated_total: int = 0
    arb_opportunities_total: int = 0   # raw_edge > 0, even if below threshold
    last_book_update_ts: str | None = None
    # Infra connectivity (visible without Railway log access).
    db_ok: bool = False
    cache_ok: bool = False
    sentiment_enabled: bool = False
    onchain_enabled: bool = False
    # News funnel telemetry — shows exactly where the article→signal pipeline
    # drops off (helps diagnose "0 signals" without Railway log access).
    news_ingested_total: int = 0
    news_processed_total: int = 0
    news_signals_emitted_total: int = 0
    news_matcher_mode: str | None = None   # "inmem-vector" | "pgvector" | "keyword"
    scorer_kind: str | None = None         # "claude" | "gemini" | "heuristic"
    # Polymarket category breakdown — counts of active markets per category
    # (Politics, Sports, Crypto, Pop Culture, Business, Science, Tech, …).
    # Lets the operator see what slice of the universe is being watched.
    markets_by_category: dict[str, int] = field(default_factory=dict)
    markets_active_total: int = 0          # full set returned by Gamma sync
    ws_subscribed_total: int = 0           # subset we actually trade on
    # Trade-flow funnel: strategy-signal → aggregator → risk → executed order.
    # Sourced from in-process Prometheus counters; shows EXACTLY where signals
    # are dropping (aggregator conflict / risk reject / etc).
    signals_emitted_total: int = 0         # raw strategy signals
    signals_aggregated_total: int = 0      # passed aggregator (non-conflict)
    risk_accepted_total: int = 0
    risk_rejected_total: int = 0
    orders_submitted_total: int = 0
    # Resolution-aware NAV split (Phase I.0). nav_usd is the *liquidation*
    # NAV — what we'd have if we exited every position right now at last
    # mark. thesis_nav_usd is what NAV would be if every open position
    # reverted to the price we entered at (our model's fair value) — a
    # rough proxy for "expected NAV if we hold to resolution and our edge
    # is real". The gap between the two is unrealized noise.
    liquidation_nav_usd: float = 0.0
    thesis_nav_usd: float = 0.0
    # Ledger genesis NAV (set on Ledger init from PAPER_STARTING_NAV env var
    # in paper mode). Surfaced so the Portfolio page's "vs start" tile is
    # right regardless of bankroll override.
    starting_nav_usd: float = 0.0
    # Diagnostic: last error in broker_refresh_loop, surfaced for debugging
    # silent failures without Railway log access. Empty when healthy.
    last_refresh_error: str | None = None
    # Phase Q.1: per-(strategy, reason) reject counts. Tells the operator
    # EXACTLY which constraint is killing trades. Top-N bucket per strategy
    # surfaced so dashboard panel is bounded.
    strategy_rejects: dict[str, dict[str, int]] = field(default_factory=dict)
    # Phase Q.2a: book population health — how many of the WS-subscribed
    # books have bids/asks/both/neither. If "asks_only" or "bids_only" is
    # the dominant bucket, WS dispatch is missing one side OR bootstrap
    # didn't fill it. Bypasses strategy-level proxies for direct visibility.
    book_health: dict[str, int] = field(default_factory=dict)
    # Slippage drift monitor (Phase K). None when < 10 fills observed yet.
    slippage_summary: dict[str, Any] = field(default_factory=dict)

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
            "pipeline_ticks_total": self.pipeline_ticks_total,
            "strategies_evaluated_total": self.strategies_evaluated_total,
            "arb_opportunities_total": self.arb_opportunities_total,
            "last_book_update_ts": self.last_book_update_ts,
            "db_ok": self.db_ok,
            "cache_ok": self.cache_ok,
            "sentiment_enabled": self.sentiment_enabled,
            "onchain_enabled": self.onchain_enabled,
            "news_ingested_total": self.news_ingested_total,
            "news_processed_total": self.news_processed_total,
            "news_signals_emitted_total": self.news_signals_emitted_total,
            "news_matcher_mode": self.news_matcher_mode,
            "scorer_kind": self.scorer_kind,
            "markets_by_category": self.markets_by_category,
            "markets_active_total": self.markets_active_total,
            "ws_subscribed_total": self.ws_subscribed_total,
            "signals_emitted_total": self.signals_emitted_total,
            "signals_aggregated_total": self.signals_aggregated_total,
            "risk_accepted_total": self.risk_accepted_total,
            "risk_rejected_total": self.risk_rejected_total,
            "orders_submitted_total": self.orders_submitted_total,
            "liquidation_nav_usd": self.liquidation_nav_usd,
            "thesis_nav_usd": self.thesis_nav_usd,
            "starting_nav_usd": self.starting_nav_usd,
            "last_refresh_error": self.last_refresh_error,
            "strategy_rejects": self.strategy_rejects,
            "book_health": self.book_health,
            "slippage_summary": self.slippage_summary,
        }


class AgentStateBroker:
    """Holds the latest snapshot + rolling event log; multiplexes to subscribers."""

    def __init__(self) -> None:
        self._snapshot = Snapshot()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=MAX_RECENT)
        self._started_at = time.time()
        # Session-first hooks — fire once per agent lifetime. Used by the
        # alerts dispatcher to surface "first paper signal / fill" events.
        self._first_signal_fired = False
        self._first_order_fired = False
        self._on_first_signal: Callable[[dict[str, Any]], None] | None = None
        self._on_first_order: Callable[[dict[str, Any]], None] | None = None
        self._on_kill_switch_change: Callable[[bool, str | None], None] | None = None
        # Persistence hooks — fire on EVERY signal/order push (not just first)
        # so DB writers can mirror in-memory state. Errors swallowed silently
        # so persistence failures never block the trading loop.
        self._on_signal_persist: Callable[[dict[str, Any]], None] | None = None
        self._on_order_persist: Callable[[dict[str, Any]], None] | None = None

    def set_persistence_hooks(
        self,
        *,
        signal_hook: Callable[[dict[str, Any]], None] | None = None,
        order_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Register per-event persistence callbacks. Each fires on every
        push_signal / push_order — used by the DB writer to mirror state
        so deploys don't wipe the dashboard."""
        if signal_hook is not None:
            self._on_signal_persist = signal_hook
        if order_hook is not None:
            self._on_order_persist = order_hook

    def seed_signals(self, signals: list[dict[str, Any]]) -> None:
        """Replay historical signals into the snapshot on boot — used by
        the DB backfill so the dashboard isn't blank after Railway restart.
        Does NOT fire the first-signal hook (those are session-first only)."""
        self._snapshot.last_signals = list(signals[:50])

    def seed_orders(self, orders: list[dict[str, Any]]) -> None:
        """Same as seed_signals but for the orders feed."""
        self._snapshot.last_orders = list(orders[:50])

    # ---------- markets directory (compact projection) ----------
    # Used by /api/markets — we DON'T put this in the SSE snapshot stream
    # because the full set is hundreds of rows × ~1KB each = too noisy.
    # Lives on the broker instead, fetched on demand.

    def update_markets_directory(self, markets: list[dict[str, Any]]) -> None:
        """Replace the cached markets directory. Caller is expected to pass
        the same compact projection shape that /api/markets returns."""
        self._markets_directory = list(markets)

    def get_markets_directory(self) -> list[dict[str, Any]]:
        return getattr(self, "_markets_directory", []) or []

    def set_first_signal_hook(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback that fires exactly once — on the first signal
        observed this session. Safe to call before any signals exist."""
        self._on_first_signal = cb

    def set_first_order_hook(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback that fires exactly once — on the first order."""
        self._on_first_order = cb

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
        thesis_nav_usd: float | Decimal | None = None,
    ) -> None:
        self._snapshot.ts = datetime.now(UTC)
        self._snapshot.nav_usd = float(nav_usd)
        # nav_usd is the liquidation NAV — make it explicit too.
        self._snapshot.liquidation_nav_usd = float(nav_usd)
        if thesis_nav_usd is not None:
            self._snapshot.thesis_nav_usd = float(thesis_nav_usd)
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
            cb = self._on_kill_switch_change
            if cb is not None:
                try:
                    cb(engaged, reason)
                except Exception:
                    pass

    def set_kill_switch_hook(
        self, cb: Callable[[bool, str | None], None]
    ) -> None:
        """Register a callback that fires on every kill-switch state change."""
        self._on_kill_switch_change = cb

    def update_mode(self, mode: str) -> None:
        self._snapshot.mode = mode

    def update_strategies(self, names: list[str]) -> None:
        self._snapshot.strategies_enabled = names

    def update_markets_watched(self, count: int) -> None:
        self._snapshot.markets_watched = count

    def update_market_coverage(
        self,
        *,
        markets_by_category: dict[str, int],
        markets_active_total: int,
        ws_subscribed_total: int,
    ) -> None:
        """Push category breakdown + WS-subscription split from gamma_sync."""
        self._snapshot.markets_by_category = dict(markets_by_category)
        self._snapshot.markets_active_total = markets_active_total
        self._snapshot.ws_subscribed_total = ws_subscribed_total

    def update_pipeline_funnel(
        self,
        *,
        signals_emitted: int | None = None,
        signals_aggregated: int | None = None,
        risk_accepted: int | None = None,
        risk_rejected: int | None = None,
        orders_submitted: int | None = None,
    ) -> None:
        """Push trade-flow funnel counters so the operator can see EXACTLY
        where signals drop between strategy and executor."""
        if signals_emitted is not None:
            self._snapshot.signals_emitted_total = signals_emitted
        if signals_aggregated is not None:
            self._snapshot.signals_aggregated_total = signals_aggregated
        if risk_accepted is not None:
            self._snapshot.risk_accepted_total = risk_accepted
        if risk_rejected is not None:
            self._snapshot.risk_rejected_total = risk_rejected
        if orders_submitted is not None:
            self._snapshot.orders_submitted_total = orders_submitted

    def update_infra(
        self,
        *,
        db_ok: bool | None = None,
        cache_ok: bool | None = None,
        sentiment_enabled: bool | None = None,
        onchain_enabled: bool | None = None,
    ) -> None:
        if db_ok is not None:
            self._snapshot.db_ok = db_ok
        if cache_ok is not None:
            self._snapshot.cache_ok = cache_ok
        if sentiment_enabled is not None:
            self._snapshot.sentiment_enabled = sentiment_enabled
        if onchain_enabled is not None:
            self._snapshot.onchain_enabled = onchain_enabled

    def update_news_stats(
        self,
        *,
        ingested_total: int | None = None,
        processed_total: int | None = None,
        signals_emitted_total: int | None = None,
        matcher_mode: str | None = None,
        scorer_kind: str | None = None,
    ) -> None:
        """Push pipeline funnel telemetry from the news loops. Each param is
        optional so callers can update only what they have."""
        if ingested_total is not None:
            self._snapshot.news_ingested_total = ingested_total
        if processed_total is not None:
            self._snapshot.news_processed_total = processed_total
        if signals_emitted_total is not None:
            self._snapshot.news_signals_emitted_total = signals_emitted_total
        if matcher_mode is not None:
            self._snapshot.news_matcher_mode = matcher_mode
        if scorer_kind is not None:
            self._snapshot.scorer_kind = scorer_kind

    def increment_pipeline_tick(self, *, strategies_evaluated: int, arb_seen: int = 0) -> None:
        self._snapshot.pipeline_ticks_total += 1
        self._snapshot.strategies_evaluated_total += strategies_evaluated
        self._snapshot.arb_opportunities_total += arb_seen
        self._snapshot.last_book_update_ts = datetime.now(UTC).isoformat()

    def push_signal(self, signal: dict[str, Any]) -> None:
        signal = {**signal, "ts": signal.get("ts") or datetime.now(UTC).isoformat()}
        self._snapshot.last_signals.insert(0, signal)
        self._snapshot.last_signals = self._snapshot.last_signals[:50]
        self._enqueue({"type": "signal", "data": signal})
        # Persistence — fire EVERY push so DB mirrors in-memory state.
        persist = self._on_signal_persist
        if persist is not None:
            try:
                persist(signal)
            except Exception:
                pass
        if not self._first_signal_fired:
            self._first_signal_fired = True
            cb = self._on_first_signal
            if cb is not None:
                try:
                    cb(signal)
                except Exception:
                    pass

    def push_order(self, order: dict[str, Any]) -> None:
        order = {**order, "ts": order.get("ts") or datetime.now(UTC).isoformat()}
        self._snapshot.last_orders.insert(0, order)
        self._snapshot.last_orders = self._snapshot.last_orders[:50]
        self._enqueue({"type": "order", "data": order})
        # Persistence — fire EVERY push so order transitions land in DB.
        persist = self._on_order_persist
        if persist is not None:
            try:
                persist(order)
            except Exception:
                pass
        if not self._first_order_fired:
            self._first_order_fired = True
            cb = self._on_first_order
            if cb is not None:
                try:
                    cb(order)
                except Exception:
                    pass

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

    def emit_snapshot(self) -> None:
        """Push the current snapshot to all subscribers as a full refresh.

        The UI's SSE reducer handles `type: snapshot` by replacing state — so
        calling this from the broker_refresh_loop every 5s gives the dashboard
        a live view of NAV / ticks / markets without REST re-fetching.
        """
        self._enqueue({"type": "snapshot", "data": self.snapshot.asdict()})

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
