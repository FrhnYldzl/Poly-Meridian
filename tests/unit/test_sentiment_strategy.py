"""SentimentStrategy behavior — high-impact, directional positions."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from poly_meridian.domain import Action, Features, Market
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.sentiment import SentimentStrategy


def _market() -> Market:
    return Market(
        condition_id="0xs",
        question="q",
        category="Politics",
        yes_token_id="yes",
        no_token_id="no",
    )


def _book(token: str, ask: str = "0.50") -> LocalBook:
    b = LocalBook(token_id=token)
    b.apply_snapshot({
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": ask, "size": "100"}],
    })
    return b


@pytest.mark.asyncio
async def test_no_signal_when_disabled() -> None:
    s = SentimentStrategy({"enabled": False})
    s.attach_book("yes", _book("yes"))
    s.attach_recent_signals("0xs", [{"sentiment": 1.0, "impact": 0.9, "direction": "YES"}])
    assert await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={})) is None


@pytest.mark.asyncio
async def test_no_signal_when_impact_below_threshold() -> None:
    s = SentimentStrategy({"enabled": True, "impact_threshold": 0.6})
    s.attach_book("yes", _book("yes"))
    s.attach_recent_signals("0xs", [{"sentiment": 1.0, "impact": 0.3, "direction": "YES"}])
    assert await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={})) is None


@pytest.mark.asyncio
async def test_yes_signal_when_strong_positive() -> None:
    s = SentimentStrategy({"enabled": True, "impact_threshold": 0.5})
    s.attach_book("yes", _book("yes", ask="0.50"))
    s.attach_recent_signals("0xs", [
        {"sentiment": 0.8, "impact": 0.7, "direction": "YES"},
    ])
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is not None
    assert sig.suggested_action == Action.BUY_YES
    assert sig.conviction > 0
    assert sig.edge > 0


@pytest.mark.asyncio
async def test_no_side_signal_when_strong_negative() -> None:
    s = SentimentStrategy({"enabled": True, "impact_threshold": 0.5})
    s.attach_book("no", _book("no", ask="0.50"))
    s.attach_recent_signals("0xs", [
        {"sentiment": -0.8, "impact": 0.7, "direction": "NO"},
    ])
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is not None
    assert sig.suggested_action == Action.BUY_NO


@pytest.mark.asyncio
async def test_no_signal_when_winning_direction_neutral() -> None:
    s = SentimentStrategy({"enabled": True, "impact_threshold": 0.5})
    s.attach_book("yes", _book("yes"))
    s.attach_recent_signals("0xs", [
        {"sentiment": 0.0, "impact": 0.7, "direction": "NEUTRAL"},
    ])
    assert await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={})) is None
