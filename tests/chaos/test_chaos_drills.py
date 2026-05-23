"""Chaos engineering drills. See MASTER_SPEC §23.

Each drill simulates a failure mode and verifies that:
  - The kill-switch engages when warranted
  - The agent doesn't crash on transient errors
  - The risk gate continues to enforce limits during degradation

These run as pytest tests but the failures are mocked — actual operational
chaos (real network partitions, real DB kills) is documented in
`scripts/dr_drill.py` and run manually during the Phase 6 promotion drill.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from poly_meridian.domain import Action, AggregatedSignal, PortfolioSnapshot
from poly_meridian.ingestion.gamma_client import GammaClient
from poly_meridian.risk import DefaultRiskPolicy, RiskDecision, RiskLimits
from poly_meridian.risk.kill_switch import KillReason, KillSwitch, KillSwitchConfig


@pytest.mark.asyncio
async def test_gamma_client_recovers_from_transient_timeout() -> None:
    """Single timeout, then success — client should retry & succeed."""
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ReadTimeout("simulated", request=req)
        return httpx.Response(200, json=[{"conditionId": "x", "question": "q"}])

    c = GammaClient(base_url="https://test")
    await c.start()
    c._client._transport = httpx.MockTransport(handler)  # type: ignore[reportPrivateUsage]
    rows = await c.list_active_markets()
    await c.stop()
    assert rows == [{"conditionId": "x", "question": "q"}]
    assert call_count == 2     # one retry was used


@pytest.mark.asyncio
async def test_gamma_client_fails_after_max_retries() -> None:
    """Persistent 5xx → exception bubbles up so caller can handle."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    c = GammaClient(base_url="https://test")
    await c.start()
    c._client._transport = httpx.MockTransport(handler)  # type: ignore[reportPrivateUsage]
    with pytest.raises(httpx.HTTPError):
        await c.list_active_markets()
    await c.stop()


def test_kill_switch_engages_on_runaway_api_errors() -> None:
    """20 errors in 20 calls → engage on API_ERROR_RATE."""
    ks = KillSwitch(config=KillSwitchConfig(api_error_rate_threshold=0.10))
    for _ in range(20):
        ks.observe_api_call(ok=False)
    assert ks.engaged is True
    assert ks.reason == KillReason.API_ERROR_RATE


def test_kill_switch_engages_on_ws_disconnect_grace_exceeded() -> None:
    ks = KillSwitch(config=KillSwitchConfig(websocket_disconnect_grace_sec=30))
    ks.observe_ws_disconnect(disconnected_for_sec=120)
    assert ks.engaged is True
    assert ks.reason == KillReason.WS_DISCONNECT


def test_kill_switch_engages_on_wallet_balance_mismatch() -> None:
    """Operator drift between expected and actual wallet balance → engage."""
    ks = KillSwitch()
    ks.observe_wallet_balance(expected_usd=10_000, actual_usd=9_500, tol_usd=100)
    assert ks.engaged is True
    assert ks.reason == KillReason.WALLET_BALANCE_MISMATCH


def test_risk_policy_rejects_every_order_under_kill_switch() -> None:
    """Single source of truth: once kill-switch engaged, NOTHING gets approved."""
    p = DefaultRiskPolicy(strategy_name="chaos", limits=RiskLimits())
    p.kill_switch.manual_engage("chaos drill")

    sig = AggregatedSignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        condition_id="x",
        token_id="t",
        direction=Action.BUY_YES,
        edge=0.20, conviction=0.95,
        size_pct=0.04, proposed_price=Decimal("0.40"),
        category="Politics", market_liquidity_usd=50_000,
    )
    port = PortfolioSnapshot(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        nav_usd=Decimal("100000"), cash_usd=Decimal("100000"),
        positions=[], daily_pnl_pct=0.0,
        total_exposure_pct=0.0, open_position_count=0,
    )
    assert p.evaluate(sig, port) == RiskDecision.REJECT
    assert p.size(sig, port) is not None  # size() still works; evaluate() gates
    # But the contract is: caller MUST check decision before calling size().


@pytest.mark.asyncio
async def test_concurrent_signals_dont_corrupt_kill_switch_state() -> None:
    """Race: 100 daily-loss observations from many tasks — idempotent engage."""
    ks = KillSwitch(config=KillSwitchConfig(daily_loss_trigger_pct=0.05))

    async def observe(value: float) -> None:
        ks.observe_daily_pnl(value)

    await asyncio.gather(*(observe(-0.10) for _ in range(100)))
    assert ks.engaged is True
    assert ks.reason == KillReason.DAILY_LOSS
    # Reason isn't overwritten by other triggers fired concurrently.
    ks.observe_slippage(observed_bps=10_000, token_id="t")
    assert ks.reason == KillReason.DAILY_LOSS


def test_paper_executor_rejects_unknown_token_book() -> None:
    """Defensive: order on a token without an attached book → REJECTED."""
    import uuid

    from poly_meridian.domain import OrderType, Side, TradeDecision
    from poly_meridian.execution.paper_executor import PaperExecutor

    ex = PaperExecutor()
    td = TradeDecision(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="chaos",
        token_id=f"unknown-{uuid.uuid4()}",
        side=Side.BUY,
        order_type=OrderType.FOK,
        price=None, size=Decimal("100"),
    )

    async def go() -> None:
        order = await ex.submit(td)
        from poly_meridian.domain import OrderStatus
        assert order.status == OrderStatus.REJECTED

    asyncio.run(go())
