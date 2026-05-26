"""SentimentStrategy — news-driven directional bets. §14.2.

Pulls the window-averaged sentiment for a market (from `news_signals`)
and proposes a YES/NO position when:
  - aggregated impact_max > impact_threshold (e.g. 0.6)
  - winning direction conviction is high enough

Conviction formula (per §14.2): `impact * |sentiment|`, clipped to [0, 1].

The strategy itself doesn't fetch from the DB on every evaluate() — that
would be too slow. The main loop pre-fetches a cache of recent signals
per market and pushes it via `attach_recent_signals()`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, Features, Market, StrategySignal
from poly_meridian.features.sentiment_features import aggregate_signals
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.base import BaseStrategy

log = structlog.get_logger("poly_meridian.strategies.sentiment")


class SentimentStrategy(BaseStrategy):
    """Configuration:
      - enabled: bool
      - window_sec: 1800 (default — last 30 min of news)
      - impact_threshold: 0.6
      - min_signals: 1
      - max_size_pct: 0.03  (smaller than arb default)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(name="sentiment", config=config, enabled=config.get("enabled", False))
        self.window_sec = int(config.get("window_sec", 1800))
        self.impact_threshold = float(config.get("impact_threshold", 0.6))
        self.min_signals = int(config.get("min_signals", 1))
        self.max_size_pct = float(config.get("max_size_pct", 0.03))
        self._books: dict[str, LocalBook] = {}
        self._recent_signals: dict[str, list[dict[str, Any]]] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def attach_recent_signals(
        self, condition_id: str, signals: list[dict[str, Any]]
    ) -> None:
        self._recent_signals[condition_id] = signals

    async def evaluate(
        self, market: Market, features: Features
    ) -> StrategySignal | None:
        if not self.enabled:
            return None

        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        rows = self._recent_signals.get(market.condition_id, [])
        if len(rows) < self.min_signals:
            PM_STRATEGY_REJECT.labels(strategy="sentiment", reason="no_news_signals").inc()
            return None

        agg = aggregate_signals(rows)
        if agg.impact_max < self.impact_threshold:
            PM_STRATEGY_REJECT.labels(strategy="sentiment", reason="impact_below_thr").inc()
            return None
        if agg.winning_direction == "NEUTRAL":
            PM_STRATEGY_REJECT.labels(strategy="sentiment", reason="neutral_direction").inc()
            return None

        # Conviction = clamp(impact * |sentiment|), bounded [0,1].
        conviction = max(0.0, min(1.0, agg.impact_max * abs(agg.sentiment_avg)))
        if conviction <= 0.0:
            PM_STRATEGY_REJECT.labels(strategy="sentiment", reason="conviction_zero").inc()
            return None

        if agg.winning_direction == "YES":
            token_id = market.yes_token_id
            action = Action.BUY_YES
        else:
            token_id = market.no_token_id
            action = Action.BUY_NO

        book = self._books.get(token_id)
        if book is None:
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            return None
        price, _ = best_ask
        # `market_p_long` is the price of the SIDE WE'RE BUYING (YES if
        # winning_direction=YES, NO if =NO). Likewise `our_p_long` is OUR
        # probability that THAT side resolves true.
        market_p_long = float(price)
        # BUG #8 fix: previously `our_p` was computed for the YES side only
        # (market_p + shift), and the edge check `our_p - market_p > 0`
        # silently rejected every BUY_NO candidate (negative sentiment →
        # negative shift → edge < 0). Compute our_p relative to the LONG
        # side so both directions actually fire.
        shift_long = 0.5 * conviction * abs(agg.sentiment_avg)
        our_p_long = max(0.01, min(0.99, market_p_long + shift_long))
        edge = our_p_long - market_p_long
        if edge <= 0:
            return None

        rationale: dict[str, Any] = {
            "window_sec": self.window_sec,
            "n_signals": agg.n_signals,
            "sentiment_avg": agg.sentiment_avg,
            "impact_max": agg.impact_max,
            "winning_direction": agg.winning_direction,
            "best_ask": float(price),
            # Canonical Kelly inputs — long_side probability + price (Phase N.1).
            "our_p_long": our_p_long,
            "market_p_long": market_p_long,
        }

        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=self.name,
            condition_id=market.condition_id,
            token_id=token_id,
            edge=edge,
            conviction=conviction,
            suggested_action=action,
            rationale=rationale,
        )

    def capacity_estimate(self) -> float:
        # Sentiment-driven trades are higher-variance; cap daily capacity lower.
        return 2_500.0

    @staticmethod
    def proposed_price_from_signal(rationale: dict[str, Any]) -> Decimal:
        return Decimal(str(rationale.get("best_ask", 0.5)))

    @staticmethod
    def proposed_size_pct(
        rationale: dict[str, Any],
        bankroll_usd: Decimal,
        max_size_pct: float,
    ) -> float:
        # Phase N.1: Kelly sizing on the long side. Fall back to legacy
        # impact-scaled cap when Kelly inputs missing.
        our_p = rationale.get("our_p_long")
        mkt_p = rationale.get("market_p_long")
        if our_p is not None and mkt_p is not None:
            from poly_meridian.risk.kelly import sized_kelly
            kr = sized_kelly(
                p=float(our_p), market_price=float(mkt_p),
                bankroll_usd=bankroll_usd,
                kelly_fraction_multiplier=0.25, hard_cap_pct=max_size_pct,
            )
            return kr.f_used
        impact = float(rationale.get("impact_max", 0.0))
        return float(min(max_size_pct, max_size_pct * impact))
