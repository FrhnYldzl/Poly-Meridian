"""Trade-level risk/reward metrics for prediction-market positions.

Polymarket outcome tokens settle at either $1.00 (winner) or $0.00 (loser).
That makes the payoff math very simple:

  Buying outcome token @ entry price `p` for `size` shares:
    max_loss_usd  = p * size              (token resolves to $0)
    max_gain_usd  = (1 - p) * size        (token resolves to $1)
    risk_reward   = (1 - p) / p           (units of gain per unit of risk)

With a strategy edge `e` (our_p − market_p):
    our_p         = clamp(p + e, 0, 1)
    expected_pnl  = (our_p * (1 - p)  -  (1 - our_p) * p) * size
                  = (our_p - p) * size  =  e * size
    ev_per_dollar = e / p

These five numbers fully describe the trade's reward profile and let the
operator answer "what's the downside, what's the upside, and is it worth it?"
without re-deriving on the fly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class TradeMetrics:
    entry_price: float          # token price at entry, 0..1
    size_units: float           # number of shares
    notional_usd: float         # entry_price * size
    max_loss_usd: float         # if token resolves to $0
    max_gain_usd: float         # if token resolves to $1
    risk_reward_ratio: float    # (1-p) / p
    our_prob: float             # market_p + edge, clamped
    expected_pnl_usd: float     # edge * size
    ev_per_dollar: float        # edge / entry_price

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def compute_trade_metrics(
    *,
    entry_price: float | None,
    size_units: float,
    edge: float = 0.0,
) -> TradeMetrics | None:
    """Build a TradeMetrics from the basic order fields.

    Returns None when entry_price is missing or out of (0, 1) bounds —
    in that case the metrics are undefined and we don't want fake numbers
    showing up on the dashboard.
    """
    if entry_price is None:
        return None
    p = float(entry_price)
    if p <= 0.0 or p >= 1.0:
        return None
    size = max(0.0, float(size_units))
    if size == 0.0:
        return None

    notional = p * size
    max_loss = p * size
    max_gain = (1.0 - p) * size
    rr = (1.0 - p) / p if p > 0 else 0.0
    our_p = max(0.0, min(1.0, p + float(edge)))
    expected = float(edge) * size
    ev_per_dollar = float(edge) / p if p > 0 else 0.0

    return TradeMetrics(
        entry_price=p,
        size_units=size,
        notional_usd=notional,
        max_loss_usd=max_loss,
        max_gain_usd=max_gain,
        risk_reward_ratio=rr,
        our_prob=our_p,
        expected_pnl_usd=expected,
        ev_per_dollar=ev_per_dollar,
    )


__all__ = ["TradeMetrics", "compute_trade_metrics"]
