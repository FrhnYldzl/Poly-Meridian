"""Sports Elo rating engine + resolver. See §14.5."""
from __future__ import annotations

import math
from dataclasses import dataclass

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)


@dataclass
class EloEngine:
    """Stateless Elo math. Caller stores ratings; we compute outcomes."""

    k_factor: float = 32.0
    base_rating: float = 1500.0

    def expected_score(self, rating_a: float, rating_b: float, *, home_advantage: float = 0.0) -> float:
        """Probability that A beats B (in 0..1)."""
        adj = (rating_a + home_advantage) - rating_b
        return 1.0 / (1.0 + math.pow(10.0, -adj / 400.0))

    def update(
        self,
        rating_a: float,
        rating_b: float,
        score_a: float,
        *,
        home_advantage: float = 0.0,
    ) -> tuple[float, float]:
        """Apply a match result. score_a = 1 (A won), 0 (B won), 0.5 (draw)."""
        expected_a = self.expected_score(rating_a, rating_b, home_advantage=home_advantage)
        new_a = rating_a + self.k_factor * (score_a - expected_a)
        new_b = rating_b + self.k_factor * ((1.0 - score_a) - (1.0 - expected_a))
        return new_a, new_b


class SportsResolver(CategoryResolver):
    """Resolves Sports markets via Elo expected score.

    Expects `ctx.sports_metadata[condition_id]` to contain:
      {
        "home_team_id": "...",
        "away_team_id": "...",
        "home_advantage_elo": float,   # default 80
        "yes_means_home_wins": bool,   # if False, YES = away wins
      }
    """

    category = "Sports"

    def __init__(self, engine: EloEngine | None = None) -> None:
        self.engine = engine or EloEngine()

    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        meta = ctx.sports_metadata.get(market.condition_id)
        if meta is None:
            return None
        home = meta.get("home_team_id")
        away = meta.get("away_team_id")
        if not home or not away:
            return None
        r_home = ctx.elo_ratings.get(home)
        r_away = ctx.elo_ratings.get(away)
        if r_home is None or r_away is None:
            return None

        home_adv = float(meta.get("home_advantage_elo", 80.0))
        p_home = self.engine.expected_score(r_home, r_away, home_advantage=home_adv)
        yes_means_home = bool(meta.get("yes_means_home_wins", True))
        p_yes = p_home if yes_means_home else (1.0 - p_home)

        # Confidence proxy: rating gap + presence of both ratings = high
        rating_gap = abs(r_home - r_away)
        confidence = min(1.0, 0.5 + rating_gap / 400.0)

        return ProbabilityEstimate(
            p_yes=p_yes,
            confidence=confidence,
            rationale={
                "category": "Sports",
                "home_team": home,
                "away_team": away,
                "elo_home": r_home,
                "elo_away": r_away,
                "p_home": p_home,
                "yes_means_home_wins": yes_means_home,
            },
        )
