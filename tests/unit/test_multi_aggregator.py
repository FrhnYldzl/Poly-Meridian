"""Multi-strategy aggregator behavior."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from poly_meridian.domain import Action, Market, StrategySignal
from poly_meridian.strategies.aggregator import SignalAggregator


def _arb_sig(action: Action = Action.BUY_YES, conviction: float = 0.95) -> StrategySignal:
    return StrategySignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="arbitrage",
        condition_id="0xs",
        token_id="yes",
        edge=0.10,
        conviction=conviction,
        suggested_action=action,
        rationale={
            "yes_ask": 0.40, "no_ask": 0.40, "total_ask": 0.80, "raw_edge": 0.20,
            "depth_min_units": 200.0, "partner_token": "no",
        },
    )


def _sent_sig(action: Action = Action.BUY_YES, conviction: float = 0.6) -> StrategySignal:
    return StrategySignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="sentiment",
        condition_id="0xs",
        token_id="yes",
        edge=0.05,
        conviction=conviction,
        suggested_action=action,
        rationale={"best_ask": 0.45, "impact_max": 0.8, "our_p": 0.55},
    )


def _sm_sig(action: Action = Action.BUY_YES, conviction: float = 0.7) -> StrategySignal:
    return StrategySignal(
        ts=datetime(2026, 5, 23, tzinfo=UTC),
        strategy="smart_money",
        condition_id="0xs",
        token_id="yes",
        edge=0.08,
        conviction=conviction,
        suggested_action=action,
        rationale={"best_ask": 0.42, "cluster_size": 4, "net_usd_total": 50_000, "our_p": 0.55},
    )


def _market() -> Market:
    return Market(
        condition_id="0xs",
        question="q",
        category="Politics",
        yes_token_id="yes",
        no_token_id="no",
        liquidity_usd=Decimal("50000"),
    )


def test_three_strategies_same_direction_aggregate() -> None:
    agg = SignalAggregator()
    res = agg.aggregate(
        [_arb_sig(), _sent_sig(), _sm_sig()],
        market=_market(),
        bankroll_usd=Decimal("100000"),
    )
    assert res is not None
    assert res.direction == Action.BUY_YES
    assert set(res.contributors) == {"arbitrage", "sentiment", "smart_money"}
    # proposed_price = max across helpers — arbitrage uses yes_ask=0.40, sentiment best_ask=0.45,
    # smart_money best_ask=0.42; max = 0.45
    assert res.proposed_price == Decimal("0.45")


def test_strong_one_wins_against_two_opposing_weak() -> None:
    agg = SignalAggregator(conflict_threshold=0.10)
    res = agg.aggregate(
        [
            _arb_sig(action=Action.BUY_YES, conviction=0.95),
            _sent_sig(action=Action.BUY_NO, conviction=0.3),
            _sm_sig(action=Action.BUY_NO, conviction=0.3),
        ],
        market=_market(),
        bankroll_usd=Decimal("100000"),
    )
    assert res is not None
    assert res.direction == Action.BUY_YES


def test_tied_strong_signals_yield_conflict() -> None:
    agg = SignalAggregator(conflict_threshold=0.10)
    res = agg.aggregate(
        [
            _arb_sig(action=Action.BUY_YES, conviction=0.8),
            _sm_sig(action=Action.BUY_NO, conviction=0.8),
        ],
        market=_market(),
        bankroll_usd=Decimal("100000"),
    )
    assert res is None


def test_size_pct_clamped_by_max() -> None:
    agg = SignalAggregator(max_size_pct_per_position=0.05)
    res = agg.aggregate(
        [_arb_sig(), _sent_sig(), _sm_sig()],
        market=_market(),
        bankroll_usd=Decimal("100000"),
    )
    assert res is not None
    assert res.size_pct <= 0.05
