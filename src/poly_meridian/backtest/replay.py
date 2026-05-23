"""Historical replay engine. See MASTER_SPEC §18.

Replays `orderbook_snapshots` + `trades` + `news_signals` in chronological
order, hydrates a synthetic Pipeline (with PaperExecutor against the
recorded book), and produces equity curve + trade list for metrics.

Note: this is a *deterministic* replay. Real-world latency / slippage are
simulated by the standing PaperExecutor (which already includes the fee
schedule and book-walk model from Phase 2 + Phase 4 fees).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, Market, Order, Side
from poly_meridian.execution import PaperExecutor
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.portfolio import Ledger, snapshot
from poly_meridian.risk import DefaultRiskPolicy
from poly_meridian.strategies import SignalAggregator
from poly_meridian.strategies.base import BaseStrategy

log = structlog.get_logger("poly_meridian.backtest.replay")


@dataclass
class ReplayConfig:
    starting_nav_usd: Decimal = Decimal("100000")
    tick_interval_sec: int = 60         # how often to evaluate strategies
    log_every_n: int = 1000


@dataclass
class ReplayResult:
    equity_curve: list[float] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    final_nav: float = 0.0

    @property
    def duration_sec(self) -> float:
        if self.start_ts is None or self.end_ts is None:
            return 0.0
        return (self.end_ts - self.start_ts).total_seconds()


@dataclass
class HistoricalTick:
    """One discrete observation: a book snapshot for `token_id` at `ts`."""

    ts: datetime
    token_id: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


@dataclass
class HistoricalDataset:
    """In-memory replay input. Production wiring loads from Timescale; this
    abstraction lets us unit-test the engine with synthetic data."""

    markets: list[Market]
    ticks: list[HistoricalTick]
    news_signals: list[dict[str, Any]] = field(default_factory=list)
    smart_money_state: dict[str, Any] = field(default_factory=dict)


class Replayer:
    """Drives a strategy + risk + executor pipeline over historical data."""

    def __init__(
        self,
        *,
        dataset: HistoricalDataset,
        strategies: list[BaseStrategy],
        aggregator: SignalAggregator,
        risk: DefaultRiskPolicy,
        config: ReplayConfig | None = None,
    ) -> None:
        self.dataset = dataset
        self.strategies = strategies
        self.aggregator = aggregator
        self.risk = risk
        self.config = config or ReplayConfig()

        self.ledger = Ledger(starting_cash_usd=self.config.starting_nav_usd)
        self.executor = PaperExecutor(on_fill=self._on_fill)  # type: ignore[arg-type]
        self.books: dict[str, LocalBook] = {}
        self._pending_pnls: dict[str, list[float]] = {}
        for m in dataset.markets:
            self.executor.attach_category(m.yes_token_id, m.category or None)
            self.executor.attach_category(m.no_token_id, m.category or None)
            for tid in (m.yes_token_id, m.no_token_id):
                self.books[tid] = LocalBook(token_id=tid)
                self.executor.attach_book(tid, self.books[tid])
                for strat in strategies:
                    if hasattr(strat, "attach_book"):
                        strat.attach_book(tid, self.books[tid])

    async def run(self) -> ReplayResult:
        result = ReplayResult()
        if not self.dataset.ticks:
            return result

        sorted_ticks = sorted(self.dataset.ticks, key=lambda t: t.ts)
        result.start_ts = sorted_ticks[0].ts
        result.end_ts = sorted_ticks[-1].ts

        # We evaluate strategies once per `tick_interval_sec` of replay time.
        next_eval_at = result.start_ts + timedelta(seconds=self.config.tick_interval_sec)
        market_by_token: dict[str, Market] = {}
        for m in self.dataset.markets:
            market_by_token[m.yes_token_id] = m
            market_by_token[m.no_token_id] = m

        for i, tick in enumerate(sorted_ticks):
            self._apply_tick(tick)

            if tick.ts >= next_eval_at:
                for m in self.dataset.markets:
                    await self._evaluate_once(m, tick.ts)
                await self.executor.reconcile()
                snap = snapshot(self.ledger)
                result.equity_curve.append(float(snap.nav_usd))
                next_eval_at = tick.ts + timedelta(seconds=self.config.tick_interval_sec)

            if i % self.config.log_every_n == 0:
                log.debug("replay.progress", i=i, ts=tick.ts.isoformat())

        # Final snapshot.
        snap = snapshot(self.ledger)
        result.final_nav = float(snap.nav_usd)
        result.trade_pnls = list(self._all_pending_pnls())
        result.orders = list(self.executor._orders.values())  # type: ignore[attr-defined]
        log.info(
            "replay.done",
            ticks=len(sorted_ticks),
            equity_points=len(result.equity_curve),
            final_nav=result.final_nav,
        )
        return result

    # ---------- internals ----------

    def _apply_tick(self, tick: HistoricalTick) -> None:
        book = self.books.get(tick.token_id)
        if book is None:
            book = LocalBook(token_id=tick.token_id)
            self.books[tick.token_id] = book
            self.executor.attach_book(tick.token_id, book)
            for strat in self.strategies:
                if hasattr(strat, "attach_book"):
                    strat.attach_book(tick.token_id, book)
        book.apply_snapshot({
            "bids": [{"price": str(p), "size": str(s)} for p, s in tick.bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in tick.asks],
        })

    async def _evaluate_once(self, market: Market, ts: datetime) -> None:
        from poly_meridian.features import compute_features  # local to avoid circular

        feats = compute_features(
            token_id=market.yes_token_id,
            now=ts,
            book=self.books.get(market.yes_token_id),
            end_date=market.end_date_iso,
        )

        signals = []
        for strat in self.strategies:
            if not getattr(strat, "enabled", False):
                continue
            try:
                s = await strat.evaluate(market, feats)
            except Exception as exc:
                log.warning("replay.strategy_error", strategy=strat.name, error=str(exc))
                continue
            if s is not None:
                signals.append(s)

        if not signals:
            return

        agg = self.aggregator.aggregate(
            signals,
            market=market,
            bankroll_usd=self.ledger.cash + Decimal("1"),
        )
        if agg is None:
            return

        port = snapshot(self.ledger)
        from poly_meridian.risk import RiskDecision

        decision = self.risk.evaluate(agg, port)
        if decision == RiskDecision.REJECT:
            return
        trade = self.risk.size(agg, port)
        if trade is None:
            return
        await self.executor.submit(trade)

    async def _on_fill(
        self,
        order: Order,
        filled_qty: Decimal | None = None,
        fill_price: Decimal | None = None,
        fee: Decimal | None = None,
    ) -> None:
        if filled_qty is None or fill_price is None:
            if order.avg_fill_price is None or order.filled_size <= 0:
                return
            filled_qty = order.filled_size
            fill_price = order.avg_fill_price
            fee = fee or Decimal(0)
        entry = self.ledger.apply_fill(
            ts=order.ts_filled or datetime.now(UTC),
            order=order,
            filled_qty=filled_qty,
            fill_price=fill_price,
            fee=fee or Decimal(0),
        )
        # Realized P&L is captured for SELL legs by Ledger.apply_fill;
        # we pick it up at finalize. For BUYs we record the open cost only.
        if order.side == Side.SELL:
            self._pending_pnls.setdefault(order.token_id, []).append(float(entry.notional - entry.fee))

    def _all_pending_pnls(self) -> list[float]:
        out: list[float] = []
        for lst in self._pending_pnls.values():
            out.extend(lst)
        # For positions still open at end, mark-to-market unrealized.
        for pos in self.ledger.positions():
            out.append(float(pos.unrealized_pnl()))
        return out


__all__ = ["HistoricalDataset", "HistoricalTick", "ReplayConfig", "ReplayResult", "Replayer"]
