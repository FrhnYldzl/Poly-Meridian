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
    FoldResult,
    WalkForwardResult,
    iter_folds,
    make_folds,
    run_folds,
    slice_dataset,
)

__all__ = [
    "Fold",
    "FoldResult",
    "HistoricalDataset",
    "HistoricalTick",
    "PerformanceMetrics",
    "ReplayConfig",
    "ReplayResult",
    "Replayer",
    "WalkForwardResult",
    "compute_all",
    "iter_folds",
    "make_folds",
    "meets_live_gate",
    "report_json",
    "report_markdown",
    "run_folds",
    "slice_dataset",
    "write_report",
]
