"""FundamentalsStrategy dispatch + signal emission."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from poly_meridian.domain import Action, Features, Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.fundamentals import FundamentalsStrategy


@dataclass
class _FixedResolver(CategoryResolver):
    """Returns a constant probability — for unit testing the strategy logic."""

    category: str
    p_yes: float
    confidence: float = 0.9

    def resolve(self, market: Market, ctx: FundamentalsContext) -> ProbabilityEstimate | None:
        return ProbabilityEstimate(
            p_yes=self.p_yes,
            confidence=self.confidence,
            rationale={"category": self.category, "test": True},
        )


def _market(cat: str = "Politics") -> Market:
    return Market(condition_id="0xq", question="q", category=cat,
                  yes_token_id="yes", no_token_id="no")


def _book(token: str, ask: str = "0.50") -> LocalBook:
    b = LocalBook(token_id=token)
    b.apply_snapshot({
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": ask, "size": "100"}],
    })
    return b


@pytest.mark.asyncio
async def test_disabled_returns_none() -> None:
    s = FundamentalsStrategy({"enabled": False})
    s.attach_book("yes", _book("yes"))
    assert await s.evaluate(_market(), Features(ts=datetime.now(UTC), token_id="yes", values={})) is None


@pytest.mark.asyncio
async def test_emits_yes_signal_when_our_p_above_market() -> None:
    resolver = _FixedResolver(category="Politics", p_yes=0.80)
    s = FundamentalsStrategy({"enabled": True}, resolvers={"Politics": resolver})
    s.attach_book("yes", _book("yes", ask="0.50"))
    s.attach_book("no", _book("no"))
    sig = await s.evaluate(_market("Politics"),
                            Features(ts=datetime.now(UTC), token_id="yes", values={}))
    assert sig is not None
    assert sig.suggested_action == Action.BUY_YES
    assert sig.edge > 0.20
    assert sig.rationale["category"] == "Politics"


@pytest.mark.asyncio
async def test_emits_no_signal_when_our_p_below_market() -> None:
    resolver = _FixedResolver(category="Politics", p_yes=0.20)
    s = FundamentalsStrategy({"enabled": True}, resolvers={"Politics": resolver})
    s.attach_book("yes", _book("yes", ask="0.50"))
    s.attach_book("no", _book("no", ask="0.50"))
    sig = await s.evaluate(_market("Politics"),
                            Features(ts=datetime.now(UTC), token_id="yes", values={}))
    assert sig is not None
    assert sig.suggested_action == Action.BUY_NO


@pytest.mark.asyncio
async def test_no_signal_when_edge_below_threshold() -> None:
    resolver = _FixedResolver(category="Politics", p_yes=0.51)
    s = FundamentalsStrategy({"enabled": True, "min_edge": 0.05},
                              resolvers={"Politics": resolver})
    s.attach_book("yes", _book("yes", ask="0.50"))
    s.attach_book("no", _book("no"))
    sig = await s.evaluate(_market("Politics"),
                            Features(ts=datetime.now(UTC), token_id="yes", values={}))
    assert sig is None  # edge=0.01 < threshold


@pytest.mark.asyncio
async def test_no_signal_when_resolver_confidence_low() -> None:
    resolver = _FixedResolver(category="Politics", p_yes=0.80, confidence=0.2)
    s = FundamentalsStrategy({"enabled": True, "min_confidence": 0.5},
                              resolvers={"Politics": resolver})
    s.attach_book("yes", _book("yes", ask="0.50"))
    s.attach_book("no", _book("no"))
    sig = await s.evaluate(_market("Politics"),
                            Features(ts=datetime.now(UTC), token_id="yes", values={}))
    assert sig is None  # confidence 0.2 below 0.5 threshold


@pytest.mark.asyncio
async def test_unknown_category_returns_none() -> None:
    s = FundamentalsStrategy({"enabled": True})
    s.attach_book("yes", _book("yes"))
    sig = await s.evaluate(_market("UnknownCat"),
                            Features(ts=datetime.now(UTC), token_id="yes", values={}))
    assert sig is None
