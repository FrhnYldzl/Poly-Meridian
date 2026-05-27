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
    LLMResolver,
    MacroResolver,
    PoliticsResolver,
    SportsResolver,
)
from poly_meridian.fundamentals.default import DefaultResolver
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
        llm_resolver: LLMResolver | None = None,
    ) -> None:
        # Phase R.3: enable by default now that LLMResolver fills the
        # previously-empty data path. Operator can still disable via
        # config/strategies/fundamentals.yaml: { enabled: false }.
        super().__init__(name="fundamentals", config=config, enabled=config.get("enabled", True))
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
        # Legacy DefaultResolver (Phase N.7 disabled it). Kept around as
        # a no-op fallback for code paths that still reference it.
        self._default_resolver = DefaultResolver()

        # Phase R — LLMResolver becomes the *real* universal fallback.
        # Whenever a category-specific resolver returns None (which is
        # nearly always today since poll / Elo / funding feeds aren't
        # wired), the LLM gets the question + GDELT news summary +
        # smart-money flow direction and returns its own probability.
        # When ANTHROPIC_API_KEY is missing the resolver simply returns
        # None, and FundamentalsStrategy bails the same way as before.
        try:
            from poly_meridian.settings import get_settings
            settings = get_settings()
            self._llm_enabled = bool(
                getattr(settings, "llm_resolver_enabled", True)
                and settings.anthropic_api_key.get_secret_value()
            )
        except Exception:
            self._llm_enabled = False
        self._llm_resolver = llm_resolver if llm_resolver is not None else (
            LLMResolver() if self._llm_enabled else None
        )

        self._books: dict[str, LocalBook] = {}
        self._context = FundamentalsContext()
        # Phase R.5 — per-market news summary + smart-money flow cache.
        # Pipeline pushes these via attach_news_summary / attach_smart_money
        # so the LLM resolver reads fresh context per condition.
        self._news_summaries: dict[str, str] = {}
        self._sm_directions: dict[str, str] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def attach_news_summary(self, condition_id: str, summary: str) -> None:
        """Pipeline pushes a 1-3 sentence news digest per condition."""
        if summary:
            self._news_summaries[condition_id] = summary[:600]

    def attach_smart_money(self, condition_id: str, direction: str) -> None:
        """Pipeline pushes 'YES'/'NO'/'NEUTRAL' direction per condition."""
        if direction:
            self._sm_directions[condition_id] = direction

    def llm_usage(self) -> dict[str, Any]:
        """Surface LLMResolver counters for /api/state."""
        if self._llm_resolver is None:
            return {"llm_enabled": False}
        u = self._llm_resolver.get_usage()
        u["llm_enabled"] = True
        return u

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
        resolver = self._resolvers.get(category)

        # Refresh the context's `now` so resolvers see consistent time.
        self._context.now = datetime.now(UTC)

        # Phase R — anchor LLM against the live book before each call.
        # The LLMResolver reads ctx.current_market_p to decide whether
        # a deep-dive (Sonnet) re-query is worth the cost.
        yes_book = self._books.get(market.yes_token_id)
        if yes_book is not None:
            ask = yes_book.best_ask()
            if ask is not None:
                self._context.current_market_p = float(ask[0])

        # Phase R.5 — push the latest per-market news + smart-money flow
        # into the FundamentalsContext so LLM has structured inputs.
        self._context.news_summary = self._news_summaries.get(market.condition_id)
        self._context.smart_money_direction = self._sm_directions.get(
            market.condition_id
        )

        est = None
        if resolver is not None and (
            category in self._enabled_categories or category == "Uncategorized"
        ):
            try:
                est = resolver.resolve(market, self._context)
            except Exception as exc:
                log.warning(
                    "fundamentals.resolver_error",
                    category=category,
                    condition_id=market.condition_id,
                    error=str(exc),
                )

        # Phase R — LLM fallback. Category-specific resolvers return None
        # today (no poll/Elo/funding feeds), so this is the path that
        # actually fires. resolve_async returns a calibrated p_yes from
        # Claude using question text + recent news + smart-money flow.
        # When ANTHROPIC_API_KEY is missing or budget is exhausted, the
        # resolver returns None and the strategy bails silently.
        if est is None and self._llm_resolver is not None:
            try:
                est = await self._llm_resolver.resolve_async(market, self._context)
            except Exception as exc:
                log.warning(
                    "fundamentals.llm_resolver_error",
                    condition_id=market.condition_id,
                    error=str(exc)[:200],
                )

        if est is None:
            from poly_meridian.pipeline import PM_STRATEGY_REJECT
            PM_STRATEGY_REJECT.labels(strategy="fundamentals", reason="resolver_none").inc()
            return None
        if est.confidence < self.min_confidence:
            from poly_meridian.pipeline import PM_STRATEGY_REJECT
            PM_STRATEGY_REJECT.labels(strategy="fundamentals", reason="low_confidence").inc()
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

        # Canonical Kelly inputs on the long side (Phase N.1).
        # For BUY_YES: our_p_long = our_p_yes, market_p_long = market_p_yes.
        # For BUY_NO:  our_p_long = 1 - our_p_yes, market_p_long = 1 - market_p_yes.
        if edge > 0:
            our_p_long = est.p_yes
            market_p_long = market_p
        else:
            our_p_long = 1.0 - est.p_yes
            market_p_long = 1.0 - market_p
        rationale: dict[str, Any] = {
            "category": category,
            "our_p_yes": est.p_yes,
            "market_p_yes": market_p,
            "resolver_confidence": est.confidence,
            "best_ask": float(price),
            "max_size_pct": self.max_size_pct,
            "our_p_long": our_p_long,
            "market_p_long": market_p_long,
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
        # Phase N.1: Kelly on the long-side probability, scaled by resolver
        # confidence so a 0.5-confidence estimate gets half the Kelly.
        our_p = rationale.get("our_p_long")
        mkt_p = rationale.get("market_p_long")
        if our_p is not None and mkt_p is not None:
            from poly_meridian.risk.kelly import sized_kelly
            effective_cap = min(max_size_pct, cap)
            kr = sized_kelly(
                p=float(our_p), market_price=float(mkt_p),
                bankroll_usd=bankroll_usd,
                # Confidence × 0.25 — uncertain resolvers size smaller.
                kelly_fraction_multiplier=0.25 * confidence,
                hard_cap_pct=effective_cap,
            )
            return kr.f_used
        return float(min(max_size_pct, cap * confidence))
