"""Time-to-resolution features. See §13."""
from __future__ import annotations

import math
from datetime import datetime


def time_to_resolution_hours(now: datetime, end_date: datetime | None) -> float | None:
    if end_date is None:
        return None
    if end_date.tzinfo != now.tzinfo and (end_date.tzinfo is None or now.tzinfo is None):
        return None
    delta = (end_date - now).total_seconds() / 3600.0
    return float(delta)


def log_time_to_resolution(now: datetime, end_date: datetime | None) -> float | None:
    h = time_to_resolution_hours(now, end_date)
    if h is None or h <= 0:
        return None
    return math.log(h + 1.0)


def time_decay_factor(now: datetime, end_date: datetime | None, horizon_hours: float = 168.0) -> float | None:
    """Smooth 0..1 factor: 0 right after listing, 1 right before resolution.

    Useful weight for time-decay arb signals (§14.4).
    """
    h = time_to_resolution_hours(now, end_date)
    if h is None or h <= 0:
        return 1.0
    return float(max(0.0, 1.0 - min(1.0, h / horizon_hours)))
