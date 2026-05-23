"""TA features — pure-compute correctness."""
from __future__ import annotations

import pytest

from poly_meridian.features.ta_features import (
    RollingPriceWindow,
    momentum,
    rolling_volatility,
    rolling_zscore,
    rsi,
)


def test_volatility_single_sample_is_none() -> None:
    assert rolling_volatility([0.5]) is None


def test_volatility_constant_series_is_zero() -> None:
    assert rolling_volatility([0.5, 0.5, 0.5, 0.5]) == pytest.approx(0.0)


def test_volatility_known_value() -> None:
    # Values [1, 2, 3, 4, 5] → mean=3, pop-var = 2.0, sd ≈ sqrt(2) = 1.4142
    v = rolling_volatility([1, 2, 3, 4, 5])
    assert v == pytest.approx(1.4142, abs=1e-3)


def test_zscore_at_extreme() -> None:
    # last value much higher than mean → positive z-score
    z = rolling_zscore([0.5] * 10 + [0.9])
    assert z is not None
    assert z > 2.0


def test_momentum_positive() -> None:
    assert momentum([0.40, 0.50]) == pytest.approx(0.25)


def test_momentum_returns_none_when_first_zero() -> None:
    assert momentum([0.0, 0.5]) is None


def test_rsi_returns_none_when_short() -> None:
    assert rsi([0.5, 0.6, 0.55], period=14) is None


def test_rsi_pure_uptrend_near_100() -> None:
    prices = [0.30 + i * 0.01 for i in range(30)]
    val = rsi(prices, period=14)
    assert val is not None
    assert val > 90


def test_rsi_pure_downtrend_near_zero() -> None:
    prices = [0.80 - i * 0.01 for i in range(30)]
    val = rsi(prices, period=14)
    assert val is not None
    assert val < 10


def test_rolling_window_obeys_capacity() -> None:
    win = RollingPriceWindow(capacity=3)
    for p in [1, 2, 3, 4, 5]:
        win.push(float(p))
    assert win.list() == [3.0, 4.0, 5.0]
    assert win.latest() == 5.0
