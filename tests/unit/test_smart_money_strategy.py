"""SmartMoneyStrategy — cluster detection logic."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poly_meridian.domain import Action, Features, Market
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.smart_money import (
    ClusterState,
    SmartMoneyStrategy,
    WalletFlow,
)


def _market() -> Market:
    return Market(
        condition_id="0xs",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
    )


def _book(token: str) -> LocalBook:
    b = LocalBook(token_id=token)
    b.apply_snapshot({
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.50", "size": "100"}],
    })
    return b


def _features() -> Features:
    return Features(ts=datetime(2026, 5, 23, tzinfo=UTC), token_id="yes", values={})


@pytest.mark.asyncio
async def test_no_signal_when_disabled() -> None:
    s = SmartMoneyStrategy({"enabled": False, "min_wallet_count": 3})
    s.attach_book("yes", _book("yes"))
    assert await s.evaluate(_market(), _features()) is None


@pytest.mark.asyncio
async def test_no_signal_below_min_wallet_count() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "min_wallet_count": 3,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    now = datetime.now(UTC)
    s.attach_cluster_state(ClusterState(
        condition_id="0xs",
        yes_flows=[
            WalletFlow(wallet="0xa", direction="YES", net_usd=5_000, last_update=now),
            WalletFlow(wallet="0xb", direction="YES", net_usd=5_000, last_update=now),
        ],
    ))
    assert await s.evaluate(_market(), _features()) is None


@pytest.mark.asyncio
async def test_yes_signal_on_clear_yes_cluster() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "min_wallet_count": 3,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    now = datetime.now(UTC)
    s.attach_cluster_state(ClusterState(
        condition_id="0xs",
        yes_flows=[
            WalletFlow("0xa", "YES", 5_000, now),
            WalletFlow("0xb", "YES", 6_000, now),
            WalletFlow("0xc", "YES", 7_000, now),
            WalletFlow("0xd", "YES", 5_500, now),
        ],
    ))
    sig = await s.evaluate(_market(), _features())
    assert sig is not None
    assert sig.suggested_action == Action.BUY_YES
    assert sig.rationale["cluster_size"] == 4
    assert sig.conviction > 0.5


@pytest.mark.asyncio
async def test_no_signal_when_stale() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "min_wallet_count": 3,
        "min_net_usd_per_wallet": 1_000,
        "freshness_max_sec": 60,
    })
    s.attach_book("yes", _book("yes"))
    stale = datetime.now(UTC) - timedelta(seconds=120)
    s.attach_cluster_state(ClusterState(
        condition_id="0xs",
        yes_flows=[
            WalletFlow("0xa", "YES", 5_000, stale),
            WalletFlow("0xb", "YES", 5_000, stale),
            WalletFlow("0xc", "YES", 5_000, stale),
        ],
        last_update=stale,
    ))
    assert await s.evaluate(_market(), _features()) is None


@pytest.mark.asyncio
async def test_below_min_net_usd_does_not_qualify_wallet() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "min_wallet_count": 3,
        "min_net_usd_per_wallet": 10_000,
    })
    s.attach_book("yes", _book("yes"))
    now = datetime.now(UTC)
    s.attach_cluster_state(ClusterState(
        condition_id="0xs",
        yes_flows=[
            WalletFlow("0xa", "YES", 5_000, now),
            WalletFlow("0xb", "YES", 5_000, now),
            WalletFlow("0xc", "YES", 5_000, now),
        ],
    ))
    # Each wallet < min_net_usd, so cluster size by qualifying = 0.
    assert await s.evaluate(_market(), _features()) is None
