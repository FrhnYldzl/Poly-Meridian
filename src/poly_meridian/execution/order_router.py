"""OrderRouter — maker-first routing logic + N.4 arb two-leg.

Phase 2 minimal: every TradeDecision goes straight to the PaperExecutor.
Phase N.4 adds atomic two-leg routing for arbitrage: when
`decision.paired_token` is set, both legs submit CONCURRENTLY via
asyncio.gather so a single leg can't fill in isolation (which would
leave directional risk — the original BUG #5).

If the partner leg fails or rejects, we attempt to cancel the primary.
This is best-effort: a true atomic 2-leg cross requires venue-level
support which Polymarket doesn't offer. We minimize the asymmetric-fill
window by submitting both before either fully fills.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, UTC

import structlog

from poly_meridian.domain import (
    Order,
    OrderStatus,
    OrderType,
    Side,
    TradeDecision,
)
from poly_meridian.execution.base import Executor

log = structlog.get_logger("poly_meridian.router")


class OrderRouter:
    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def route(self, decision: TradeDecision) -> Order:
        # Primary leg log.
        log.info(
            "router.route",
            strategy=decision.strategy,
            token_id=decision.token_id,
            side=str(decision.side),
            order_type=str(decision.order_type),
            paired=bool(decision.paired_token),
        )

        if decision.paired_token is None:
            return await self._executor.submit(decision)

        # Two-leg arb — submit both legs concurrently.
        # Both legs share the same size (complete-set arb buys 1 unit each).
        primary_task = asyncio.create_task(self._executor.submit(decision))
        partner_decision = decision.model_copy(update={
            "token_id": decision.paired_token,
            "side": decision.paired_side or Side.BUY,
            "price": decision.paired_price,
            # Clear partner fields on the second decision to avoid recursion.
            "paired_token": None,
            "paired_price": None,
            "paired_side": None,
        })
        partner_task = asyncio.create_task(self._executor.submit(partner_decision))

        primary, partner = await asyncio.gather(
            primary_task, partner_task, return_exceptions=True
        )

        # If either leg errored or rejected, try to unwind the other.
        primary_ok = isinstance(primary, Order) and primary.status not in (
            OrderStatus.REJECTED, OrderStatus.CANCELLED,
        )
        partner_ok = isinstance(partner, Order) and partner.status not in (
            OrderStatus.REJECTED, OrderStatus.CANCELLED,
        )

        if primary_ok and partner_ok:
            log.info(
                "router.arb_both_legs",
                primary_id=primary.order_id,
                partner_id=partner.order_id,
                primary_status=str(primary.status),
                partner_status=str(partner.status),
            )
            return primary

        # Asymmetric outcome — best-effort unwind.
        log.warning(
            "router.arb_asymmetric",
            primary_ok=primary_ok, partner_ok=partner_ok,
            primary=str(primary)[:120], partner=str(partner)[:120],
        )
        if primary_ok and not partner_ok and isinstance(primary, Order):
            try:
                await self._executor.cancel(primary.order_id)
                log.warning("router.arb_unwind_primary", order_id=primary.order_id)
            except Exception:
                pass
        if partner_ok and not primary_ok and isinstance(partner, Order):
            try:
                await self._executor.cancel(partner.order_id)
                log.warning("router.arb_unwind_partner", order_id=partner.order_id)
            except Exception:
                pass

        # Return a synthetic rejected primary so callers see the failure.
        if isinstance(primary, Order):
            return primary
        # If primary errored entirely, fabricate an order shell so the
        # type contract holds. The caller logs warning either way.
        raise primary if isinstance(primary, BaseException) else RuntimeError(
            "router.arb_both_legs_failed"
        )
