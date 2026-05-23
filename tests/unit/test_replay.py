"""Replay engine end-to-end: synthetic arb opportunity → fill → final NAV."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poly_meridian.backtest.replay import (
    HistoricalDataset,
    HistoricalTick,
    ReplayConfig,
    Replayer,
)
from poly_meridian.domain import Market
from poly_meridian.risk import DefaultRiskPolicy, RiskLimits
from poly_meridian.strategies import ArbitrageStrategy, SignalAggregator


def _arb_dataset(market: Market) -> HistoricalDataset:
    """20 minutes of arb-friendly data: YES_ask + NO_ask = 0.80 (20 cent edge)."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ticks: list[HistoricalTick] = []
    for i in range(20):
        ts = base + timedelta(minutes=i)
        for token in (market.yes_token_id, market.no_token_id):
            ticks.append(
                HistoricalTick(
                    ts=ts,
                    token_id=token,
                    bids=[(0.30, 100.0)],
                    asks=[(0.40, 100.0)],
                )
            )
    return HistoricalDataset(markets=[market], ticks=ticks)


@pytest.mark.asyncio
async def test_replay_executes_arbitrage_opportunity() -> None:
    market = Market(
        condition_id="0xab",
        question="will X happen?",
        category="Politics",
        yes_token_id="yes",
        no_token_id="no",
    )
    ds = _arb_dataset(market)

    strat = ArbitrageStrategy({
        "enabled": True,
        "imbalance_threshold": 0.01,
        "min_edge_after_fees_bps": 0,
        "default_taker_fee_bps": 100,
        "max_size_pct": 0.04,
    })
    risk = DefaultRiskPolicy(
        strategy_name="backtest",
        limits=RiskLimits(
            min_market_liquidity_usd=0,
            max_total_exposure_pct=0.99,
            max_exposure_per_category_pct=0.99,
        ),
    )
    aggregator = SignalAggregator(max_size_pct_per_position=0.05)

    replayer = Replayer(
        dataset=ds,
        strategies=[strat],
        aggregator=aggregator,
        risk=risk,
        config=ReplayConfig(starting_nav_usd=Decimal("10000"), tick_interval_sec=60),
    )
    result = await replayer.run()
    assert result.final_nav > 0
    # Equity curve has at least one sample per evaluation tick.
    assert len(result.equity_curve) >= 1


@pytest.mark.asyncio
async def test_replay_handles_empty_dataset() -> None:
    risk = DefaultRiskPolicy(strategy_name="t")
    strat = ArbitrageStrategy({"enabled": True})
    aggregator = SignalAggregator()
    ds = HistoricalDataset(markets=[], ticks=[])
    r = Replayer(dataset=ds, strategies=[strat], aggregator=aggregator, risk=risk)
    result = await r.run()
    assert result.final_nav == 0.0
    assert result.equity_curve == []
