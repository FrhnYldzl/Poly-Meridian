"""Slippage estimation. Pure compute. See MASTER_SPEC §16.2.

Model: expected_slippage_bps = a * (order_size / book_depth_at_5pct)^b
Phase 2 shipped defaults (a=50, b=1.2). Phase 5b adds `fit_from_fills()`
to re-calibrate from realized paper-trade slippage observations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from poly_meridian.ingestion.book import LocalBook


@dataclass(frozen=True)
class SlippageFit:
    a: float
    b: float
    n_samples: int
    rmse_bps: float

    def estimate_bps(self, *, size: float, depth: float) -> float:
        if depth <= 0:
            return float("inf")
        return self.a * math.pow(size / depth, self.b)


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


def fit_from_fills(observations: list[dict[str, float]]) -> SlippageFit | None:
    """Fit `slippage_bps = a * (size/depth)^b` via log-log linear regression.

    `observations` = list of {"size", "depth", "slippage_bps"}. Returns None
    when not enough samples (<10) or all values are non-positive.

    log(s) = log(a) + b * log(r)   where r = size/depth
    """
    points: list[tuple[float, float]] = []
    for o in observations:
        s = float(o.get("slippage_bps", 0))
        size = float(o.get("size", 0))
        depth = float(o.get("depth", 0))
        if s <= 0 or size <= 0 or depth <= 0:
            continue
        r = size / depth
        if r <= 0:
            continue
        points.append((math.log(r), math.log(s)))

    if len(points) < 10:
        return None

    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in points)
    den = sum((x - mean_x) ** 2 for x, _ in points)
    if den <= 0:
        return None
    b = num / den
    log_a = mean_y - b * mean_x
    a = math.exp(log_a)

    # RMSE in bps space (not log).
    sq = 0.0
    for o in observations:
        s = float(o.get("slippage_bps", 0))
        size = float(o.get("size", 0))
        depth = float(o.get("depth", 0))
        if s <= 0 or size <= 0 or depth <= 0:
            continue
        predicted = a * math.pow(size / depth, b)
        sq += (predicted - s) ** 2
    rmse = math.sqrt(sq / n)
    return SlippageFit(a=a, b=b, n_samples=n, rmse_bps=rmse)


def slippage_from_fill(
    *,
    expected_price: float,
    realized_vwap: float,
) -> float:
    """Convert a single paper fill into observed slippage_bps. Sign-aware.

    Slippage = |realized - expected| / expected, in basis points.
    """
    if expected_price <= 0:
        return 0.0
    return abs(realized_vwap - expected_price) / expected_price * 10_000.0


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
