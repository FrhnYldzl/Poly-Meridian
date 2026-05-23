"""Walk-forward multi-fold runner — aggregate metrics across folds."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poly_meridian.backtest import (
    HistoricalDataset,
    HistoricalTick,
    ReplayConfig,
    make_folds,
    run_folds,
)
from poly_meridian.domain import Market
from poly_meridian.risk import DefaultRiskPolicy, RiskLimits
from poly_meridian.strategies import ArbitrageStrategy, SignalAggregator


def _arb_dataset() -> tuple[HistoricalDataset, Market]:
    market = Market(
        condition_id="0xab",
        question="q",
        category="Politics",
        yes_token_id="yes",
        no_token_id="no",
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ticks: list[HistoricalTick] = []
    for i in range(100):
        ts = base + timedelta(hours=i)
        for token in (market.yes_token_id, market.no_token_id):
            ticks.append(
                HistoricalTick(
                    ts=ts, token_id=token,
                    bids=[(0.30, 100.0)], asks=[(0.40, 100.0)],
                )
            )
    return HistoricalDataset(markets=[market], ticks=ticks), market


def _factory() -> tuple[list, SignalAggregator, DefaultRiskPolicy]:
    strat = ArbitrageStrategy({
        "enabled": True, "imbalance_threshold": 0.01,
        "min_edge_after_fees_bps": 0, "default_taker_fee_bps": 100,
        "max_size_pct": 0.04,
    })
    risk = DefaultRiskPolicy(
        strategy_name="wf",
        limits=RiskLimits(
            min_market_liquidity_usd=0,
            max_total_exposure_pct=0.99,
            max_exposure_per_category_pct=0.99,
        ),
    )
    return [strat], SignalAggregator(max_size_pct_per_position=0.05), risk


@pytest.mark.asyncio
async def test_run_folds_produces_per_fold_results() -> None:
    ds, _ = _arb_dataset()
    folds = make_folds(
        start=ds.ticks[0].ts,
        end=ds.ticks[-1].ts + timedelta(hours=1),
        train_days=1,
        test_days=1,
    )
    # Need at least one fold for the test.
    assert len(folds) >= 1
    result = await run_folds(
        dataset=ds,
        folds=folds,
        strategy_factory=_factory,
        config=ReplayConfig(starting_nav_usd=Decimal("10000"), tick_interval_sec=3600),
    )
    assert len(result.folds) == len(folds)
    for fr in result.folds:
        assert fr.metrics is not None
        assert fr.final_nav > 0


@pytest.mark.asyncio
async def test_aggregate_metrics_summarizes_folds() -> None:
    ds, _ = _arb_dataset()
    folds = make_folds(
        start=ds.ticks[0].ts,
        end=ds.ticks[-1].ts + timedelta(hours=1),
        train_days=1, test_days=1,
    )
    result = await run_folds(
        dataset=ds, folds=folds, strategy_factory=_factory,
        config=ReplayConfig(starting_nav_usd=Decimal("10000"), tick_interval_sec=3600),
    )
    if not result.folds:
        return
    agg = result.aggregate_metrics()
    assert "mean_sharpe" in agg
    assert "worst_max_drawdown" in agg
    assert agg["n_folds"] == len(result.folds)
