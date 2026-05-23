"""Fundamentals base types. See MASTER_SPEC §14.5."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from poly_meridian.domain import Market


@dataclass(frozen=True)
class ProbabilityEstimate:
    """A resolver's output. Confidence ∈ [0, 1] — caller weights by this."""

    p_yes: float                # 0..1, our probability of YES resolving true
    confidence: float           # 0..1, how much we trust this estimate
    rationale: dict[str, Any] = field(default_factory=dict)


@dataclass
class FundamentalsContext:
    """Mutable bag of per-category inputs the main loop fills in.

    The resolver expects whatever its category needs to be present; missing
    inputs cause it to return None (graceful degradation).
    """

    # Politics
    polls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # condition_id → list of poll rows. Each row: {timestamp, yes_pct, sample_size,
    # source, methodology_weight}.

    # Sports
    elo_ratings: dict[str, float] = field(default_factory=dict)
    # team_id → current Elo rating
    sports_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    # condition_id → {home_team_id, away_team_id, home_advantage_bps, ...}

    # Crypto
    spot_prices: dict[str, float] = field(default_factory=dict)
    # symbol → latest spot (e.g. "BTC-USD" → 105_432.10)
    funding_rates: dict[str, float] = field(default_factory=dict)
    # symbol → funding rate (e.g. 0.0001 = 0.01%)
    netflow_24h: dict[str, float] = field(default_factory=dict)
    # symbol → 24h exchange netflow USD (positive = inflow, negative = outflow)
    crypto_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    # condition_id → {symbol, target_price, deadline, direction}

    # Macro
    economic_events: list[dict[str, Any]] = field(default_factory=list)
    # rows: {ts, type, impact, country, description}
    macro_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    # condition_id → {event_type, country, hawkish_bias_baseline, ...}

    # Common
    now: datetime | None = None


class CategoryResolver(ABC):
    """One per category (Politics / Sports / Crypto / Macro)."""

    category: str

    @abstractmethod
    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        """Return our probability estimate, or None when inputs are missing."""
