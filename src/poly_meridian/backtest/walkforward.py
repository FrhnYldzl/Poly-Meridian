"""Walk-forward validation. See MASTER_SPEC §18.

Cuts the historical window into rolling train/test pairs and runs the
replay on each test slice. Strategies stay configuration-locked across
all folds (no parameter tuning between folds in Phase 4 — that's a
hyper-parameter search story for Phase 6+).

Phase 5b adds `run_folds()` which runs the full Replayer across every
fold and aggregates per-fold metrics + overall.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator

from poly_meridian.backtest.metrics import PerformanceMetrics, compute_all
from poly_meridian.backtest.replay import (
    HistoricalDataset,
    HistoricalTick,
    ReplayConfig,
    Replayer,
)
from poly_meridian.risk import DefaultRiskPolicy
from poly_meridian.strategies import SignalAggregator
from poly_meridian.strategies.base import BaseStrategy


@dataclass(frozen=True)
class Fold:
    fold_index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def make_folds(
    *,
    start: datetime,
    end: datetime,
    train_days: int,
    test_days: int,
    step_days: int | None = None,
) -> list[Fold]:
    """Sliding train/test split.

    Each fold has `train_days` history followed by `test_days` of out-of-
    sample. Step defaults to `test_days` (non-overlapping test windows).
    """
    if start >= end:
        return []
    step = timedelta(days=step_days or test_days)
    train_td = timedelta(days=train_days)
    test_td = timedelta(days=test_days)

    folds: list[Fold] = []
    cursor = start + train_td
    idx = 0
    while cursor + test_td <= end:
        train_start = cursor - train_td
        train_end = cursor
        test_start = cursor
        test_end = cursor + test_td
        folds.append(Fold(idx, train_start, train_end, test_start, test_end))
        cursor += step
        idx += 1
    return folds


def slice_dataset(
    dataset: HistoricalDataset, *, start: datetime, end: datetime
) -> HistoricalDataset:
    ticks: list[HistoricalTick] = [
        t for t in dataset.ticks if start <= t.ts < end
    ]
    return HistoricalDataset(
        markets=dataset.markets,
        ticks=ticks,
        news_signals=[
            s for s in dataset.news_signals
            if start <= s.get("ts", start) < end
        ],
        smart_money_state=dataset.smart_money_state,
    )


def iter_folds(
    dataset: HistoricalDataset, folds: list[Fold]
) -> Iterator[tuple[Fold, HistoricalDataset]]:
    for fold in folds:
        slc = slice_dataset(dataset, start=fold.test_start, end=fold.test_end)
        yield fold, slc


@dataclass
class FoldResult:
    fold: Fold
    metrics: PerformanceMetrics
    final_nav: float
    n_ticks: int


@dataclass
class WalkForwardResult:
    folds: list[FoldResult] = field(default_factory=list)

    def aggregate_metrics(self) -> dict[str, float]:
        """Cross-fold summary: mean Sharpe, worst max-DD, total trade count."""
        if not self.folds:
            return {}
        n = len(self.folds)
        return {
            "n_folds": n,
            "mean_total_return": sum(f.metrics.total_return for f in self.folds) / n,
            "mean_sharpe": sum(f.metrics.sharpe for f in self.folds) / n,
            "median_sharpe": _median([f.metrics.sharpe for f in self.folds]),
            "worst_max_drawdown": max(f.metrics.max_drawdown for f in self.folds),
            "mean_win_rate": sum(f.metrics.win_rate for f in self.folds) / n,
            "total_trades": sum(f.metrics.trade_count for f in self.folds),
        }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


async def run_folds(
    *,
    dataset: HistoricalDataset,
    folds: list[Fold],
    strategy_factory,        # zero-arg callable returning fresh strategies + risk + aggregator
    config: ReplayConfig | None = None,
) -> WalkForwardResult:
    """Run a `Replayer` across each fold with fresh strategy state.

    `strategy_factory()` must return a tuple `(strategies, aggregator, risk)`.
    A factory is required so each fold gets independent strategy instances —
    in-process state (rolling windows, cluster state, etc.) shouldn't leak.
    """
    result = WalkForwardResult()
    for fold, slc in iter_folds(dataset, folds):
        strategies, aggregator, risk = strategy_factory()
        replayer = Replayer(
            dataset=slc,
            strategies=strategies,
            aggregator=aggregator,
            risk=risk,
            config=config,
        )
        rep_result = await replayer.run()
        duration_sec = rep_result.duration_sec or 1.0
        period_sec = (config.tick_interval_sec if config else 60)
        metrics = compute_all(
            equity_curve=rep_result.equity_curve,
            trade_pnls=rep_result.trade_pnls,
            duration_sec=duration_sec,
            period_sec=period_sec,
        )
        result.folds.append(
            FoldResult(
                fold=fold,
                metrics=metrics,
                final_nav=rep_result.final_nav,
                n_ticks=len(slc.ticks),
            )
        )
    return result


# Re-export factory signature alias so callers can type-hint cleanly.
StrategyFactory = "callable returning (strategies, aggregator, risk)"
