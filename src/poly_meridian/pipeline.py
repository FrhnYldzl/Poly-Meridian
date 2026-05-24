"""Trading pipeline — orchestrates the per-tick decision flow.

ingest → features → strategies → aggregator → risk → executor → portfolio

This module is the single place that wires strategies, the risk gate, and
the executor together. Main loop drives it on a clock or on book updates.

Critical invariant (MASTER_SPEC, immutable rule #3):
  **Every trade decision passes through `RiskPolicy.evaluate()`. No bypass.**
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from prometheus_client import Counter

from poly_meridian.domain import Market, Order
from poly_meridian.execution import OrderRouter, PaperExecutor
from poly_meridian.features import compute_features
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.portfolio import Ledger, mark_all, snapshot
from poly_meridian.risk import DefaultRiskPolicy, RiskDecision
from poly_meridian.strategies import (
    ArbitrageStrategy,
    SentimentStrategy,
    SignalAggregator,
    SmartMoneyStrategy,
    StatQuantStrategy,
)

log = structlog.get_logger("poly_meridian.pipeline")

PM_SIGNAL_EMITTED = Counter(
    "pm_signal_emitted_total",
    "strategy signals emitted",
    ["strategy"],
)
PM_SIGNAL_AGGREGATED = Counter(
    "pm_signal_aggregated_total",
    "aggregated signals reaching risk gate",
)
PM_RISK_DECISION = Counter(
    "pm_risk_decision_total",
    "risk gate decisions",
    ["decision"],
)
PM_ORDER_SUBMITTED = Counter(
    "pm_order_submitted_total",
    "orders submitted to executor",
    ["side", "mode"],
)


class Pipeline:
    """Holds the wired strategies → aggregator → risk → executor chain."""

    def __init__(
        self,
        *,
        arbitrage: ArbitrageStrategy,
        sentiment: SentimentStrategy | None,
        smart_money: SmartMoneyStrategy | None,
        aggregator: SignalAggregator,
        risk: DefaultRiskPolicy,
        executor: PaperExecutor,
        ledger: Ledger,
        token_to_category: dict[str, str] | None = None,
        stat_quant: StatQuantStrategy | None = None,
    ) -> None:
        self.arbitrage = arbitrage
        self.sentiment = sentiment
        self.smart_money = smart_money
        self.stat_quant = stat_quant
        self.aggregator = aggregator
        self.risk = risk
        self.router = OrderRouter(executor)
        self.executor = executor
        self.ledger = ledger
        self.token_to_category: dict[str, str] = token_to_category or {}
        self._books: dict[str, LocalBook] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book
        self.arbitrage.attach_book(token_id, book)
        if self.sentiment is not None:
            self.sentiment.attach_book(token_id, book)
        if self.smart_money is not None:
            self.smart_money.attach_book(token_id, book)
        if self.stat_quant is not None:
            self.stat_quant.attach_book(token_id, book)
        self.executor.attach_book(token_id, book)
        # Propagate category for fee schedule.
        cat = self.token_to_category.get(token_id)
        if cat is not None:
            self.executor.attach_category(token_id, cat)

    def register_market(self, market: Market) -> None:
        self.arbitrage.register_pair(
            market.condition_id, market.yes_token_id, market.no_token_id
        )
        if market.category:
            self.token_to_category[market.yes_token_id] = market.category
            self.token_to_category[market.no_token_id] = market.category
            self.executor.attach_category(market.yes_token_id, market.category)
            self.executor.attach_category(market.no_token_id, market.category)

    def attach_news_signals(self, condition_id: str, signals: list[dict[str, Any]]) -> None:
        if self.sentiment is not None:
            self.sentiment.attach_recent_signals(condition_id, signals)

    def attach_cluster_state(self, state: Any) -> None:
        if self.smart_money is not None:
            self.smart_money.attach_cluster_state(state)

    async def tick(self, market: Market) -> Order | None:
        """One pass through the full pipeline for a single market."""
        now = datetime.now(UTC)
        book = self._books.get(market.yes_token_id)

        feats = compute_features(
            token_id=market.yes_token_id,
            now=now,
            book=book,
            end_date=market.end_date_iso,
        )

        # Push fresh mid prices into StatQuant's rolling windows. Done BEFORE
        # evaluate() so the strategy sees the current tick in its history.
        if self.stat_quant is not None and self.stat_quant.enabled:
            for tid in (market.yes_token_id, market.no_token_id):
                b = self._books.get(tid)
                if b is None:
                    continue
                mid = b.mid()
                if mid is not None:
                    self.stat_quant.push_price(tid, float(mid))

        # Evaluate enabled strategies in parallel-friendly sequence.
        signals = []
        for strat in (self.arbitrage, self.sentiment, self.smart_money, self.stat_quant):
            if strat is None or not getattr(strat, "enabled", False):
                continue
            try:
                s = await strat.evaluate(market, feats)
            except Exception as exc:
                log.warning("pipeline.strategy_error", strategy=strat.name, error=str(exc))
                continue
            if s is not None:
                signals.append(s)
                PM_SIGNAL_EMITTED.labels(strategy=strat.name).inc()
                # Surface the signal on the operator dashboard with full
                # rationale so the user can SEE what fired and WHY.
                broker = getattr(self, "broker", None)
                if broker is not None:
                    try:
                        broker.push_signal({
                            "ts": s.ts.isoformat(),
                            "strategy": s.strategy,
                            "condition_id": s.condition_id,
                            "token_id": s.token_id,
                            "edge": s.edge,
                            "conviction": s.conviction,
                            # Use .value so the UI gets clean "BUY_YES"
                            # rather than "Action.BUY_YES".
                            "suggested_action": s.suggested_action.value,
                            "rationale": s.rationale,
                            "market_question": market.question,
                        })
                    except Exception:
                        pass

        if not signals:
            return None

        # Mark all positions BEFORE the risk snapshot — fresh MTM on every tick.
        mark_all(self.ledger, self._books, ts=now)

        agg = self.aggregator.aggregate(
            signals,
            market=market,
            bankroll_usd=self.ledger.cash + Decimal("1"),
        )
        if agg is None:
            return None
        PM_SIGNAL_AGGREGATED.inc()

        port = snapshot(self.ledger, token_to_category=self.token_to_category, ts=now)
        decision = self.risk.evaluate(agg, port)
        PM_RISK_DECISION.labels(decision=str(decision)).inc()

        if decision == RiskDecision.REJECT:
            return None

        trade = self.risk.size(agg, port)
        if trade is None:
            return None

        order = await self.router.route(trade)
        PM_ORDER_SUBMITTED.labels(side=str(order.side), mode=str(order.mode)).inc()
        # Surface the order on the dashboard. Use contributors from the agg
        # so the UI shows the *combined* strategy attribution rather than just
        # the last leg name.
        broker = getattr(self, "broker", None)
        if broker is not None:
            try:
                broker.push_order({
                    "ts": (order.ts_filled or order.ts_created).isoformat(),
                    "order_id": order.order_id,
                    "strategy": order.strategy,
                    "contributors": list(agg.contributors),
                    "condition_id": agg.condition_id,
                    "token_id": order.token_id,
                    # .value strips the "Side." / "OrderStatus." enum prefix.
                    "side": order.side.value,
                    "status": order.status.value,
                    "price": float(order.price) if order.price is not None else None,
                    "size": float(order.size),
                    "filled_size": float(order.filled_size),
                    "avg_fill_price": float(order.avg_fill_price)
                        if order.avg_fill_price is not None else None,
                    "mode": order.mode.value,
                    "edge": agg.edge,
                    "conviction": agg.conviction,
                    "size_pct": agg.size_pct,
                    "market_question": market.question,
                })
            except Exception:
                pass
        return order

    async def on_fill(
        self,
        order: Order,
        filled_qty: Decimal | None = None,
        fill_price: Decimal | None = None,
        fee: Decimal | None = None,
    ) -> None:
        # Fee-aware path (PaperExecutor passes filled qty/price/fee per fill).
        if filled_qty is None or fill_price is None:
            # Backwards-compat: caller didn't pass per-fill detail.
            if order.avg_fill_price is None or order.filled_size <= 0:
                return
            filled_qty = order.filled_size
            fill_price = order.avg_fill_price
            fee = fee or Decimal(0)
        self.ledger.apply_fill(
            ts=order.ts_filled or datetime.now(UTC),
            order=order,
            filled_qty=filled_qty,
            fill_price=fill_price,
            fee=fee or Decimal(0),
        )

    def context_metrics(self) -> dict[str, Any]:
        return {
            "nav_usd": float(snapshot(self.ledger).nav_usd),
            "cash_usd": float(self.ledger.cash),
            "open_positions": len(self.ledger.positions()),
            "kill_switch_engaged": self.risk.is_kill_switch_engaged(),
        }
