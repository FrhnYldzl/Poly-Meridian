"""Walk-forward validation. See MASTER_SPEC §18.

Cuts the historical window into rolling train/test pairs and runs the
replay on each test slice. Strategies stay configuration-locked across
all folds (no parameter tuning between folds in Phase 4 — that's a
hyper-parameter search story for Phase 5+).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

from poly_meridian.backtest.replay import HistoricalDataset, HistoricalTick


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
