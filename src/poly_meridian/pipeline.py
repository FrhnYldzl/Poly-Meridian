"""Trading pipeline — orchestrates the per-tick decision flow.

ingest → features → strategy → aggregator → risk → executor → portfolio

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
from poly_meridian.strategies import ArbitrageStrategy, SignalAggregator

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
    """Holds the wired strategy → aggregator → risk → executor chain."""

    def __init__(
        self,
        *,
        strategy: ArbitrageStrategy,
        aggregator: SignalAggregator,
        risk: DefaultRiskPolicy,
        executor: PaperExecutor,
        ledger: Ledger,
        token_to_category: dict[str, str] | None = None,
    ) -> None:
        self.strategy = strategy
        self.aggregator = aggregator
        self.risk = risk
        self.router = OrderRouter(executor)
        self.executor = executor
        self.ledger = ledger
        self.token_to_category: dict[str, str] = token_to_category or {}
        self._books: dict[str, LocalBook] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book
        self.strategy.attach_book(token_id, book)
        self.executor.attach_book(token_id, book)

    def register_market(self, market: Market) -> None:
        self.strategy.register_pair(
            market.condition_id, market.yes_token_id, market.no_token_id
        )
        if market.category:
            self.token_to_category[market.yes_token_id] = market.category
            self.token_to_category[market.no_token_id] = market.category

    async def tick(self, market: Market) -> Order | None:
        """One pass through the full pipeline for a single market.

        Returns the Order submitted (paper) if a trade was approved, else None.
        """
        now = datetime.now(UTC)
        book = self._books.get(market.yes_token_id)

        feats = compute_features(
            token_id=market.yes_token_id,
            now=now,
            book=book,
            end_date=market.end_date_iso,
        )

        sig = await self.strategy.evaluate(market, feats)
        if sig is None:
            return None
        PM_SIGNAL_EMITTED.labels(strategy=self.strategy.name).inc()

        # Mark all positions BEFORE the risk snapshot — fresh MTM on every tick.
        mark_all(self.ledger, self._books, ts=now)

        agg = self.aggregator.aggregate(
            [sig],
            market=market,
            bankroll_usd=self.ledger.cash + Decimal("1"),  # avoid div-by-zero
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
        """Wire this as the PaperExecutor's `on_fill` callback so the ledger
        stays in sync without the pipeline polling."""
        if order.avg_fill_price is None or order.filled_size <= 0:
            return
        # Phase 2 has no per-category fee map — assume 0 in paper.
        # Real fee accounting (§2.2 table) lands in Phase 4.
        self.ledger.apply_fill(
            ts=order.ts_filled or datetime.now(UTC),
            order=order,
            filled_qty=order.filled_size,
            fill_price=order.avg_fill_price,
            fee=Decimal(0),
        )

    def context_metrics(self) -> dict[str, Any]:
        """Surface NAV + exposure for dashboards / structured logging."""
        return {
            "nav_usd": float(snapshot(self.ledger).nav_usd),
            "cash_usd": float(self.ledger.cash),
            "open_positions": len(self.ledger.positions()),
            "kill_switch_engaged": self.risk.is_kill_switch_engaged(),
        }
