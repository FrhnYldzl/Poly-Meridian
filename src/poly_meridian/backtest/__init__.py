"""Backtest engine — historical replay, walk-forward, metrics. See §18."""
from poly_meridian.backtest.metrics import (
    PerformanceMetrics,
    compute_all,
    meets_live_gate,
)
from poly_meridian.backtest.replay import (
    HistoricalDataset,
    HistoricalTick,
    ReplayConfig,
    ReplayResult,
    Replayer,
)
from poly_meridian.backtest.reports import (
    report_json,
    report_markdown,
    write_report,
)
from poly_meridian.backtest.walkforward import (
    Fold,
    iter_folds,
    make_folds,
    slice_dataset,
)

__all__ = [
    "Fold",
    "HistoricalDataset",
    "HistoricalTick",
    "PerformanceMetrics",
    "ReplayConfig",
    "ReplayResult",
    "Replayer",
    "compute_all",
    "iter_folds",
    "make_folds",
    "meets_live_gate",
    "report_json",
    "report_markdown",
    "slice_dataset",
    "write_report",
]
