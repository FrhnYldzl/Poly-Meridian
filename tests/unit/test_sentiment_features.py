"""Sentiment aggregation."""
from __future__ import annotations

from poly_meridian.features.sentiment_features import aggregate_signals


def test_empty_signals_returns_zeros() -> None:
    agg = aggregate_signals([])
    assert agg.n_signals == 0
    assert agg.sentiment_avg == 0.0
    assert agg.impact_max == 0.0
    assert agg.winning_direction == "NEUTRAL"


def test_aggregate_weighted_by_impact() -> None:
    rows = [
        {"sentiment": 1.0, "impact": 0.9, "direction": "YES"},
        {"sentiment": -0.5, "impact": 0.1, "direction": "NO"},
    ]
    agg = aggregate_signals(rows)
    # weighted_sum = 0.9 + (-0.05) = 0.85; weight = 1.0; avg = 0.85
    assert round(agg.sentiment_avg, 3) == 0.85
    assert agg.impact_max == 0.9
    assert agg.winning_direction == "YES"


def test_direction_score_sums_impact() -> None:
    rows = [
        {"sentiment": 0.5, "impact": 0.4, "direction": "YES"},
        {"sentiment": 0.6, "impact": 0.3, "direction": "YES"},
        {"sentiment": -0.4, "impact": 0.5, "direction": "NO"},
    ]
    agg = aggregate_signals(rows)
    assert agg.direction_score["YES"] == 0.7
    assert agg.direction_score["NO"] == 0.5
    assert agg.winning_direction == "YES"


def test_zero_impact_yields_zero_avg() -> None:
    rows = [{"sentiment": 0.5, "impact": 0.0, "direction": "YES"}]
    agg = aggregate_signals(rows)
    assert agg.sentiment_avg == 0.0
