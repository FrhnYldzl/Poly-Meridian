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

        # Phase N.7: hard-disable the anchor-at-0.5 path.
        #
        # The old design returned p_yes=0.50 for ANY liquid market, which the
        # FundamentalsStrategy compared to market_p_yes and fired BUY_NO on
        # every market priced >0.5 (most favorites) and BUY_YES on every
        # market <0.5 — a systematic fade-the-favorite policy. Pre-resolution
        # Polymarket pricing is generally INFORMATIONALLY EFFICIENT, so
        # fading the favorite loses ~55% of the time. EV-negative once fees
        # are netted.
        #
        # Until real category resolvers (Politics polls, Sports Elo, Crypto
        # funding, Macro calendar) land with actual EV-positive signal, the
        # safest fallback is to stay silent. FundamentalsStrategy will just
        # return None for any market whose category-specific resolver also
        # returns None, instead of opening a directional bet on false signal.
        return None

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
