"""SmartMoneyStrategy v2 — 3-tier behavior."""
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
        condition_id="0xq",
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
    return Features(ts=datetime.now(UTC), token_id="yes", values={})


def _flows(direction: str, count: int, tier: int, net_usd: float = 10_000) -> list[WalletFlow]:
    now = datetime.now(UTC)
    return [
        WalletFlow(
            wallet=f"0x{i:040x}",
            direction=direction,
            net_usd=net_usd,
            last_update=now,
            tier=tier,
        )
        for i in range(count)
    ]


def _attach_tiers(s: SmartMoneyStrategy, flows: list[WalletFlow]) -> None:
    for f in flows:
        s.attach_wallet_tier(f.wallet, f.tier)


@pytest.mark.asyncio
async def test_no_signal_when_disabled() -> None:
    s = SmartMoneyStrategy({"enabled": False})
    s.attach_book("yes", _book("yes"))
    s.attach_cluster_state(ClusterState(
        condition_id="0xq",
        yes_flows=_flows("YES", 5, tier=1),
    ))
    assert await s.evaluate(_market(), _features()) is None


@pytest.mark.asyncio
async def test_tier1_cluster_emits_signal() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    flows = _flows("YES", 4, tier=1)
    _attach_tiers(s, flows)
    s.attach_cluster_state(ClusterState(condition_id="0xq", yes_flows=flows))
    sig = await s.evaluate(_market(), _features())
    assert sig is not None
    assert sig.suggested_action == Action.BUY_YES
    assert sig.rationale["tier"] == 1
    assert "copied_from" in sig.rationale
    assert len(sig.rationale["copied_from"]) == 4


@pytest.mark.asyncio
async def test_tier2_cluster_emits_signal_with_lower_conviction() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    flows = _flows("YES", 3, tier=2)
    _attach_tiers(s, flows)
    s.attach_cluster_state(ClusterState(condition_id="0xq", yes_flows=flows))
    sig = await s.evaluate(_market(), _features())
    assert sig is not None
    assert sig.rationale["tier"] == 2
    # Tier 2 conviction should be lower than Tier 1 in similar cluster size.
    assert sig.conviction < 0.9


@pytest.mark.asyncio
async def test_tier3_default_does_not_auto_trade() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "tier3_auto_trade": False,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    flows = _flows("YES", 4, tier=3)
    _attach_tiers(s, flows)
    s.attach_cluster_state(ClusterState(condition_id="0xq", yes_flows=flows))
    # Tier 3 cluster exists but tier3_auto_trade=false → no signal.
    sig = await s.evaluate(_market(), _features())
    assert sig is None


@pytest.mark.asyncio
async def test_latency_decay_drops_stale_events() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "latency_decay_sec": 60,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    flows = [
        WalletFlow(wallet=f"0x{i:040x}", direction="YES", net_usd=10_000,
                   last_update=stale_time, tier=1)
        for i in range(5)
    ]
    _attach_tiers(s, flows)
    s.attach_cluster_state(ClusterState(
        condition_id="0xq", yes_flows=flows, last_update=stale_time,
    ))
    sig = await s.evaluate(_market(), _features())
    assert sig is None  # all flows past latency cutoff


@pytest.mark.asyncio
async def test_hedge_flag_excludes_wallet() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    flows = _flows("YES", 4, tier=1)
    _attach_tiers(s, flows)
    # Flag 3 of 4 as hedgers → only 1 remains, below tier1_min_cluster=3.
    for f in flows[:3]:
        s.attach_wallet_hedge_flag(f.wallet, True)
    s.attach_cluster_state(ClusterState(condition_id="0xq", yes_flows=flows))
    sig = await s.evaluate(_market(), _features())
    assert sig is None


@pytest.mark.asyncio
async def test_drawdown_filter_excludes_losing_wallets() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "min_net_usd_per_wallet": 1_000,
    })
    s.attach_book("yes", _book("yes"))
    flows = _flows("YES", 4, tier=1)
    _attach_tiers(s, flows)
    for f in flows[:3]:
        s.attach_wallet_drawdown(f.wallet, 0.30)
    s.attach_cluster_state(ClusterState(condition_id="0xq", yes_flows=flows))
    sig = await s.evaluate(_market(), _features())
    assert sig is None


@pytest.mark.asyncio
async def test_below_min_net_usd_filters_out_wallet() -> None:
    s = SmartMoneyStrategy({
        "enabled": True,
        "tier1_min_cluster": 3,
        "tier2_min_cluster": 2,
        "min_net_usd_per_wallet": 10_000,
    })
    s.attach_book("yes", _book("yes"))
    # All 4 wallets below threshold (each $1K).
    flows = _flows("YES", 4, tier=1, net_usd=1_000)
    _attach_tiers(s, flows)
    s.attach_cluster_state(ClusterState(condition_id="0xq", yes_flows=flows))
    sig = await s.evaluate(_market(), _features())
    assert sig is None
