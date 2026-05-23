"""Backtest metrics math."""
from __future__ import annotations

import math

import pytest

from poly_meridian.backtest.metrics import (
    cagr,
    compute_all,
    max_drawdown,
    meets_live_gate,
    returns_from_equity,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    trade_stats,
)


def test_total_return() -> None:
    assert total_return([100, 110]) == pytest.approx(0.10)
    assert total_return([100]) == 0.0


def test_max_drawdown_basic() -> None:
    assert max_drawdown([100, 120, 80, 90, 110]) == pytest.approx((120 - 80) / 120)


def test_max_drawdown_monotone_up_is_zero() -> None:
    assert max_drawdown([100, 110, 120, 130]) == 0.0


def test_returns_from_equity() -> None:
    r = returns_from_equity([100, 110, 121])
    assert r[0] == pytest.approx(0.10)
    assert r[1] == pytest.approx(0.10)


def test_sharpe_positive_for_consistent_uptrend() -> None:
    rets = [0.01, 0.01, 0.01, 0.01, 0.01]
    s = sharpe_ratio(rets, periods_per_year=252)
    assert s > 0


def test_sharpe_zero_when_no_volatility() -> None:
    rets = [0.0, 0.0, 0.0]
    s = sharpe_ratio(rets, periods_per_year=252)
    assert s == 0.0


def test_sortino_only_penalises_downside() -> None:
    # Mostly positive returns with one drawdown
    rets = [0.02] * 10 + [-0.05]
    so = sortino_ratio(rets, periods_per_year=252)
    sh = sharpe_ratio(rets, periods_per_year=252)
    # Sortino should be at least as high as Sharpe (treats upside vol favourably).
    assert so >= sh


def test_cagr_positive() -> None:
    one_year = 365 * 24 * 3600
    assert cagr([100, 200], one_year) == pytest.approx(1.0)


def test_trade_stats() -> None:
    win_rate, pf, exp = trade_stats([10, -5, 20, -10, 15])
    assert win_rate == pytest.approx(3 / 5)
    # profit = 45, loss = 15 → pf = 3
    assert pf == pytest.approx(3.0)
    # expectancy = (10 - 5 + 20 - 10 + 15) / 5 = 6
    assert exp == pytest.approx(6.0)


def test_compute_all_assembles_metrics() -> None:
    duration_sec = 30 * 24 * 3600       # 30 days
    equity = [100, 110, 105, 115, 120, 125]
    trades = [5.0, -2.0, 10.0]
    m = compute_all(
        equity_curve=equity,
        trade_pnls=trades,
        duration_sec=duration_sec,
        period_sec=60.0,
    )
    assert m.trade_count == 3
    assert m.total_return > 0
    assert not math.isnan(m.sharpe)
    assert m.max_drawdown > 0


def test_live_gate_fails_when_sharpe_low() -> None:
    from poly_meridian.backtest.metrics import PerformanceMetrics

    metrics = PerformanceMetrics(
        total_return=0.10, cagr=0.10, volatility_annual=0.10,
        sharpe=1.0, sortino=1.0, calmar=0.5,
        max_drawdown=0.10, win_rate=0.60, profit_factor=2.0,
        expectancy_usd=5.0, trade_count=300,
    )
    passes, fails = meets_live_gate(metrics)
    assert not passes
    assert any("sharpe" in f for f in fails)


def test_live_gate_passes_when_all_thresholds_met() -> None:
    from poly_meridian.backtest.metrics import PerformanceMetrics

    metrics = PerformanceMetrics(
        total_return=0.20, cagr=0.40, volatility_annual=0.25,
        sharpe=2.0, sortino=2.5, calmar=1.6,
        max_drawdown=0.20, win_rate=0.58, profit_factor=2.0,
        expectancy_usd=8.0, trade_count=300,
    )
    passes, fails = meets_live_gate(metrics)
    assert passes
    assert fails == []
