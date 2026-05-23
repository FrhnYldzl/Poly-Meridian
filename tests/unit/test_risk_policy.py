"""DefaultRiskPolicy — evaluation + sizing."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poly_meridian.domain import Action, AggregatedSignal, PortfolioSnapshot
from poly_meridian.risk import DefaultRiskPolicy, RiskDecision, RiskLimits


def _signal(
    *,
    size_pct: float = 0.04,
    direction: Action = Action.BUY_YES,
    category: str | None = "Politics",
    proposed_price: str = "0.45",
    market_liquidity_usd: float | None = 100_000.0,
) -> AggregatedSignal:
    return AggregatedSignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        condition_id="0xcond",
        token_id="tok-yes",
        direction=direction,
        edge=0.20,
        conviction=0.85,
        size_pct=size_pct,
        proposed_price=Decimal(proposed_price),
        category=category,
        market_liquidity_usd=market_liquidity_usd,
        contributors=["arbitrage"],
    )


def _portfolio(
    *,
    daily_pnl_pct: float = 0.0,
    total_exposure_pct: float = 0.0,
    category_exposure: dict[str, float] | None = None,
    open_positions: int = 0,
    nav_usd: str = "100000",
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        nav_usd=Decimal(nav_usd),
        cash_usd=Decimal(nav_usd),
        positions=[],
        daily_pnl_pct=daily_pnl_pct,
        total_exposure_pct=total_exposure_pct,
        category_exposure_pct=category_exposure or {},
        open_position_count=open_positions,
    )


def test_approves_clean_signal() -> None:
    p = DefaultRiskPolicy(strategy_name="arbitrage")
    decision = p.evaluate(_signal(), _portfolio())
    assert decision == RiskDecision.APPROVE


def test_rejects_when_kill_switch_engaged() -> None:
    p = DefaultRiskPolicy(strategy_name="arbitrage")
    p.kill_switch.manual_engage("test")
    assert p.is_kill_switch_engaged() is True
    decision = p.evaluate(_signal(), _portfolio())
    assert decision == RiskDecision.REJECT


def test_rejects_when_daily_loss_breached() -> None:
    p = DefaultRiskPolicy(
        strategy_name="arbitrage",
        limits=RiskLimits(daily_max_loss_pct=0.05),
    )
    decision = p.evaluate(_signal(), _portfolio(daily_pnl_pct=-0.10))
    assert decision == RiskDecision.REJECT
    assert p.is_kill_switch_engaged() is True   # daily loss observer also flips switch


def test_rejects_when_liquidity_below_min() -> None:
    p = DefaultRiskPolicy(
        strategy_name="arbitrage",
        limits=RiskLimits(min_market_liquidity_usd=50_000),
    )
    decision = p.evaluate(
        _signal(market_liquidity_usd=10_000),
        _portfolio(),
    )
    assert decision == RiskDecision.REJECT


def test_rejects_when_too_many_open_positions() -> None:
    p = DefaultRiskPolicy(
        strategy_name="arbitrage",
        limits=RiskLimits(max_open_positions=5),
    )
    decision = p.evaluate(_signal(), _portfolio(open_positions=5))
    assert decision == RiskDecision.REJECT


def test_reduces_when_total_exposure_would_breach() -> None:
    p = DefaultRiskPolicy(
        strategy_name="arbitrage",
        limits=RiskLimits(max_total_exposure_pct=0.50, max_position_pct_of_bankroll=0.05),
    )
    decision = p.evaluate(_signal(size_pct=0.04), _portfolio(total_exposure_pct=0.48))
    assert decision == RiskDecision.REDUCE


def test_rejects_non_buy_direction() -> None:
    p = DefaultRiskPolicy(strategy_name="arbitrage")
    decision = p.evaluate(_signal(direction=Action.SELL), _portfolio())
    assert decision == RiskDecision.REJECT


def test_size_produces_trade_decision() -> None:
    p = DefaultRiskPolicy(strategy_name="arbitrage")
    sig = _signal(size_pct=0.02, proposed_price="0.40")
    port = _portfolio(nav_usd="100000")
    assert p.evaluate(sig, port) == RiskDecision.APPROVE
    td = p.size(sig, port)
    assert td is not None
    # size_pct=0.02 capped by hard cap (0.05) → 0.02 actual; $2000 / $0.40 = 5000 units
    assert td.price == Decimal("0.40")
    assert td.size == pytest.approx(Decimal("5000.00"))


def test_size_rejects_missing_price() -> None:
    p = DefaultRiskPolicy(strategy_name="arbitrage")
    sig = AggregatedSignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        condition_id="x",
        token_id="t",
        direction=Action.BUY_YES,
        edge=0.1,
        conviction=0.9,
        size_pct=0.02,
        proposed_price=None,
        category="Politics",
        market_liquidity_usd=50_000.0,
    )
    decision = p.evaluate(sig, _portfolio())
    assert decision == RiskDecision.REJECT
