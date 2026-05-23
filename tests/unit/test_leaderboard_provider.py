"""Leaderboard provider: tier classification + payload normalization."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from poly_meridian.ingestion.leaderboard_provider import (
    LeaderboardEntry,
    LeaderboardProvider,
    TierThresholds,
    _normalize_rows,
    classify_tier,
)


def _entry(**kw: object) -> LeaderboardEntry:
    base: dict[str, object] = {
        "address": "0xa",
        "display_name": None,
        "lifetime_pnl_usd": Decimal("0"),
        "win_rate": 0.0,
        "trade_count": 0,
        "last_7d_pnl_usd": Decimal("0"),
        "drawdown_7d_pct": 0.0,
        "category_focus": None,
        "last_trade_ts": None,
        "raw": {},
    }
    base.update(kw)
    return LeaderboardEntry(**base)  # type: ignore[arg-type]


def test_classify_tier_returns_tier_1_when_all_conditions_met() -> None:
    e = _entry(
        lifetime_pnl_usd=Decimal("600000"),
        win_rate=0.60,
        trade_count=300,
        drawdown_7d_pct=0.05,
    )
    assert classify_tier(e) == 1


def test_classify_tier_2_when_recent_pnl_strong_but_lifetime_weak() -> None:
    e = _entry(
        lifetime_pnl_usd=Decimal("50000"),
        win_rate=0.55,
        trade_count=100,
        last_7d_pnl_usd=Decimal("80000"),
    )
    assert classify_tier(e) == 2


def test_classify_tier_3_default() -> None:
    assert classify_tier(_entry()) == 3


def test_classify_tier_1_excluded_when_drawdown_high() -> None:
    e = _entry(
        lifetime_pnl_usd=Decimal("1000000"),
        win_rate=0.70,
        trade_count=500,
        drawdown_7d_pct=0.30,
    )
    # high drawdown → fails Tier 1, last_7d_pnl=0 → also fails Tier 2
    assert classify_tier(e) == 3


def test_classify_tier_with_custom_thresholds() -> None:
    t = TierThresholds(
        tier1_lifetime_pnl_usd=100_000,
        tier1_win_rate=0.50,
        tier1_trade_count=50,
        tier1_drawdown_7d_max=0.30,
    )
    e = _entry(
        lifetime_pnl_usd=Decimal("150000"),
        win_rate=0.52,
        trade_count=60,
        drawdown_7d_pct=0.10,
    )
    assert classify_tier(e, t) == 1


def test_normalize_rows_list_envelope() -> None:
    payload = [
        {"address": "0xa1", "name": "alice", "pnl": "100000", "winRate": 60, "tradeCount": 150},
        {"address": "0xa2", "pnl": "50000"},
    ]
    entries = _normalize_rows(payload)
    assert len(entries) == 2
    assert entries[0].address == "0xa1"
    assert entries[0].display_name == "alice"
    assert entries[0].lifetime_pnl_usd == Decimal("100000")
    assert entries[0].win_rate == 0.6


def test_normalize_rows_data_envelope() -> None:
    payload = {"data": [{"address": "0xab", "pnl": "20000"}]}
    entries = _normalize_rows(payload)
    assert len(entries) == 1


def test_normalize_rows_ignores_invalid_address() -> None:
    payload = [{"address": "not-hex", "pnl": "100"}, {"foo": "bar"}]
    entries = _normalize_rows(payload)
    assert entries == []


@pytest.mark.asyncio
async def test_fetch_top_traders_uses_first_working_endpoint() -> None:
    call_log: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        call_log.append(str(req.url))
        if "data-api" in str(req.url):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json=[{"address": "0xabc", "pnl": "1000"}])

    transport = httpx.MockTransport(handler)
    lb = LeaderboardProvider(candidate_urls=(
        "https://data-api.polymarket.com/leaderboard",
        "https://fallback.example/leaderboard",
    ))
    await lb.start()
    lb._client._transport = transport  # type: ignore[reportPrivateUsage]

    entries = await lb.fetch_top_traders(period="month", sort="profit", limit=10)
    assert len(entries) == 1
    assert entries[0].address == "0xabc"
    # First URL failed (404), second succeeded → exactly 2 calls.
    assert len(call_log) == 2
    await lb.stop()
