"""Macro resolver — economic-calendar-driven probability. See §14.5.

Expects `ctx.economic_events` rows + `ctx.macro_metadata[condition_id]`:
  {
    "event_type": "fed_rate_decision",     # or "cpi_release", "nfp", ...
    "country": "US",
    "expected_outcome": "hawkish",         # or "dovish", "neutral"
    "consensus_value": float | None,        # e.g. expected CPI YoY
    "yes_means_hawkish": bool,
  }

Heuristic (Phase 5b minimal): count recent same-event-type prints + their
direction (hawkish/dovish). Higher hawkish count → higher p_yes when
`yes_means_hawkish=true`. This is a low-confidence baseline — sufficient
to wire the framework; real models live in research notebooks.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)


class MacroResolver(CategoryResolver):
    category = "Macro"

    def __init__(self, *, lookback_days: int = 180) -> None:
        self.lookback_days = lookback_days

    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        meta = ctx.macro_metadata.get(market.condition_id)
        if meta is None:
            return None

        event_type = meta.get("event_type")
        if not event_type:
            return None
        yes_means_hawkish = bool(meta.get("yes_means_hawkish", True))

        now = ctx.now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.lookback_days)

        hawkish = 0
        dovish = 0
        for ev in ctx.economic_events:
            if ev.get("type") != event_type:
                continue
            ts = ev.get("ts")
            if not isinstance(ts, datetime) or ts < cutoff:
                continue
            outcome = str(ev.get("outcome", "")).lower()
            if outcome == "hawkish":
                hawkish += 1
            elif outcome == "dovish":
                dovish += 1

        total = hawkish + dovish
        if total < 2:
            return None

        hawk_ratio = hawkish / total
        p_yes = hawk_ratio if yes_means_hawkish else (1.0 - hawk_ratio)
        # Confidence grows with sample size, asymptotic.
        confidence = min(0.85, 0.3 + 0.1 * total)

        return ProbabilityEstimate(
            p_yes=p_yes,
            confidence=confidence,
            rationale={
                "category": "Macro",
                "event_type": event_type,
                "n_hawkish": hawkish,
                "n_dovish": dovish,
                "hawk_ratio": hawk_ratio,
                "yes_means_hawkish": yes_means_hawkish,
            },
        )
