"""Portfolio ledger + P&L math."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poly_meridian.domain import Mode, Order, OrderStatus, OrderType, Side
from poly_meridian.portfolio import Ledger, nav_usd, realized_pnl, snapshot, unrealized_pnl


def _order(side: Side, qty: str, price: str) -> Order:
    return Order(
        order_id=f"o-{qty}-{price}",
        ts_created=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arb",
        token_id="tok",
        side=side,
        order_type=OrderType.FAK,
        price=Decimal(price),
        size=Decimal(qty),
        filled_size=Decimal(qty),
        avg_fill_price=Decimal(price),
        status=OrderStatus.FILLED,
        mode=Mode.PAPER,
    )


def test_buy_then_sell_realizes_pnl() -> None:
    led = Ledger(starting_cash_usd=Decimal("10000"))

    led.apply_fill(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        order=_order(Side.BUY, "100", "0.40"),
        filled_qty=Decimal("100"),
        fill_price=Decimal("0.40"),
    )
    assert led.cash == Decimal("9960.00")  # spent $40
    pos = led.get_position("tok")
    assert pos is not None
    assert pos.qty == Decimal("100")
    assert pos.avg_cost == Decimal("0.4000")

    led.apply_fill(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        order=_order(Side.SELL, "100", "0.55"),
        filled_qty=Decimal("100"),
        fill_price=Decimal("0.55"),
    )
    assert led.cash == Decimal("10015.00")  # +$55 - $40 = +$15 net
    # Position is closed; realized pnl == $15 ((0.55 - 0.40) * 100)
    assert realized_pnl(led) == Decimal("15.00")


def test_unrealized_pnl_tracks_mark() -> None:
    led = Ledger(starting_cash_usd=Decimal("10000"))
    led.apply_fill(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        order=_order(Side.BUY, "100", "0.40"),
        filled_qty=Decimal("100"),
        fill_price=Decimal("0.40"),
    )
    # No mark update yet → last_mark = fill_price → unrealized = 0
    assert unrealized_pnl(led) == Decimal("0.00")

    led.mark("tok", Decimal("0.50"))
    # 100 * (0.50 - 0.40) = $10
    assert unrealized_pnl(led) == Decimal("10.00")
    assert nav_usd(led) == Decimal("9970.00")   # $9960 cash + 100*$0.50 marks = $10010 — wait
    # cash = 9960, position value = 100 * 0.50 = 50, total = 10010
    assert nav_usd(led) == Decimal("10010.00")


def test_snapshot_has_exposure_breakdowns() -> None:
    led = Ledger(starting_cash_usd=Decimal("10000"))
    led.apply_fill(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        order=_order(Side.BUY, "100", "0.40"),
        filled_qty=Decimal("100"),
        fill_price=Decimal("0.40"),
    )
    led.mark("tok", Decimal("0.50"))

    snap = snapshot(led, token_to_category={"tok": "Politics"})
    assert snap.nav_usd == Decimal("10010.00")
    assert snap.cash_usd == Decimal("9960.00")
    assert snap.open_position_count == 1
    # Exposure = 100 * 0.50 = $50, NAV = $10010 → ~0.5%
    assert snap.total_exposure_pct == pytest.approx(50 / 10010, abs=1e-4)
    assert "Politics" in snap.category_exposure_pct


def test_avg_cost_weighted_across_buys() -> None:
    led = Ledger(starting_cash_usd=Decimal("10000"))
    led.apply_fill(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        order=_order(Side.BUY, "100", "0.40"),
        filled_qty=Decimal("100"),
        fill_price=Decimal("0.40"),
    )
    led.apply_fill(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        order=_order(Side.BUY, "100", "0.50"),
        filled_qty=Decimal("100"),
        fill_price=Decimal("0.50"),
    )
    pos = led.get_position("tok")
    assert pos is not None
    assert pos.qty == Decimal("200")
    # weighted avg = (100*0.40 + 100*0.50) / 200 = 0.45
    assert pos.avg_cost == Decimal("0.4500")
