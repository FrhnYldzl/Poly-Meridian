"""Execution layer — order routing, maker-first logic, paper + live executors. §16."""
from poly_meridian.execution.base import Executor
from poly_meridian.execution.fees import DEFAULT_FEES, FeeSchedule
from poly_meridian.execution.live_executor import LiveExecutor
from poly_meridian.execution.order_router import OrderRouter
from poly_meridian.execution.paper_executor import PaperExecutor
from poly_meridian.execution.slippage_model import (
    SlippageFit,
    estimate_slippage_bps,
    fit_from_fills,
    slippage_from_fill,
    walk_book_for_fill,
)

__all__ = [
    "DEFAULT_FEES",
    "Executor",
    "FeeSchedule",
    "LiveExecutor",
    "OrderRouter",
    "PaperExecutor",
    "SlippageFit",
    "estimate_slippage_bps",
    "fit_from_fills",
    "slippage_from_fill",
    "walk_book_for_fill",
]
