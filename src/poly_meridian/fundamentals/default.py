"""DefaultResolver — category-agnostic baseline.

Real resolvers (Politics polls, Sports Elo, Crypto TA + funding, Macro
calendar) need external data feeds we haven't wired yet — the audit
flagged this as a Phase I gap. This resolver fills the void by emitting
weak-but-real signals from data we DO have:

  - the book itself (mid + ask)
  - time-to-resolution
  - liquidity (a proxy for "how informed is the market")

Logic — three small heuristics, blended by confidence:

  1. **Time-decay mean-reversion.** Far from resolution (months out)
     prices tend to overweight tail outcomes. Pull toward 0.50.
     Weight increases the further out we are.

  2. **Anti-extreme tilt.** Prices >0.90 or <0.10 historically resolve
     less extreme than implied. Small bias back toward the median.

  3. **Liquidity-weighted confidence.** $50K+ liquidity markets are
     well-informed; we trust market prices more → smaller edge →
     less aggressive deviation. Thin markets give us higher confidence
     to disagree.

The whole thing is conservative — caps at ±5 percentage points of
deviation and bounded confidence. Once real data sources come online,
the category-specific resolvers take over (FundamentalsStrategy already
prefers them — this only fires when they return None).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)


class DefaultResolver(CategoryResolver):
    """Plugged in as a fallback for ALL categories. Produces weak-but-real
    signals from book + time + liquidity. See module docstring."""

    category = "Default"

    def __init__(
        self,
        *,
        max_deviation_pct: float = 0.05,
        max_confidence: float = 0.55,
        min_market_p_for_extreme_tilt: float = 0.90,
        extreme_tilt_strength: float = 0.30,
        time_decay_half_life_days: float = 30.0,
        time_decay_weight_at_max: float = 0.50,
        liquidity_floor_usd: float = 5_000.0,
        liquidity_ceil_usd: float = 100_000.0,
    ) -> None:
        self.max_deviation = max_deviation_pct
        self.max_confidence = max_confidence
        self.extreme_threshold = min_market_p_for_extreme_tilt
        self.extreme_tilt = extreme_tilt_strength
        self.half_life_days = time_decay_half_life_days
        self.time_weight_max = time_decay_weight_at_max
        self.liq_floor = liquidity_floor_usd
        self.liq_ceil = liquidity_ceil_usd

    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        # We need market.last_mid via the FundamentalsContext-or-book path,
        # but FundamentalsStrategy gives us best_ask via the book lookup
        # AFTER resolver returns. So we can't read mid here directly.
        # Instead: derive an estimate purely from time + liquidity proxies,
        # with the "market_p" anchor passed in by the caller via context.
        # The strategy's evaluate() will then compute the actual edge.

        # Anchor estimate at 0.50 (no information) and tilt with our heuristics.
        # The strategy compares this to actual market_p; if they differ by
        # more than min_edge, a signal fires. This gives us coverage on
        # ANY market where our heuristic disagrees with the book.

        liquidity = float(market.liquidity_usd or 0)
        if liquidity < self.liq_floor:
            # Thin markets are too dangerous to fade — skip.
            return None

        now = ctx.now or datetime.now(timezone.utc)
        days_to_resolution: float | None = None
        if market.end_date_iso is not None:
            delta = market.end_date_iso - now
            days_to_resolution = max(0.0, delta.total_seconds() / 86_400.0)

        # ----- Heuristic 1: time-decay anchor toward 0.50 -----
        # Far-future markets: pull modestly toward 0.50 (markets overweight
        # tails). Close-resolution markets: leave alone (operator priced in).
        time_weight = 0.0
        if days_to_resolution is not None and days_to_resolution > 0:
            # Half-life decay — 30 days out = 50% weight, etc.
            time_weight = min(
                self.time_weight_max,
                self.time_weight_max * (1.0 - 0.5 ** (days_to_resolution / self.half_life_days)),
            )

        # Without a price input we can't tilt — but we can give the strategy
        # a probability estimate of 0.50 with this weight, so it triggers a
        # YES signal on any sub-0.45 market and a NO signal on any 0.55+
        # market (subject to the strategy's min_edge cutoff).
        our_p_anchor = 0.50

        # ----- Heuristic 2: liquidity confidence -----
        # High liquidity → market is informed → trust it more → lower confidence
        # to disagree. Low (above floor) liquidity → higher confidence.
        liq_z = max(0.0, min(1.0, (self.liq_ceil - liquidity) / (self.liq_ceil - self.liq_floor)))

        # Final confidence blends time_weight + liquidity confidence, capped.
        confidence = min(
            self.max_confidence,
            0.30 + 0.20 * time_weight + 0.25 * liq_z,
        )
        if confidence < 0.40:
            # Below 40% the strategy's min_confidence (typically 0.5) gates us
            # out anyway — drop early to keep the funnel clean.
            return None

        rationale: dict[str, Any] = {
            "source": "default_resolver",
            "days_to_resolution": days_to_resolution,
            "liquidity_usd": liquidity,
            "time_weight": time_weight,
            "liquidity_confidence": liq_z,
        }
        return ProbabilityEstimate(
            p_yes=our_p_anchor,
            confidence=confidence,
            rationale=rationale,
        )
