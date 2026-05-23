"""Walk-forward fold generation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from poly_meridian.backtest.walkforward import make_folds, slice_dataset
from poly_meridian.backtest.replay import HistoricalDataset, HistoricalTick


def test_make_folds_basic() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=120)
    folds = make_folds(start=start, end=end, train_days=60, test_days=15)
    # First test starts at day 60, then day 75, 90, 105 → 4 folds (last 105→120 fits)
    assert len(folds) == 4
    assert folds[0].train_start == start
    assert folds[0].test_start == start + timedelta(days=60)
    assert folds[0].test_end == start + timedelta(days=75)


def test_make_folds_empty_when_window_too_small() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=10)
    folds = make_folds(start=start, end=end, train_days=60, test_days=15)
    assert folds == []


def test_make_folds_inverted_range_empty() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = start - timedelta(days=10)
    assert make_folds(start=start, end=end, train_days=30, test_days=10) == []


def test_slice_dataset_filters_by_ts() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ticks = [
        HistoricalTick(ts=base + timedelta(hours=i), token_id="t", bids=[], asks=[])
        for i in range(24)
    ]
    ds = HistoricalDataset(markets=[], ticks=ticks)
    sliced = slice_dataset(
        ds, start=base + timedelta(hours=5), end=base + timedelta(hours=15)
    )
    assert len(sliced.ticks) == 10
    assert sliced.ticks[0].ts == base + timedelta(hours=5)
