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
    ) -> None:
        self.arbitrage = arbitrage
        self.sentiment = sentiment
        self.smart_money = smart_money
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
        self.executor.attach_book(token_id, book)

    def register_market(self, market: Market) -> None:
        self.arbitrage.register_pair(
            market.condition_id, market.yes_token_id, market.no_token_id
        )
        if market.category:
            self.token_to_category[market.yes_token_id] = market.category
            self.token_to_category[market.no_token_id] = market.category

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

        # Evaluate enabled strategies in parallel-friendly sequence.
        signals = []
        for strat in (self.arbitrage, self.sentiment, self.smart_money):
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
        return order

    async def on_fill(self, order: Order) -> None:
        if order.avg_fill_price is None or order.filled_size <= 0:
            return
        self.ledger.apply_fill(
            ts=order.ts_filled or datetime.now(UTC),
            order=order,
            filled_qty=order.filled_size,
            fill_price=order.avg_fill_price,
            fee=Decimal(0),
        )

    def context_metrics(self) -> dict[str, Any]:
        return {
            "nav_usd": float(snapshot(self.ledger).nav_usd),
            "cash_usd": float(self.ledger.cash),
            "open_positions": len(self.ledger.positions()),
            "kill_switch_engaged": self.risk.is_kill_switch_engaged(),
        }
