"""Kelly criterion — fractional Kelly sizing. Pure compute. See MASTER_SPEC §15.1.

f* = (b·p − q) / b

  f*   = fraction of bankroll to bet
  b    = net odds = (1 − market_price) / market_price
  p    = our probability of YES resolving true
  q    = 1 − p

Poly Meridian default is **Quarter Kelly** (kelly_fraction=0.25) with a hard
cap of 5% bankroll per position. No Phase X is allowed to skip the cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class KellyResult:
    f_star: float            # unconstrained Kelly fraction
    f_used: float            # after kelly_fraction multiplier + hard cap
    size_usd: Decimal        # bankroll * f_used (rounded down to cents)
    edge: float              # p - market_price
    expected_value: float    # p * (1 - market_price) - (1 - p) * market_price


def kelly_fraction(p: float, market_price: float) -> float:
    """Unconstrained Kelly. Returns 0 when edge is negative or invalid.

    Edge cases handled:
      - market_price <= 0 or >= 1 → 0
      - p <= market_price (no edge) → 0
      - p outside [0, 1] → 0
    """
    if not (0.0 < market_price < 1.0):
        return 0.0
    if not (0.0 <= p <= 1.0):
        return 0.0
    if p <= market_price:
        return 0.0
    b = (1.0 - market_price) / market_price
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


def sized_kelly(
    *,
    p: float,
    market_price: float,
    bankroll_usd: Decimal,
    kelly_fraction_multiplier: float = 0.25,
    hard_cap_pct: float = 0.05,
) -> KellyResult:
    """Quarter Kelly (default) with hard cap. Returns a KellyResult.

    `kelly_fraction_multiplier`:
      - 1.00 = full Kelly (NEVER use in production — too aggressive)
      - 0.50 = half Kelly (industry std)
      - 0.25 = quarter Kelly (Poly Meridian default — see §7.3)

    `hard_cap_pct`: absolute max fraction regardless of Kelly output.
    """
    f_star = kelly_fraction(p, market_price)
    f_used = max(0.0, min(f_star * kelly_fraction_multiplier, hard_cap_pct))
    size = (bankroll_usd * Decimal(str(f_used))).quantize(Decimal("0.01"))
    edge = (p - market_price) if (0.0 <= p <= 1.0 and 0.0 < market_price < 1.0) else 0.0
    ev = p * (1.0 - market_price) - (1.0 - p) * market_price if (0.0 <= p <= 1.0 and 0.0 < market_price < 1.0) else 0.0
    return KellyResult(
        f_star=f_star,
        f_used=f_used,
        size_usd=size,
        edge=edge,
        expected_value=ev,
    )
