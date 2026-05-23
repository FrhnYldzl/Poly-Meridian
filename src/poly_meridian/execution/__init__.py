"""Execution layer — order routing, maker-first logic, paper + live executors. §16."""
from poly_meridian.execution.base import Executor
from poly_meridian.execution.order_router import OrderRouter
from poly_meridian.execution.paper_executor import PaperExecutor
from poly_meridian.execution.slippage_model import estimate_slippage_bps, walk_book_for_fill

__all__ = ["Executor", "OrderRouter", "PaperExecutor", "estimate_slippage_bps", "walk_book_for_fill"]
