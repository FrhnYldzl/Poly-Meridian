"""OrderRouter — maker-first routing logic. §16.1.

Phase 2 minimal: every TradeDecision goes straight to the PaperExecutor.
The "maker for T sec, then taker" cascade lives here once we have
PriceChange-driven cancel/replace plumbing (Phase 3).

For now this is a one-liner thin layer so the main-loop wiring is in
place when we expand it.
"""
from __future__ import annotations

import structlog

from poly_meridian.domain import Order, TradeDecision
from poly_meridian.execution.base import Executor

log = structlog.get_logger("poly_meridian.router")


class OrderRouter:
    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def route(self, decision: TradeDecision) -> Order:
        log.info(
            "router.route",
            strategy=decision.strategy,
            token_id=decision.token_id,
            side=str(decision.side),
            order_type=str(decision.order_type),
        )
        return await self._executor.submit(decision)
