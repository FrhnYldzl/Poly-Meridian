"""ArbitrageStrategy detection logic."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poly_meridian.domain import Features, Market
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.arbitrage import ArbitrageStrategy


def _market() -> Market:
    return Market(
        condition_id="0xcond",
        question="q",
        category="Politics",
        yes_token_id="yes",
        no_token_id="no",
        active=True,
    )


def _book(token_id: str, asks: list[tuple[str, str]], bids: list[tuple[str, str]] | None = None) -> LocalBook:
    b = LocalBook(token_id=token_id)
    b.apply_snapshot({
        "bids": [{"price": p, "size": s} for p, s in (bids or [("0.01", "1")])],
        "asks": [{"price": p, "size": s} for p, s in asks],
    })
    return b


@pytest.mark.asyncio
async def test_detects_arbitrage_when_asks_sum_below_one() -> None:
    s = ArbitrageStrategy({"enabled": True, "imbalance_threshold": 0.01, "min_edge_after_fees_bps": 0})
    s.attach_book("yes", _book("yes", asks=[("0.40", "1000")]))
    s.attach_book("no",  _book("no",  asks=[("0.40", "1000")]))
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is not None
    assert sig.strategy == "arbitrage"
    assert sig.rationale["total_ask"] == pytest.approx(0.80)
    assert sig.rationale["raw_edge"] == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_no_signal_when_asks_sum_near_one() -> None:
    s = ArbitrageStrategy({"enabled": True, "imbalance_threshold": 0.015})
    s.attach_book("yes", _book("yes", asks=[("0.50", "1000")]))
    s.attach_book("no",  _book("no",  asks=[("0.50", "1000")]))
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is None


@pytest.mark.asyncio
async def test_no_signal_when_disabled() -> None:
    s = ArbitrageStrategy({"enabled": False})
    s.attach_book("yes", _book("yes", asks=[("0.30", "1000")]))
    s.attach_book("no",  _book("no",  asks=[("0.30", "1000")]))
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is None


@pytest.mark.asyncio
async def test_no_signal_when_book_missing() -> None:
    s = ArbitrageStrategy({"enabled": True})
    s.attach_book("yes", _book("yes", asks=[("0.30", "1000")]))
    # No book for NO leg.
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is None


@pytest.mark.asyncio
async def test_fees_can_kill_thin_arb() -> None:
    # raw_edge = 0.02 (2%) but fees = 2 * 180bps = 360bps → net edge < 0
    s = ArbitrageStrategy({
        "enabled": True,
        "imbalance_threshold": 0.01,
        "min_edge_after_fees_bps": 30,
        "default_taker_fee_bps": 180,
    })
    s.attach_book("yes", _book("yes", asks=[("0.49", "1000")]))
    s.attach_book("no",  _book("no",  asks=[("0.49", "1000")]))
    sig = await s.evaluate(_market(), Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={}))
    assert sig is None


def test_proposed_size_pct_respects_depth_and_cap() -> None:
    rationale = {"yes_ask": 0.40, "depth_min_units": 100.0}
    # depth_usd = 100 * 0.40 = $40 → 0.4% of $10K. Cap 0.05 → still 0.4%.
    pct = ArbitrageStrategy.proposed_size_pct(rationale, Decimal("10000"), 0.05)
    assert pct == pytest.approx(0.004)

    # Bigger depth than cap allows → capped at 0.05.
    rationale_big = {"yes_ask": 0.40, "depth_min_units": 100_000.0}
    pct_big = ArbitrageStrategy.proposed_size_pct(rationale_big, Decimal("10000"), 0.05)
    assert pct_big == 0.05
