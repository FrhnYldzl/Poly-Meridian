"""Risk-adjusted performance metrics. Pure compute. See MASTER_SPEC §18."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PerformanceMetrics:
    total_return: float
    cagr: float
    volatility_annual: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    expectancy_usd: float
    trade_count: int


def _trading_periods_per_year(period_sec: float) -> float:
    """We don't have actual market sessions — assume continuous time. 24/7
    crypto-style: ~31.5M seconds per year. For paper trading prediction
    markets this is the right denominator."""
    return 365 * 24 * 3600 / max(period_sec, 1.0)


def total_return(equity_curve: Sequence[float]) -> float:
    if len(equity_curve) < 2 or equity_curve[0] == 0:
        return 0.0
    return (equity_curve[-1] - equity_curve[0]) / equity_curve[0]


def cagr(equity_curve: Sequence[float], duration_sec: float) -> float:
    if len(equity_curve) < 2 or equity_curve[0] <= 0 or duration_sec <= 0:
        return 0.0
    years = duration_sec / (365 * 24 * 3600)
    if years <= 0:
        return 0.0
    final = equity_curve[-1] / equity_curve[0]
    return final ** (1.0 / years) - 1.0


def returns_from_equity(equity_curve: Sequence[float]) -> list[float]:
    if len(equity_curve) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev == 0:
            out.append(0.0)
        else:
            out.append((equity_curve[i] - prev) / prev)
    return out


def volatility(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var)


def sharpe_ratio(
    returns: Sequence[float], *, periods_per_year: float, risk_free: float = 0.0
) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    sd = volatility(returns)
    if sd == 0:
        return 0.0
    return (mean - risk_free / periods_per_year) / sd * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: Sequence[float], *, periods_per_year: float, risk_free: float = 0.0
) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if mean > 0 else 0.0
    var = sum(r * r for r in downside) / len(returns)
    dd = math.sqrt(var)
    if dd == 0:
        return 0.0
    return (mean - risk_free / periods_per_year) / dd * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve[1:]:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def calmar_ratio(cagr_value: float, max_dd: float) -> float:
    if max_dd == 0:
        return float("inf") if cagr_value > 0 else 0.0
    return cagr_value / max_dd


def trade_stats(trade_pnls: Sequence[float]) -> tuple[float, float, float]:
    """Returns (win_rate, profit_factor, expectancy)."""
    if not trade_pnls:
        return 0.0, 0.0, 0.0
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    win_rate = len(wins) / len(trade_pnls)
    profit = sum(wins)
    loss = -sum(losses)
    pf = (profit / loss) if loss > 0 else (float("inf") if profit > 0 else 0.0)
    exp = sum(trade_pnls) / len(trade_pnls)
    return win_rate, pf, exp


def compute_all(
    *,
    equity_curve: Sequence[float],
    trade_pnls: Sequence[float],
    duration_sec: float,
    period_sec: float,
) -> PerformanceMetrics:
    """Run every metric in one shot for the backtest report."""
    rets = returns_from_equity(equity_curve)
    ppy = _trading_periods_per_year(period_sec)
    tr = total_return(equity_curve)
    c = cagr(equity_curve, duration_sec)
    vol_a = volatility(rets) * math.sqrt(ppy)
    sr = sharpe_ratio(rets, periods_per_year=ppy)
    so = sortino_ratio(rets, periods_per_year=ppy)
    mdd = max_drawdown(equity_curve)
    cl = calmar_ratio(c, mdd)
    win_rate, pf, exp = trade_stats(trade_pnls)
    return PerformanceMetrics(
        total_return=tr,
        cagr=c,
        volatility_annual=vol_a,
        sharpe=sr,
        sortino=so,
        calmar=cl,
        max_drawdown=mdd,
        win_rate=win_rate,
        profit_factor=pf,
        expectancy_usd=exp,
        trade_count=len(trade_pnls),
    )


def meets_live_gate(metrics: PerformanceMetrics) -> tuple[bool, list[str]]:
    """Master Spec §18 acceptance gate."""
    failures: list[str] = []
    if metrics.sharpe <= 1.5:
        failures.append(f"sharpe {metrics.sharpe:.2f} <= 1.5")
    if metrics.max_drawdown >= 0.25:
        failures.append(f"max_drawdown {metrics.max_drawdown:.2%} >= 25%")
    if metrics.win_rate <= 0.52:
        failures.append(f"win_rate {metrics.win_rate:.2%} <= 52%")
    if metrics.trade_count < 200:
        failures.append(f"trade_count {metrics.trade_count} < 200")
    return (not failures, failures)
