"""Order-book features. Pure compute, fully unit-testable. See §13."""
from __future__ import annotations

from decimal import Decimal

from poly_meridian.ingestion.book import LocalBook


def mid_price(book: LocalBook) -> float | None:
    m = book.mid()
    return float(m) if m is not None else None


def spread(book: LocalBook) -> float | None:
    b = book.best_bid()
    a = book.best_ask()
    if b is None or a is None:
        return None
    return float(a[0] - b[0])


def microprice(book: LocalBook) -> float | None:
    """Size-weighted mid: (bid_p * ask_sz + ask_p * bid_sz) / (bid_sz + ask_sz).

    Returns None when either side is empty or total size is zero.
    """
    b = book.best_bid()
    a = book.best_ask()
    if b is None or a is None:
        return None
    bid_p, bid_sz = b
    ask_p, ask_sz = a
    total = bid_sz + ask_sz
    if total == 0:
        return None
    num = bid_p * ask_sz + ask_p * bid_sz
    return float(num / total)


def depth_imbalance(book: LocalBook, pct_from_mid: Decimal = Decimal("0.05")) -> float | None:
    """(bid_depth − ask_depth) / (bid_depth + ask_depth) within ±pct of mid.

    Returns a value in [-1, 1] or None when both sides are empty.
    """
    bid = book.depth_within("bid", pct_from_mid)
    ask = book.depth_within("ask", pct_from_mid)
    total = bid + ask
    if total == 0:
        return None
    return float((bid - ask) / total)


def bid_depth_within(book: LocalBook, pct_from_mid: Decimal = Decimal("0.05")) -> float:
    return float(book.depth_within("bid", pct_from_mid))


def ask_depth_within(book: LocalBook, pct_from_mid: Decimal = Decimal("0.05")) -> float:
    return float(book.depth_within("ask", pct_from_mid))
