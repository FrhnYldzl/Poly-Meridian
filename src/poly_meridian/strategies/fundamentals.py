"""FundamentalsStrategy — dispatches by category to a CategoryResolver. §14.5.

Conviction = |our_p − market_p| (absolute edge size), bounded [0, 1].
Direction = YES if our_p > market_p, NO otherwise.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, Features, Market, StrategySignal
from poly_meridian.fundamentals import (
    CategoryResolver,
    CryptoResolver,
    FundamentalsContext,
    MacroResolver,
    PoliticsResolver,
    SportsResolver,
)
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.base import BaseStrategy

log = structlog.get_logger("poly_meridian.strategies.fundamentals")


class FundamentalsStrategy(BaseStrategy):
    """Configuration ([`config/strategies/fundamentals.yaml`](config/strategies/fundamentals.yaml)):

      enabled
      min_edge: 0.05            # |our_p - market_p| must exceed this
      min_confidence: 0.5       # resolver confidence floor
      max_size_pct: 0.025
      categories:               # which resolvers to use
        politics: { enabled: true, ... }
        sports:   { enabled: true, ... }
        crypto:   { enabled: true, ... }
        macro:    { enabled: false, ... }
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        resolvers: dict[str, CategoryResolver] | None = None,
    ) -> None:
        super().__init__(name="fundamentals", config=config, enabled=config.get("enabled", False))
        self.min_edge = float(config.get("min_edge", 0.05))
        self.min_confidence = float(config.get("min_confidence", 0.5))
        self.max_size_pct = float(config.get("max_size_pct", 0.025))

        cats = config.get("categories", {}) or {}
        self._enabled_categories = {
            k for k, v in cats.items() if isinstance(v, dict) and v.get("enabled", True)
        }
        if not self._enabled_categories:
            self._enabled_categories = {"Politics", "Sports", "Crypto", "Macro"}

        if resolvers is not None:
            self._resolvers = resolvers
        else:
            self._resolvers = {
                "Politics": PoliticsResolver(),
                "Sports": SportsResolver(),
                "Crypto": CryptoResolver(),
                "Macro": MacroResolver(),
            }

        self._books: dict[str, LocalBook] = {}
        self._context = FundamentalsContext()

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def update_context(self, **kwargs: Any) -> None:
        """Patch context fields. Caller owns merge semantics."""
        for k, v in kwargs.items():
            setattr(self._context, k, v)

    @property
    def context(self) -> FundamentalsContext:
        return self._context

    async def evaluate(
        self, market: Market, features: Features
    ) -> StrategySignal | None:
        if not self.enabled:
            return None
        category = market.category or "Uncategorized"
        if category not in self._enabled_categories and category not in self._resolvers:
            return None
        resolver = self._resolvers.get(category)
        if resolver is None:
            return None

        # Refresh the context's `now` so resolvers see consistent time.
        self._context.now = datetime.now(UTC)
        try:
            est = resolver.resolve(market, self._context)
        except Exception as exc:
            log.warning(
                "fundamentals.resolver_error",
                category=category,
                condition_id=market.condition_id,
                error=str(exc),
            )
            return None
        if est is None:
            return None
        if est.confidence < self.min_confidence:
            return None

        yes_book = self._books.get(market.yes_token_id)
        if yes_book is None:
            return None
        best_ask_yes = yes_book.best_ask()
        if best_ask_yes is None:
            return None
        market_p = float(best_ask_yes[0])

        edge = est.p_yes - market_p
        if abs(edge) < self.min_edge:
            return None

        if edge > 0:
            action = Action.BUY_YES
            token_id = market.yes_token_id
        else:
            action = Action.BUY_NO
            token_id = market.no_token_id

        book = self._books.get(token_id)
        if book is None:
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            return None
        price, _ = best_ask
        conviction = min(1.0, abs(edge) * 2.0 * est.confidence)

        rationale: dict[str, Any] = {
            "category": category,
            "our_p_yes": est.p_yes,
            "market_p_yes": market_p,
            "resolver_confidence": est.confidence,
            "best_ask": float(price),
            "max_size_pct": self.max_size_pct,
            **est.rationale,
        }
        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=self.name,
            condition_id=market.condition_id,
            token_id=token_id,
            edge=abs(edge),
            conviction=conviction,
            suggested_action=action,
            rationale=rationale,
        )

    def capacity_estimate(self) -> float:
        return 3_500.0

    @staticmethod
    def proposed_price_from_signal(rationale: dict[str, Any]) -> Decimal:
        return Decimal(str(rationale.get("best_ask", 0.5)))

    @staticmethod
    def proposed_size_pct(
        rationale: dict[str, Any],
        bankroll_usd: Decimal,
        max_size_pct: float,
    ) -> float:
        cap = float(rationale.get("max_size_pct", max_size_pct))
        confidence = float(rationale.get("resolver_confidence", 0.5))
        return float(min(max_size_pct, cap * confidence))
