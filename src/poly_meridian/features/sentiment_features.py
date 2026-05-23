"""Sentiment features — aggregate per-market news signals. §13.

The window-averaged sentiment + max impact are what `SentimentStrategy`
consumes to decide whether to take a position.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SentimentAggregate:
    n_signals: int
    sentiment_avg: float        # -1..1, conviction-weighted by impact
    impact_max: float           #  0..1
    direction_score: dict[str, float]   # YES/NO/NEUTRAL → summed impact

    @property
    def winning_direction(self) -> str:
        if not self.direction_score:
            return "NEUTRAL"
        return max(self.direction_score, key=lambda k: self.direction_score[k])


def aggregate_signals(rows: list[dict[str, Any]]) -> SentimentAggregate:
    """Combine recent news_signals into a single per-market aggregate.

    `rows` is the output of `fetch_recent_news_signals` (each row has
    `sentiment`, `impact`, `direction`).
    """
    if not rows:
        return SentimentAggregate(0, 0.0, 0.0, {})

    direction_score: dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    impact_max = 0.0

    for r in rows:
        s = float(r["sentiment"])
        i = float(r["impact"])
        d = str(r["direction"])
        weighted_sum += s * i
        weight_total += i
        impact_max = max(impact_max, i)
        direction_score[d] = direction_score.get(d, 0.0) + i

    avg = weighted_sum / weight_total if weight_total > 0 else 0.0
    return SentimentAggregate(
        n_signals=len(rows),
        sentiment_avg=avg,
        impact_max=impact_max,
        direction_score=direction_score,
    )
