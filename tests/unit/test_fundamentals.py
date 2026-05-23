"""Fundamentals resolvers — pure-compute correctness."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poly_meridian.domain import Market
from poly_meridian.fundamentals import (
    CryptoResolver,
    EloEngine,
    FundamentalsContext,
    MacroResolver,
    PoliticsResolver,
    SportsResolver,
)


def _market(cat: str, end_in_days: int | None = 30) -> Market:
    end = (datetime.now(UTC) + timedelta(days=end_in_days)) if end_in_days else None
    return Market(
        condition_id="0xq",
        question="q",
        category=cat,
        yes_token_id="yes",
        no_token_id="no",
        end_date_iso=end,
    )


# ---------- Politics ----------

def test_politics_resolver_aggregates_polls() -> None:
    now = datetime.now(UTC)
    polls = [
        {"ts": now - timedelta(days=1), "yes_pct": 0.60, "sample_size": 1000,
         "source": "PollA", "methodology_weight": 0.9, "house_bias": 0.01},
        {"ts": now - timedelta(days=3), "yes_pct": 0.62, "sample_size": 800,
         "source": "PollB", "methodology_weight": 0.8, "house_bias": 0.0},
        {"ts": now - timedelta(days=5), "yes_pct": 0.58, "sample_size": 1200,
         "source": "PollC", "methodology_weight": 0.85, "house_bias": -0.01},
    ]
    ctx = FundamentalsContext(polls={"0xq": polls}, now=now)
    res = PoliticsResolver().resolve(_market("Politics"), ctx)
    assert res is not None
    # Weighted, bias-corrected → near 0.60
    assert 0.55 < res.p_yes < 0.65
    assert res.confidence > 0


def test_politics_returns_none_when_too_few_polls() -> None:
    polls = [
        {"ts": datetime.now(UTC), "yes_pct": 0.5, "sample_size": 1000,
         "methodology_weight": 0.9, "source": "A"},
    ]
    ctx = FundamentalsContext(polls={"0xq": polls})
    assert PoliticsResolver().resolve(_market("Politics"), ctx) is None


def test_politics_old_polls_get_low_weight() -> None:
    now = datetime.now(UTC)
    fresh_polls = [
        {"ts": now, "yes_pct": 0.70, "sample_size": 1000,
         "methodology_weight": 1.0, "house_bias": 0.0, "source": f"S{i}"}
        for i in range(3)
    ]
    stale_polls = [
        {"ts": now - timedelta(days=365), "yes_pct": 0.30, "sample_size": 1000,
         "methodology_weight": 1.0, "house_bias": 0.0, "source": f"S{i+3}"}
        for i in range(3)
    ]
    ctx = FundamentalsContext(polls={"0xq": fresh_polls + stale_polls}, now=now)
    res = PoliticsResolver().resolve(_market("Politics"), ctx)
    assert res is not None
    # Fresh polls should dominate → close to 0.70.
    assert res.p_yes > 0.60


# ---------- Sports ----------

def test_elo_expected_score_symmetric() -> None:
    eng = EloEngine()
    p = eng.expected_score(1500, 1500)
    assert p == pytest.approx(0.5)


def test_elo_higher_rating_higher_prob() -> None:
    eng = EloEngine()
    assert eng.expected_score(1700, 1500) > 0.5
    assert eng.expected_score(1300, 1500) < 0.5


def test_elo_update_winner_gains() -> None:
    eng = EloEngine(k_factor=32)
    new_a, new_b = eng.update(1500, 1500, score_a=1.0)
    assert new_a > 1500
    assert new_b < 1500


def test_sports_resolver_emits_p_yes() -> None:
    ctx = FundamentalsContext(
        elo_ratings={"home_team": 1600, "away_team": 1500},
        sports_metadata={"0xq": {
            "home_team_id": "home_team",
            "away_team_id": "away_team",
            "home_advantage_elo": 80,
            "yes_means_home_wins": True,
        }},
    )
    res = SportsResolver().resolve(_market("Sports"), ctx)
    assert res is not None
    assert res.p_yes > 0.5


def test_sports_resolver_inverted_when_yes_means_away() -> None:
    ctx = FundamentalsContext(
        elo_ratings={"h": 1600, "a": 1500},
        sports_metadata={"0xq": {
            "home_team_id": "h", "away_team_id": "a",
            "yes_means_home_wins": False,
        }},
    )
    res = SportsResolver().resolve(_market("Sports"), ctx)
    assert res is not None
    assert res.p_yes < 0.5


# ---------- Crypto ----------

def test_crypto_resolver_p_above_grows_when_target_close() -> None:
    ctx = FundamentalsContext(
        spot_prices={"BTC-USD": 100_000},
        crypto_metadata={"0xq": {
            "symbol": "BTC-USD",
            "target_price": 101_000,           # very close → likely true
            "direction": "above",
            "deadline_ts": datetime.now(UTC) + timedelta(days=30),
        }},
    )
    res = CryptoResolver().resolve(_market("Crypto"), ctx)
    assert res is not None
    assert res.p_yes > 0.4


def test_crypto_resolver_p_falls_when_target_far() -> None:
    ctx = FundamentalsContext(
        spot_prices={"BTC-USD": 100_000},
        crypto_metadata={"0xq": {
            "symbol": "BTC-USD",
            "target_price": 200_000,           # very far
            "direction": "above",
            "deadline_ts": datetime.now(UTC) + timedelta(days=30),
        }},
    )
    res = CryptoResolver().resolve(_market("Crypto"), ctx)
    assert res is not None
    assert res.p_yes < 0.3


def test_crypto_resolver_below_direction_inverts() -> None:
    ctx = FundamentalsContext(
        spot_prices={"BTC-USD": 100_000},
        crypto_metadata={"0xq": {
            "symbol": "BTC-USD", "target_price": 110_000,
            "direction": "below",
            "deadline_ts": datetime.now(UTC) + timedelta(days=30),
        }},
    )
    res = CryptoResolver().resolve(_market("Crypto"), ctx)
    assert res is not None
    assert 0 < res.p_yes < 1


# ---------- Macro ----------

def test_macro_resolver_hawkish_majority() -> None:
    now = datetime.now(UTC)
    events = [
        {"ts": now - timedelta(days=10 * (i + 1)), "type": "fed_rate_decision",
         "outcome": "hawkish"} for i in range(4)
    ] + [
        {"ts": now - timedelta(days=20), "type": "fed_rate_decision",
         "outcome": "dovish"}
    ]
    ctx = FundamentalsContext(
        economic_events=events,
        macro_metadata={"0xq": {
            "event_type": "fed_rate_decision",
            "yes_means_hawkish": True,
        }},
    )
    res = MacroResolver().resolve(_market("Macro"), ctx)
    assert res is not None
    assert res.p_yes > 0.5


def test_macro_returns_none_when_insufficient_events() -> None:
    ctx = FundamentalsContext(
        economic_events=[],
        macro_metadata={"0xq": {"event_type": "cpi"}},
    )
    assert MacroResolver().resolve(_market("Macro"), ctx) is None
