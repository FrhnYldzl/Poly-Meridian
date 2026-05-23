"""SignalAggregator behaviour."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poly_meridian.domain import Action, Market, StrategySignal
from poly_meridian.strategies.aggregator import SignalAggregator


def _arb_signal(*, action: Action, conviction: float = 0.95, edge: float = 0.20) -> StrategySignal:
    return StrategySignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arbitrage",
        condition_id="0xcond",
        token_id="yes",
        edge=edge,
        conviction=conviction,
        suggested_action=action,
        rationale={
            "yes_ask": 0.40,
            "no_ask": 0.40,
            "total_ask": 0.80,
            "raw_edge": 0.20,
            "depth_min_units": 100.0,
            "partner_token": "no",
        },
    )


def _market() -> Market:
    return Market(
        condition_id="0xcond",
        question="q",
        category="Politics",
        yes_token_id="yes",
        no_token_id="no",
        liquidity_usd=Decimal("50000"),
    )


def test_single_signal_passes_through() -> None:
    agg = SignalAggregator()
    res = agg.aggregate([_arb_signal(action=Action.BUY_YES)], market=_market(), bankroll_usd=Decimal("100000"))
    assert res is not None
    assert res.direction == Action.BUY_YES
    assert res.contributors == ["arbitrage"]
    assert res.proposed_price == Decimal("0.40")
    assert res.size_pct > 0


def test_returns_none_on_empty() -> None:
    agg = SignalAggregator()
    assert agg.aggregate([], market=_market()) is None


def test_conflict_returns_none() -> None:
    agg = SignalAggregator(conflict_threshold=0.10)
    # Two strategies, opposite directions, ~equal conviction → conflict
    a = _arb_signal(action=Action.BUY_YES, conviction=0.90)
    b = _arb_signal(action=Action.BUY_NO, conviction=0.85)
    res = agg.aggregate([a, b], market=_market(), bankroll_usd=Decimal("100000"))
    assert res is None


def test_clear_winner_wins() -> None:
    agg = SignalAggregator(conflict_threshold=0.10)
    a = _arb_signal(action=Action.BUY_YES, conviction=0.95)
    b = _arb_signal(action=Action.BUY_NO, conviction=0.50)
    res = agg.aggregate([a, b], market=_market(), bankroll_usd=Decimal("100000"))
    assert res is not None
    assert res.direction == Action.BUY_YES


def test_market_metadata_attached() -> None:
    agg = SignalAggregator()
    res = agg.aggregate([_arb_signal(action=Action.BUY_YES)], market=_market(), bankroll_usd=Decimal("100000"))
    assert res is not None
    assert res.category == "Politics"
    assert res.market_liquidity_usd == 50_000.0
