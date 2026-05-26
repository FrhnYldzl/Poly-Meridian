"""ArbitrageStrategy — single-market complete-set arbitrage. §14.1.

Polymarket invariant: for a binary market, YES_price + NO_price ≈ $1.00.
When the sum of best asks falls below 1 (after fees), buying both legs is
a risk-free profit at resolution. When the sum of best bids rises above 1
(after fees), selling both legs is a risk-free profit.

Phase 2 covers single-market only. Cross-market / event-set arbitrage
(§14.1 bullet 2) lands when we have event-grouped market data; that's a
Phase 3 deliverable.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, Features, Market, StrategySignal
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.base import BaseStrategy

log = structlog.get_logger("poly_meridian.strategies.arbitrage")


class ArbitrageStrategy(BaseStrategy):
    """Detects when YES_ask + NO_ask < 1 - threshold (buy both legs).

    Configuration knobs (loaded from `config/strategies/arbitrage.yaml`):
      - imbalance_threshold:    minimum gap from 1.00 to consider (default 0.015)
      - conviction_default:     conviction emitted for math-certain arb (0.95)
      - min_edge_after_fees_bps: edge must exceed this in basis points
      - max_size_pct:           hard cap on size as fraction of bankroll (0.04)
      - default_taker_fee_bps:  worst-case taker fee assumed (e.g. 180 = 1.80%)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(name="arbitrage", config=config, enabled=config.get("enabled", True))
        self.imbalance_threshold = float(config.get("imbalance_threshold", 0.015))
        self.conviction_default = float(config.get("conviction_default", 0.95))
        self.min_edge_after_fees_bps = float(config.get("min_edge_after_fees_bps", 30))
        self.max_size_pct = float(config.get("max_size_pct", 0.04))
        self.default_taker_fee_bps = float(config.get("default_taker_fee_bps", 180))
        # Per-token book references — injected by main loop via `attach_book()`.
        self._books: dict[str, LocalBook] = {}
        # Per-condition lookup for the *other* token (YES↔NO).
        self._pair: dict[str, tuple[str, str]] = {}  # condition_id -> (yes_token, no_token)

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def register_pair(self, condition_id: str, yes_token: str, no_token: str) -> None:
        self._pair[condition_id] = (yes_token, no_token)

    async def evaluate(
        self, market: Market, features: Features
    ) -> StrategySignal | None:
        if not self.enabled:
            return None

        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        yes_book = self._books.get(market.yes_token_id)
        no_book = self._books.get(market.no_token_id)
        if yes_book is None or no_book is None:
            PM_STRATEGY_REJECT.labels(strategy="arbitrage", reason="no_book").inc()
            return None

        yes_ask = yes_book.best_ask()
        no_ask = no_book.best_ask()
        if yes_ask is None or no_ask is None:
            PM_STRATEGY_REJECT.labels(strategy="arbitrage", reason="no_best_ask").inc()
            return None

        yes_price, yes_size = yes_ask
        no_price, no_size = no_ask
        total_ask = float(yes_price + no_price)
        raw_edge = 1.0 - total_ask  # positive when arb exists

        if raw_edge < self.imbalance_threshold:
            PM_STRATEGY_REJECT.labels(strategy="arbitrage", reason="no_arb_gap").inc()
            return None

        # Net of worst-case fees on both legs (Phase 2 conservative).
        fee_bps_both_legs = self.default_taker_fee_bps * 2
        net_edge_bps = raw_edge * 10_000 - fee_bps_both_legs
        if net_edge_bps < self.min_edge_after_fees_bps:
            PM_STRATEGY_REJECT.labels(strategy="arbitrage", reason="edge_eaten_by_fees").inc()
            return None

        # Choose the cheaper leg (smaller-priced ask) to size against — the
        # YES side here. Both legs must be filled for the arb to realize.
        # Phase 2 emits a single-leg signal; the *router* completes the pair
        # via a second signal on the partner token. (Full set-arb wiring
        # lives in the aggregator/router pair, not in the strategy.)
        chosen_token = market.yes_token_id
        chosen_price = float(yes_price)
        chosen_action = Action.BUY_YES
        partner_token = market.no_token_id
        depth_min_units = float(min(yes_size, no_size))

        rationale: dict[str, Any] = {
            "yes_ask": float(yes_price),
            "no_ask": float(no_price),
            "total_ask": total_ask,
            "raw_edge": raw_edge,
            "net_edge_bps": net_edge_bps,
            "depth_min_units": depth_min_units,
            "partner_token": partner_token,
        }

        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=self.name,
            condition_id=market.condition_id,
            token_id=chosen_token,
            edge=raw_edge,
            conviction=self.conviction_default,
            suggested_action=chosen_action,
            rationale=rationale,
        )

    def capacity_estimate(self) -> float:
        """Rough daily USD capacity. Bounded by typical book depth.

        Phase 2 conservative: ~$5K/day. Will be revised once we have live
        fill data to back-fit.
        """
        return 5_000.0

    @staticmethod
    def proposed_price_from_signal(rationale: dict[str, Any]) -> Decimal:
        """Helper for the aggregator: pull the buy-leg ask price."""
        return Decimal(str(rationale.get("yes_ask", 0.5)))

    @staticmethod
    def proposed_size_pct(
        rationale: dict[str, Any],
        bankroll_usd: Decimal,
        max_size_pct: float,
    ) -> float:
        """Cap by both book depth and configured max_size_pct."""
        depth_units = float(rationale.get("depth_min_units", 0.0))
        price = float(rationale.get("yes_ask", 0.5))
        if depth_units <= 0 or price <= 0:
            return 0.0
        depth_usd = depth_units * price
        depth_pct = depth_usd / float(bankroll_usd) if bankroll_usd > 0 else 0.0
        return float(min(max_size_pct, depth_pct))
