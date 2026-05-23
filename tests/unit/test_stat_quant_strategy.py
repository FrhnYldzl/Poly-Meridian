"""StatQuantStrategy sub-signal behavior."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poly_meridian.domain import Action, Features, Market
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.stat_quant import StatQuantStrategy


def _market(end_in_hours: int | None = None) -> Market:
    end = None
    if end_in_hours is not None:
        end = datetime.now(UTC) + timedelta(hours=end_in_hours)
    return Market(
        condition_id="0xq",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
        end_date_iso=end,
    )


def _book(token: str, ask: str = "0.50") -> LocalBook:
    b = LocalBook(token_id=token)
    b.apply_snapshot({
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": ask, "size": "100"}],
    })
    return b


def _features() -> Features:
    return Features(ts=datetime.now(UTC), token_id="yes", values={})


def _push(s: StatQuantStrategy, token: str, prices: list[float]) -> None:
    for p in prices:
        s.push_price(token, p)


@pytest.mark.asyncio
async def test_no_signal_when_disabled() -> None:
    s = StatQuantStrategy({"enabled": False})
    assert await s.evaluate(_market(), _features()) is None


@pytest.mark.asyncio
async def test_mean_reversion_triggers_against_high_zscore() -> None:
    s = StatQuantStrategy({
        "enabled": True,
        "mean_reversion": {"zscore_threshold": 2.0, "min_window": 10},
    })
    s.attach_book("yes", _book("yes"))
    s.attach_book("no", _book("no"))
    _push(s, "yes", [0.50] * 10 + [0.65])  # last value above mean by many sd
    sig = await s.evaluate(_market(), _features())
    assert sig is not None
    # high z → BUY_NO (bet against move)
    assert sig.suggested_action == Action.BUY_NO
    assert "mean_reversion" in sig.strategy


@pytest.mark.asyncio
async def test_momentum_triggers_with_trend() -> None:
    s = StatQuantStrategy({
        "enabled": True,
        "momentum": {"lookback_window": 5, "return_threshold": 0.05},
        "mean_reversion": {"zscore_threshold": 100, "min_window": 10},  # disable
    })
    s.attach_book("yes", _book("yes"))
    _push(s, "yes", [0.40, 0.42, 0.45, 0.48, 0.50])
    sig = await s.evaluate(_market(), _features())
    assert sig is not None
    assert sig.suggested_action == Action.BUY_YES
    assert "momentum" in sig.strategy


@pytest.mark.asyncio
async def test_no_signal_when_window_too_small() -> None:
    s = StatQuantStrategy({"enabled": True, "mean_reversion": {"min_window": 50}})
    s.attach_book("yes", _book("yes"))
    _push(s, "yes", [0.50] * 5)
    assert await s.evaluate(_market(), _features()) is None


@pytest.mark.asyncio
async def test_time_decay_triggers_when_close_to_resolution() -> None:
    s = StatQuantStrategy({
        "enabled": True,
        "mean_reversion": {"zscore_threshold": 100, "min_window": 10},
        "momentum": {"return_threshold": 100, "lookback_window": 5},
        "vol_breakout": {"low_vol_threshold": -1, "breakout_multiplier": 100},
        "time_decay": {
            "horizon_hours_max": 24.0,
            "price_deviation_threshold": 0.10,
        },
    })
    yes_b = LocalBook(token_id="yes")
    yes_b.apply_snapshot({
        "bids": [{"price": "0.65", "size": "100"}],
        "asks": [{"price": "0.70", "size": "100"}],
    })
    no_b = LocalBook(token_id="no")
    no_b.apply_snapshot({
        "bids": [{"price": "0.28", "size": "100"}],
        "asks": [{"price": "0.32", "size": "100"}],
    })
    s.attach_book("yes", yes_b)
    s.attach_book("no", no_b)
    sig = await s.evaluate(_market(end_in_hours=2), _features())
    assert sig is not None
    assert "time_decay" in sig.strategy
    assert sig.suggested_action == Action.BUY_YES
