"""Slippage model re-fit math."""
from __future__ import annotations

import math

import pytest

from poly_meridian.execution.slippage_model import (
    SlippageFit,
    fit_from_fills,
    slippage_from_fill,
)


def test_slippage_from_fill_basic() -> None:
    # +5% from 0.40 → 0.42 = 500 bps
    assert slippage_from_fill(expected_price=0.40, realized_vwap=0.42) == pytest.approx(500.0)
    assert slippage_from_fill(expected_price=0.40, realized_vwap=0.38) == pytest.approx(500.0)
    assert slippage_from_fill(expected_price=0.0, realized_vwap=0.40) == 0.0


def test_fit_from_fills_recovers_true_params() -> None:
    """Synthesize observations from known a/b, fit, expect close match."""
    a_true, b_true = 60.0, 1.3
    obs: list[dict[str, float]] = []
    for i in range(20):
        size = 100 + 50 * i
        depth = 5000
        s_bps = a_true * (size / depth) ** b_true
        obs.append({"size": size, "depth": depth, "slippage_bps": s_bps})
    fit = fit_from_fills(obs)
    assert fit is not None
    assert isinstance(fit, SlippageFit)
    assert fit.a == pytest.approx(a_true, rel=0.05)
    assert fit.b == pytest.approx(b_true, rel=0.05)
    assert fit.rmse_bps < 0.5      # nearly perfect fit on synthetic data


def test_fit_from_fills_returns_none_when_few_samples() -> None:
    obs = [{"size": 100, "depth": 1000, "slippage_bps": 10}]
    assert fit_from_fills(obs) is None


def test_fit_estimate_bps() -> None:
    fit = SlippageFit(a=50, b=1.2, n_samples=100, rmse_bps=5)
    # size=200, depth=1000 → r=0.2 → 50 * 0.2^1.2 ≈ 7.25
    val = fit.estimate_bps(size=200, depth=1000)
    assert val == pytest.approx(50 * math.pow(0.2, 1.2), rel=0.001)


def test_fit_handles_noise() -> None:
    """Add 10% multiplicative noise; fit should still be reasonable."""
    import random
    rng = random.Random(42)
    a_true, b_true = 80.0, 1.1
    obs: list[dict[str, float]] = []
    for i in range(40):
        size = 50 + 25 * i
        depth = 4000
        ideal = a_true * (size / depth) ** b_true
        noisy = ideal * rng.uniform(0.9, 1.1)
        obs.append({"size": size, "depth": depth, "slippage_bps": noisy})
    fit = fit_from_fills(obs)
    assert fit is not None
    # Should be within 25% of truth despite noise.
    assert fit.a == pytest.approx(a_true, rel=0.25)
    assert fit.b == pytest.approx(b_true, rel=0.25)
