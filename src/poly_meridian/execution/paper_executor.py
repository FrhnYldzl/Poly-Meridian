"""PaperExecutor — simulate fills against the local book. §16.3.

Every order is persisted to `our_orders` with `mode='paper'`. Fills are
simulated:
  - **Maker (GTC at a price)**: posted to a virtual book; filled when subsequent
    trades cross the price. Phase 2 simplification — we treat a maker order
    as "rests at price P, fills if a price_change event shows a trade at
    P or better against us". We DO immediately fill if our price is already
    crossing the book (e.g. BUY at $0.45 when best ask is $0.44).
  - **Taker (FOK/FAK)**: walks the book top-down (`walk_book_for_fill`),
    pays VWAP.

This module is **mode='paper' only**. Live executor (§16.4) lands in Phase 6.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from poly_meridian.domain import (
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TradeDecision,
)
from poly_meridian.execution.base import Executor
from poly_meridian.execution.fees import DEFAULT_FEES, FeeSchedule
from poly_meridian.execution.slippage_model import walk_book_for_fill
from poly_meridian.ingestion.book import LocalBook

log = structlog.get_logger("poly_meridian.paper_executor")


class _ResingOrder:
    """A maker order sitting on a virtual book until filled or cancelled."""

    __slots__ = ("order", "remaining", "submitted_at")

    def __init__(self, order: Order) -> None:
        self.order = order
        self.remaining = order.size
        self.submitted_at = datetime.now(UTC)


class PaperExecutor(Executor):
    """Simulates fills against `LocalBook` instances supplied by the caller.

    The caller (main loop) wires the per-token `LocalBook` references via
    `attach_book()` once at startup. Then submit/cancel/reconcile drive
    state.
    """

    mode = Mode.PAPER

    def __init__(
        self,
        *,
        maker_timeout_sec: float = 60.0,
        fee_schedule: FeeSchedule | None = None,
        us_mode: bool = False,
        on_fill: object = None,  # Callable[[Order], Awaitable[None]] — wired by main loop
    ) -> None:
        self._books: dict[str, LocalBook] = {}
        self._orders: dict[str, Order] = {}
        self._resting: dict[str, _ResingOrder] = {}
        self._maker_timeout_sec = maker_timeout_sec
        self._lock = asyncio.Lock()
        self._on_fill = on_fill  # async callback per filled order
        self._fees = fee_schedule or DEFAULT_FEES
        self._us_mode = us_mode
        self._token_category: dict[str, str] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def attach_category(self, token_id: str, category: str | None) -> None:
        if category:
            self._token_category[token_id] = category

    def estimate_fill_fee(
        self,
        *,
        token_id: str,
        notional_usd: Decimal,
        price: float,
        is_maker: bool,
    ) -> Decimal:
        return self._fees.estimate_fee_usd(
            notional_usd=notional_usd,
            category=self._token_category.get(token_id),
            is_maker=is_maker,
            price=price,
            us_mode=self._us_mode,
        )

    async def submit(self, decision: TradeDecision) -> Order:
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            ts_created=decision.ts,
            strategy=decision.strategy,
            token_id=decision.token_id,
            side=decision.side,
            order_type=decision.order_type,
            price=decision.price,
            size=decision.size,
            status=OrderStatus.PENDING,
            mode=Mode.PAPER,
        )

        async with self._lock:
            self._orders[order_id] = order

            if decision.order_type in (OrderType.FOK, OrderType.FAK):
                await self._fill_taker(order, all_or_nothing=decision.order_type == OrderType.FOK)
            else:
                await self._post_maker(order)

        log.info(
            "paper.submit",
            order_id=order_id,
            side=str(decision.side),
            token_id=decision.token_id,
            price=str(decision.price),
            size=str(decision.size),
            order_type=str(decision.order_type),
            status=str(order.status),
        )
        return self._orders[order_id]

    async def cancel(self, order_id: str) -> bool:
        async with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return False
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return False
            order.status = OrderStatus.CANCELLED
            self._resting.pop(order_id, None)
            log.info("paper.cancel", order_id=order_id)
            return True

    async def reconcile(self) -> None:
        """Walk resting orders, try to fill any that the current book crosses.

        Called periodically by the main loop (e.g. every tick).
        """
        async with self._lock:
            stale: list[str] = []
            for order_id, resting in list(self._resting.items()):
                book = self._books.get(resting.order.token_id)
                if book is None:
                    continue

                # Cancel after maker timeout (Phase 2 simplification — Phase 3
                # adds the §16.1 "partially taker after T sec" path).
                age = (datetime.now(UTC) - resting.submitted_at).total_seconds()
                if age > self._maker_timeout_sec:
                    resting.order.status = OrderStatus.CANCELLED
                    stale.append(order_id)
                    log.info("paper.maker_timeout", order_id=order_id, age=age)
                    continue

                if self._can_cross(resting.order, book):
                    await self._fill_maker_from_book(resting, book)
                    if resting.remaining <= 0:
                        stale.append(order_id)

            for sid in stale:
                self._resting.pop(sid, None)

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def open_orders(self) -> list[Order]:
        return [
            o for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIAL)
        ]

    # ---------- internals ----------

    async def _post_maker(self, order: Order) -> None:
        book = self._books.get(order.token_id)
        if book is None or order.price is None:
            order.status = OrderStatus.REJECTED
            return

        # Cross-the-book check — immediately fill if our price crosses.
        if self._can_cross(order, book):
            resting = _ResingOrder(order)
            await self._fill_maker_from_book(resting, book)
            if resting.remaining > 0:
                order.status = OrderStatus.PARTIAL
                self._resting[order.order_id] = resting
            return

        order.status = OrderStatus.LIVE
        self._resting[order.order_id] = _ResingOrder(order)

    def _can_cross(self, order: Order, book: LocalBook) -> bool:
        if order.price is None:
            return False
        if order.side == Side.BUY:
            best_ask = book.best_ask()
            return best_ask is not None and order.price >= best_ask[0]
        best_bid = book.best_bid()
        return best_bid is not None and order.price <= best_bid[0]

    async def _fill_maker_from_book(self, resting: _ResingOrder, book: LocalBook) -> None:
        order = resting.order
        vwap, filled = walk_book_for_fill(book, str(order.side), resting.remaining)
        if vwap is None or filled <= 0:
            return
        await self._apply_fill(order, vwap, filled)
        resting.remaining -= filled

    async def _fill_taker(self, order: Order, *, all_or_nothing: bool) -> None:
        book = self._books.get(order.token_id)
        if book is None:
            order.status = OrderStatus.REJECTED
            return
        vwap, filled = walk_book_for_fill(book, str(order.side), order.size)
        if vwap is None or filled <= 0:
            order.status = OrderStatus.REJECTED
            return
        if all_or_nothing and filled < order.size:
            # FOK: must fully fill or cancel.
            order.status = OrderStatus.CANCELLED
            log.info("paper.fok_unfilled", order_id=order.order_id, filled=str(filled), need=str(order.size))
            return
        await self._apply_fill(order, vwap, filled)

    async def _apply_fill(self, order: Order, vwap: Decimal, filled: Decimal) -> None:
        total_filled = order.filled_size + filled
        if order.avg_fill_price is None:
            order.avg_fill_price = vwap
        else:
            # Combined VWAP across this and prior fills.
            prior_cost = order.avg_fill_price * order.filled_size
            order.avg_fill_price = (
                (prior_cost + vwap * filled) / total_filled
            ).quantize(Decimal("0.0001"))
        order.filled_size = total_filled
        order.ts_filled = datetime.now(UTC)
        order.status = (
            OrderStatus.FILLED if total_filled >= order.size else OrderStatus.PARTIAL
        )
        # Per-category fee (§2.2). Maker fills are at limit price (free),
        # taker fills paid VWAP. We approximate is_maker by order_type:
        # GTC/GTD resting and crossing at our limit are maker, FOK/FAK taker.
        is_maker = order.order_type.name in ("GTC", "GTD")
        notional = vwap * filled
        fee = self.estimate_fill_fee(
            token_id=order.token_id,
            notional_usd=notional,
            price=float(vwap),
            is_maker=is_maker,
        )
        log.info(
            "paper.fill",
            order_id=order.order_id,
            side=str(order.side),
            vwap=str(vwap),
            filled=str(filled),
            cumulative=str(total_filled),
            fee_usd=str(fee),
            is_maker=is_maker,
            status=str(order.status),
        )
        if self._on_fill is not None and order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL):
            try:
                # Pass the just-applied fill + fee so the ledger can record them.
                await self._on_fill(order, filled, vwap, fee)  # type: ignore[misc]
            except TypeError:
                # Backwards-compat: callers wired before the fee-aware change
                try:
                    await self._on_fill(order)  # type: ignore[misc]
                except Exception as exc:
                    log.warning("paper.on_fill_error", error=str(exc), order_id=order.order_id)
            except Exception as exc:
                log.warning("paper.on_fill_error", error=str(exc), order_id=order.order_id)
