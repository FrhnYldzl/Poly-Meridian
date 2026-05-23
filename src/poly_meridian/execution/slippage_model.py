"""Slippage estimation. Pure compute. See MASTER_SPEC §16.2.

Simple model: expected_slippage = a * (order_size / book_depth_at_5pct)^b
Defaults are conservative initial guesses; Phase 4 backtest engine will
re-fit these from realized fills.
"""
from __future__ import annotations

from decimal import Decimal

from poly_meridian.ingestion.book import LocalBook


def estimate_slippage_bps(
    *,
    book: LocalBook,
    side: str,
    size: Decimal,
    a: float = 50.0,
    b: float = 1.2,
    pct_from_mid: Decimal = Decimal("0.05"),
) -> float:
    """Returns expected slippage in basis points. 0 when book has no depth."""
    depth = book.depth_within("ask" if side.upper() == "BUY" else "bid", pct_from_mid)
    if depth <= 0:
        return float("inf")
    ratio = float(size) / float(depth)
    return float(a * (ratio ** b))


def walk_book_for_fill(
    book: LocalBook,
    side: str,
    size: Decimal,
) -> tuple[Decimal | None, Decimal]:
    """Simulate eating book depth top-down. Returns (vwap_fill_price, filled_size).

    Used by PaperExecutor to estimate taker fills.
    """
    levels = sorted(book.asks.items()) if side.upper() == "BUY" else sorted(
        book.bids.items(), reverse=True
    )
    remaining = size
    cost = Decimal(0)
    filled = Decimal(0)
    for price, lvl_size in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl_size)
        cost += take * price
        filled += take
        remaining -= take
    if filled <= 0:
        return None, Decimal(0)
    return (cost / filled).quantize(Decimal("0.0001")), filled
