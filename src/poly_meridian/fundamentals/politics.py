"""Politics resolver — poll aggregator with bias correction. See §14.5.

Inputs (via `FundamentalsContext.polls`):
  list[{
      "ts": datetime,
      "yes_pct": float,        # 0..1, poll's reported probability for YES
      "sample_size": int,
      "source": str,
      "methodology_weight": float,  # 0..1, our trust in the pollster
      "house_bias": float,     # -0.05..+0.05, known systematic lean
  }]

Aggregation:
  - Weight each poll by sqrt(sample_size) × methodology_weight × time_decay
  - Subtract house_bias to correct for known systematic lean
  - Weighted average → p_yes
  - Confidence proportional to total weight + sample-size diversity
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)


class PoliticsResolver(CategoryResolver):
    category = "Politics"

    def __init__(
        self,
        *,
        recency_half_life_days: float = 14.0,
        min_total_weight: float = 5.0,
        min_polls: int = 3,
    ) -> None:
        self.recency_half_life_days = recency_half_life_days
        self.min_total_weight = min_total_weight
        self.min_polls = min_polls

    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        polls = ctx.polls.get(market.condition_id, [])
        if len(polls) < self.min_polls:
            return None

        now = ctx.now or datetime.now(UTC)
        weighted_sum = 0.0
        total_weight = 0.0
        sources: set[str] = set()
        accepted = 0

        for p in polls:
            yes_pct = p.get("yes_pct")
            ts = p.get("ts")
            if not isinstance(yes_pct, (int, float)) or not (0.0 <= yes_pct <= 1.0):
                continue
            if not isinstance(ts, datetime):
                continue
            sample = int(p.get("sample_size", 0)) or 1
            mw = float(p.get("methodology_weight", 0.5))
            bias = float(p.get("house_bias", 0.0))

            age_days = (now - ts).total_seconds() / 86_400
            time_factor = math.pow(0.5, age_days / self.recency_half_life_days)
            weight = math.sqrt(sample) * mw * time_factor
            corrected = max(0.0, min(1.0, yes_pct - bias))

            weighted_sum += corrected * weight
            total_weight += weight
            sources.add(str(p.get("source", "unknown")))
            accepted += 1

        if total_weight < self.min_total_weight or accepted < self.min_polls:
            return None

        p_yes = weighted_sum / total_weight
        # Confidence scales with weight + source diversity, asymptotes to 1.
        weight_factor = 1.0 - math.exp(-total_weight / 50.0)
        diversity_factor = min(1.0, len(sources) / 3)
        confidence = weight_factor * (0.6 + 0.4 * diversity_factor)

        return ProbabilityEstimate(
            p_yes=p_yes,
            confidence=confidence,
            rationale={
                "category": "Politics",
                "n_polls": accepted,
                "total_weight": total_weight,
                "n_sources": len(sources),
                "p_yes": p_yes,
            },
        )
