"""Crypto resolver — TA-driven probability for price-target markets. §14.5.

Expects `ctx.crypto_metadata[condition_id]` to contain:
  {
    "symbol": "BTC-USD",
    "target_price": 150_000.0,
    "deadline_ts": datetime,       # market resolution date
    "direction": "above",          # or "below"
  }

And in `ctx.spot_prices`, `ctx.funding_rates`, `ctx.netflow_24h`:
  - latest spot for the symbol
  - funding rate (positive = longs paying shorts)
  - 24h exchange netflow (positive USD = inflow → bullish-ish)

Model: bridge implied price between spot and target via volatility-adjusted
log-normal random walk approximation, with funding + netflow as bias terms.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)


class CryptoResolver(CategoryResolver):
    category = "Crypto"

    def __init__(
        self,
        *,
        default_annual_vol: float = 0.80,        # 80% annualized for BTC-class
        funding_weight: float = 2.0,             # rough beta from rate → drift
        netflow_weight: float = 0.00000005,      # USD → drift scaling
    ) -> None:
        self.default_annual_vol = default_annual_vol
        self.funding_weight = funding_weight
        self.netflow_weight = netflow_weight

    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        meta = ctx.crypto_metadata.get(market.condition_id)
        if meta is None:
            return None

        symbol = meta.get("symbol")
        target = meta.get("target_price")
        deadline = meta.get("deadline_ts") or market.end_date_iso
        direction = (meta.get("direction") or "above").lower()
        if not symbol or target is None or deadline is None:
            return None

        spot = ctx.spot_prices.get(symbol)
        if spot is None or spot <= 0 or target <= 0:
            return None

        now = ctx.now or datetime.now(UTC)
        seconds_left = (deadline - now).total_seconds()
        if seconds_left <= 0:
            return None
        years_left = seconds_left / (365.25 * 24 * 3600)

        # Drift from funding rate (annualized) + netflow.
        funding = float(ctx.funding_rates.get(symbol, 0.0)) * 365 * 3  # 8h funding → annual
        netflow = float(ctx.netflow_24h.get(symbol, 0.0))
        drift = self.funding_weight * funding + self.netflow_weight * netflow
        sigma = self.default_annual_vol

        log_return_to_target = math.log(target / spot)
        mean = drift * years_left
        std = sigma * math.sqrt(max(years_left, 1e-6))
        if std <= 0:
            return None

        # Standard normal CDF approximation (Abramowitz & Stegun 7.1.26).
        z = (log_return_to_target - mean) / std
        p_above = 1.0 - _norm_cdf(z)
        p_yes = p_above if direction.startswith("above") else (1.0 - p_above)

        # Confidence: tighter when far from target & longer remaining time.
        confidence = min(1.0, 0.4 + min(0.6, abs(z) / 3.0))

        return ProbabilityEstimate(
            p_yes=p_yes,
            confidence=confidence,
            rationale={
                "category": "Crypto",
                "symbol": symbol,
                "spot": spot,
                "target": target,
                "years_left": years_left,
                "drift": drift,
                "sigma": sigma,
                "z_score": z,
                "p_above": p_above,
                "direction": direction,
            },
        )


def _norm_cdf(x: float) -> float:
    """Cumulative normal — Abramowitz/Stegun 7.1.26 polynomial approximation."""
    if x < 0:
        return 1.0 - _norm_cdf(-x)
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    t = 1.0 / (1.0 + p * x)
    erf = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + erf)
