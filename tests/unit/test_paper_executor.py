"""PaperExecutor — fill simulation."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poly_meridian.domain import OrderStatus, OrderType, Side, TradeDecision
from poly_meridian.execution.paper_executor import PaperExecutor
from poly_meridian.ingestion.book import LocalBook


def _book(asks: list[tuple[str, str]], bids: list[tuple[str, str]] | None = None) -> LocalBook:
    b = LocalBook(token_id="t")
    b.apply_snapshot({
        "bids": [{"price": p, "size": s} for p, s in (bids or [("0.10", "1")])],
        "asks": [{"price": p, "size": s} for p, s in asks],
    })
    return b


@pytest.mark.asyncio
async def test_taker_fill_walks_book() -> None:
    ex = PaperExecutor()
    ex.attach_book("t", _book([("0.40", "100"), ("0.41", "100")]))
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="t",
        side=Side.BUY,
        order_type=OrderType.FAK,
        price=None,
        size=Decimal("150"),
    )
    order = await ex.submit(td)
    assert order.status == OrderStatus.FILLED
    # 100 @ 0.40 + 50 @ 0.41 = 40 + 20.50 = 60.50 / 150 = 0.4033
    assert order.avg_fill_price == pytest.approx(Decimal("0.4033"), abs=Decimal("0.0001"))
    assert order.filled_size == Decimal("150")


@pytest.mark.asyncio
async def test_fok_kills_when_insufficient_depth() -> None:
    ex = PaperExecutor()
    ex.attach_book("t", _book([("0.40", "50")]))
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="t",
        side=Side.BUY,
        order_type=OrderType.FOK,
        price=None,
        size=Decimal("100"),  # more than book depth (50)
    )
    order = await ex.submit(td)
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_size == Decimal(0)


@pytest.mark.asyncio
async def test_maker_above_best_ask_crosses_immediately() -> None:
    ex = PaperExecutor()
    ex.attach_book("t", _book([("0.40", "100")]))
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="t",
        side=Side.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.42"),     # crosses
        size=Decimal("50"),
    )
    order = await ex.submit(td)
    assert order.status == OrderStatus.FILLED
    assert order.avg_fill_price == Decimal("0.4000")


@pytest.mark.asyncio
async def test_maker_below_best_ask_rests() -> None:
    ex = PaperExecutor()
    ex.attach_book("t", _book([("0.40", "100")]))
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="t",
        side=Side.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.38"),     # does NOT cross
        size=Decimal("50"),
    )
    order = await ex.submit(td)
    assert order.status == OrderStatus.LIVE
    assert ex.open_orders() == [order]


@pytest.mark.asyncio
async def test_cancel_resting_order() -> None:
    ex = PaperExecutor()
    ex.attach_book("t", _book([("0.40", "100")]))
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="t",
        side=Side.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.38"),
        size=Decimal("50"),
    )
    order = await ex.submit(td)
    assert order.status == OrderStatus.LIVE
    ok = await ex.cancel(order.order_id)
    assert ok is True
    assert ex.get_order(order.order_id).status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_reconcile_fills_resting_when_book_moves() -> None:
    ex = PaperExecutor()
    book = _book([("0.40", "100")])
    ex.attach_book("t", book)
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="t",
        side=Side.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.42"),     # would normally cross now
        size=Decimal("50"),
    )
    # Trick: post when book is "thin" (no asks), then add depth and reconcile.
    book.asks.clear()
    order = await ex.submit(td)
    assert order.status == OrderStatus.LIVE

    # Now restore asks so the resting order can cross.
    book.apply_snapshot({"bids": [{"price": "0.10", "size": "1"}], "asks": [{"price": "0.40", "size": "100"}]})
    await ex.reconcile()
    assert ex.get_order(order.order_id).status == OrderStatus.FILLED
