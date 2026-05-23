"""Feature registry — central catalog. Strategies + persistence read from here. §13."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from poly_meridian.domain import Features
from poly_meridian.features import orderbook_features as ob
from poly_meridian.features import time_features as tf
from poly_meridian.ingestion.book import LocalBook


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    description: str
    requires_book: bool = False
    requires_end_date: bool = False


# Public catalog — append new features here only.
CATALOG: dict[str, FeatureSpec] = {
    "mid_price":           FeatureSpec("mid_price", "mid of best bid/ask", requires_book=True),
    "spread":              FeatureSpec("spread", "best_ask - best_bid", requires_book=True),
    "microprice":          FeatureSpec("microprice", "size-weighted mid", requires_book=True),
    "depth_imbalance_5pct": FeatureSpec(
        "depth_imbalance_5pct", "(bid - ask) / (bid + ask) within 5% of mid",
        requires_book=True,
    ),
    "bid_depth_5pct":      FeatureSpec("bid_depth_5pct", "bid depth within 5%", requires_book=True),
    "ask_depth_5pct":      FeatureSpec("ask_depth_5pct", "ask depth within 5%", requires_book=True),
    "time_to_resolution_hours": FeatureSpec(
        "time_to_resolution_hours", "hours until market resolves",
        requires_end_date=True,
    ),
    "log_time_to_resolution": FeatureSpec(
        "log_time_to_resolution", "ln(hours + 1)", requires_end_date=True,
    ),
    "time_decay_factor": FeatureSpec(
        "time_decay_factor", "0..1, 1 = at resolution", requires_end_date=True,
    ),
}


# Per-feature compute closures. Each takes a context dict and returns float | None.
def _book_compute(fn: Callable[[LocalBook], float | None]) -> Callable[..., float | None]:
    def _c(*, book: LocalBook | None = None, **_: Any) -> float | None:
        return fn(book) if book is not None else None
    return _c


def _book_compute_with_pct(
    fn: Callable[[LocalBook, Decimal], float | None],
) -> Callable[..., float | None]:
    def _c(*, book: LocalBook | None = None, **_: Any) -> float | None:
        return fn(book, Decimal("0.05")) if book is not None else None
    return _c


def _time_compute(
    fn: Callable[[datetime, datetime | None], float | None],
) -> Callable[..., float | None]:
    def _c(
        *,
        now: datetime | None = None,
        end_date: datetime | None = None,
        **_: Any,
    ) -> float | None:
        if now is None:
            return None
        return fn(now, end_date)
    return _c


COMPUTERS: dict[str, Callable[..., float | None]] = {
    "mid_price":               _book_compute(ob.mid_price),
    "spread":                  _book_compute(ob.spread),
    "microprice":              _book_compute(ob.microprice),
    "depth_imbalance_5pct":    _book_compute_with_pct(ob.depth_imbalance),
    "bid_depth_5pct":          _book_compute_with_pct(
        lambda b, pct: ob.bid_depth_within(b, pct)
    ),
    "ask_depth_5pct":          _book_compute_with_pct(
        lambda b, pct: ob.ask_depth_within(b, pct)
    ),
    "time_to_resolution_hours": _time_compute(tf.time_to_resolution_hours),
    "log_time_to_resolution":   _time_compute(tf.log_time_to_resolution),
    "time_decay_factor":        _time_compute(
        lambda now, end: tf.time_decay_factor(now, end, 168.0)
    ),
}


def compute_features(
    *,
    token_id: str,
    now: datetime,
    book: LocalBook | None = None,
    end_date: datetime | None = None,
) -> Features:
    """Compute every catalog feature whose dependencies are satisfied."""
    values: dict[str, float] = {}
    ctx: dict[str, Any] = {"book": book, "now": now, "end_date": end_date}
    for name in CATALOG:
        try:
            v = COMPUTERS[name](**ctx)
        except Exception:
            v = None
        if v is not None:
            values[name] = float(v)
    return Features(ts=now, token_id=token_id, values=values)
