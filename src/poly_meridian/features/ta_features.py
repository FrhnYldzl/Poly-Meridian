"""Technical-analysis features. Pure compute, fully testable. §13."""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime


def rolling_volatility(prices: Iterable[float]) -> float | None:
    """Population std-dev of a price series. None when <2 samples."""
    arr = list(prices)
    if len(arr) < 2:
        return None
    mean = sum(arr) / len(arr)
    var = sum((p - mean) ** 2 for p in arr) / len(arr)
    return math.sqrt(var)


def rolling_zscore(prices: Iterable[float]) -> float | None:
    arr = list(prices)
    if len(arr) < 2:
        return None
    mean = sum(arr) / len(arr)
    sd = rolling_volatility(arr) or 0.0
    if sd == 0:
        return 0.0
    return (arr[-1] - mean) / sd


def trade_count(trade_ts: Iterable[datetime]) -> int:
    return sum(1 for _ in trade_ts)


def rolling_volume(trade_sizes: Iterable[float]) -> float:
    return float(sum(trade_sizes))


def momentum(prices: Iterable[float]) -> float | None:
    """Return = (last - first) / first. None when <2 samples or first==0."""
    arr = list(prices)
    if len(arr) < 2 or arr[0] == 0:
        return None
    return (arr[-1] - arr[0]) / arr[0]


def rsi(prices: Iterable[float], period: int = 14) -> float | None:
    """Classic Wilder RSI. None when not enough samples.

    Polymarket prices live in (0, 1) so RSI behaves similarly to equity
    markets — overbought >70, oversold <30 are still meaningful.
    """
    arr = list(prices)
    if len(arr) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = arr[i] - arr[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(arr)):
        diff = arr[i] - arr[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass
class RollingPriceWindow:
    """Bounded-deque price history per token. Pure in-memory; survives
    process lifetime only. Strategies use this to compute features in
    real time without re-querying the DB."""

    capacity: int
    prices: deque[float] = field(default_factory=lambda: deque(maxlen=0))

    def __post_init__(self) -> None:
        self.prices = deque(maxlen=self.capacity)

    def push(self, price: float) -> None:
        self.prices.append(price)

    def latest(self) -> float | None:
        return self.prices[-1] if self.prices else None

    def list(self) -> list[float]:
        return list(self.prices)
