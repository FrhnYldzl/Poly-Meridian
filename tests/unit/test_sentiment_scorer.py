"""Sentiment scorer — heuristic + JSON parsing edge cases."""
from __future__ import annotations

import pytest

from poly_meridian.sentiment.scorer import (
    HeuristicSentimentScorer,
    SentimentResult,
    _parse_json_result,
)


@pytest.mark.asyncio
async def test_heuristic_positive() -> None:
    s = HeuristicSentimentScorer()
    res = await s.score(
        article_title="Stocks rise as Fed signals rate cut",
        article_body="Markets surged on bullish growth data",
        market_question="Will SPX close above 6000?",
    )
    assert res.sentiment > 0
    assert res.direction == "YES"


@pytest.mark.asyncio
async def test_heuristic_negative() -> None:
    s = HeuristicSentimentScorer()
    res = await s.score(
        article_title="Tech stocks crash on recession fears",
        article_body="Major declines and bearish forecasts cloud the outlook",
        market_question="Will SPX close above 6000?",
    )
    assert res.sentiment < 0
    assert res.direction == "NO"


@pytest.mark.asyncio
async def test_heuristic_neutral_when_no_keywords() -> None:
    s = HeuristicSentimentScorer()
    res = await s.score(
        article_title="Earnings calendar this week",
        article_body="Various companies will report",
        market_question="?",
    )
    assert res.direction == "NEUTRAL"


def test_parse_json_plain() -> None:
    out = _parse_json_result('{"sentiment": 0.5, "impact": 0.8, "direction": "YES", "rationale": "x"}')
    assert out is not None
    assert out["sentiment"] == 0.5
    assert out["direction"] == "YES"


def test_parse_json_with_fence() -> None:
    raw = '```json\n{"sentiment": -0.3, "impact": 0.4, "direction": "NO", "rationale": "y"}\n```'
    out = _parse_json_result(raw)
    assert out is not None
    assert out["sentiment"] == -0.3
    assert out["direction"] == "NO"


def test_parse_json_with_preamble() -> None:
    raw = 'Here is the analysis:\n{"sentiment": 0.0, "impact": 0.1, "direction": "NEUTRAL"}'
    out = _parse_json_result(raw)
    assert out is not None
    assert out["impact"] == 0.1


def test_parse_json_returns_none_for_garbage() -> None:
    assert _parse_json_result("totally not json") is None


def test_result_clip_clamps_values() -> None:
    r = SentimentResult(sentiment=5.0, impact=2.0, direction="INVALID")
    c = r.clip()
    assert c.sentiment == 1.0
    assert c.impact == 1.0
    assert c.direction == "NEUTRAL"
