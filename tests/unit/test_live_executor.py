"""LiveExecutor safety + happy-path behavior with a mocked CLOB client."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from poly_meridian.domain import (
    Mode,
    OrderStatus,
    OrderType,
    Side,
    TradeDecision,
)


def _decision(order_type: OrderType = OrderType.GTC) -> TradeDecision:
    return TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="t",
        token_id="yes",
        side=Side.BUY,
        order_type=order_type,
        price=Decimal("0.42"),
        size=Decimal("100"),
    )


class _MockAuthed:
    """Pretends to be a `py-clob-client.ClobClient` instance."""

    def __init__(self) -> None:
        self.created: list[Any] = []
        self.posted: list[tuple[Any, str]] = []
        self.cancels: list[str] = []
        self.opens: list[dict[str, Any]] = []

    def create_order(self, args: Any) -> Any:
        self.created.append(args)
        return {"signed": True, "args": args}

    def create_market_order(self, args: Any) -> Any:
        self.created.append(args)
        return {"signed": True, "market": True, "args": args}

    def post_order(self, signed: Any, order_type: str) -> dict[str, Any]:
        self.posted.append((signed, order_type))
        return {"orderID": f"venue-{len(self.posted)}", "status": "ok"}

    def cancel(self, venue_id: str) -> dict[str, Any]:
        self.cancels.append(venue_id)
        return {"canceled": True}

    def get_orders(self) -> list[dict[str, Any]]:
        return self.opens


class _MockLib:
    class clob_types:
        class OrderArgs:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

        class MarketOrderArgs:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)


class _MockClobClient:
    """Wraps a _MockAuthed inside the surface LiveExecutor expects."""

    def __init__(self) -> None:
        self._authed = _MockAuthed()
        self._lib = _MockLib()

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    def has_authed_client(self) -> bool:
        return True

    def init_authed(self) -> bool:
        return True

    def authed(self) -> _MockAuthed:
        return self._authed


@pytest.fixture
def live_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live-conservative")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    # Clear cached settings.
    from poly_meridian.settings import get_settings
    get_settings.cache_clear()


def test_live_executor_refuses_in_paper_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "paper")
    from poly_meridian.settings import get_settings
    get_settings.cache_clear()
    from poly_meridian.execution.live_executor import LiveExecutor
    with pytest.raises(RuntimeError, match="LiveExecutor refuses"):
        LiveExecutor()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_live_executor_submits_limit_order(live_mode_env: None) -> None:
    from poly_meridian.execution.live_executor import LiveExecutor
    mock_clob = _MockClobClient()
    ex = LiveExecutor(clob=mock_clob)
    order = await ex.submit(_decision(order_type=OrderType.GTC))
    assert order.status == OrderStatus.LIVE
    assert order.mode == Mode.LIVE_CONSERVATIVE
    # Created order + posted once.
    assert len(mock_clob._authed.created) == 1
    assert len(mock_clob._authed.posted) == 1


@pytest.mark.asyncio
async def test_live_executor_submits_market_order(live_mode_env: None) -> None:
    from poly_meridian.execution.live_executor import LiveExecutor
    mock_clob = _MockClobClient()
    ex = LiveExecutor(clob=mock_clob)
    order = await ex.submit(_decision(order_type=OrderType.FAK))
    assert order.status == OrderStatus.LIVE
    assert mock_clob._authed.created[0].__dict__.get("amount") == 100.0


@pytest.mark.asyncio
async def test_live_executor_cancel_path(live_mode_env: None) -> None:
    from poly_meridian.execution.live_executor import LiveExecutor
    mock_clob = _MockClobClient()
    ex = LiveExecutor(clob=mock_clob)
    order = await ex.submit(_decision())
    ok = await ex.cancel(order.order_id)
    assert ok is True
    assert ex.get_order(order.order_id).status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_live_executor_reconcile_marks_filled_when_off_venue(
    live_mode_env: None,
) -> None:
    from poly_meridian.execution.live_executor import LiveExecutor
    mock_clob = _MockClobClient()
    ex = LiveExecutor(clob=mock_clob)
    order = await ex.submit(_decision())
    # Venue says nothing is open → reconcile should mark our order FILLED.
    mock_clob._authed.opens = []
    await ex.reconcile()
    assert ex.get_order(order.order_id).status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_live_executor_handles_submit_error(live_mode_env: None) -> None:
    from poly_meridian.execution.live_executor import LiveExecutor

    class _BrokenAuthed(_MockAuthed):
        def create_order(self, args: Any) -> Any:
            raise RuntimeError("simulated venue rejection")

    mock_clob = _MockClobClient()
    mock_clob._authed = _BrokenAuthed()  # type: ignore[assignment]
    ex = LiveExecutor(clob=mock_clob)
    order = await ex.submit(_decision())
    assert order.status == OrderStatus.REJECTED
