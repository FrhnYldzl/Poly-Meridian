"""Signal aggregator — combine per-strategy signals into one AggregatedSignal.

See MASTER_SPEC §14.6.

Phase 2: single-strategy pass-through (just `arbitrage`). Plumbed for the
multi-strategy case (Phase 3+) so it doesn't need a rewrite later.

Algorithm:
  1. Group signals by (condition_id, token_id).
  2. Sum conviction-weighted votes per direction.
  3. If max - second_max < conflict_threshold → return None (ambiguous).
  4. Winning direction → AggregatedSignal with conviction-weighted edge,
     proposed_price = max of contributing strategies' proposed prices,
     size_pct = capped sum of strategies' proposed sizes.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, AggregatedSignal, Market, StrategySignal
from poly_meridian.strategies.arbitrage import ArbitrageStrategy

log = structlog.get_logger("poly_meridian.aggregator")


class SignalAggregator:
    def __init__(
        self,
        *,
        conflict_threshold: float = 0.10,
        max_size_pct_per_position: float = 0.05,
    ) -> None:
        self.conflict_threshold = conflict_threshold
        self.max_size_pct = max_size_pct_per_position

    def aggregate(
        self,
        signals: Iterable[StrategySignal],
        *,
        market: Market | None = None,
        bankroll_usd: Decimal = Decimal("100000"),
    ) -> AggregatedSignal | None:
        sigs = list(signals)
        if not sigs:
            return None

        # Group by direction.
        score: dict[Action, float] = defaultdict(float)
        edge_sum: dict[Action, float] = defaultdict(float)
        weight: dict[Action, float] = defaultdict(float)
        prices: list[Decimal] = []
        size_pcts: list[float] = []
        contributors: list[str] = []
        condition_id = sigs[0].condition_id
        token_id = sigs[0].token_id

        for s in sigs:
            if s.suggested_action in (Action.HOLD,):
                continue
            score[s.suggested_action] += s.conviction
            weight[s.suggested_action] += s.conviction
            edge_sum[s.suggested_action] += s.conviction * s.edge
            contributors.append(s.strategy)

            # Strategy-specific proposed price/size — Phase 2 wires arb;
            # later strategies will register their own helpers.
            if s.strategy == "arbitrage":
                prices.append(ArbitrageStrategy.proposed_price_from_signal(s.rationale))
                size_pcts.append(
                    ArbitrageStrategy.proposed_size_pct(
                        s.rationale, bankroll_usd, self.max_size_pct
                    )
                )

        if not score:
            return None

        # Conflict check: max vs runner-up.
        ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
        direction, top = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        if (top - runner) < self.conflict_threshold:
            log.info(
                "aggregator.conflict",
                top=str(direction),
                top_score=top,
                runner_score=runner,
                threshold=self.conflict_threshold,
            )
            return None

        # Conviction-weighted edge.
        w = weight[direction]
        edge = edge_sum[direction] / w if w > 0 else 0.0
        conviction = min(1.0, top)  # conviction is capped at 1
        proposed_price = max(prices) if prices else None
        size_pct = min(self.max_size_pct, sum(size_pcts)) if size_pcts else 0.0

        category = market.category if market is not None else None
        liquidity = float(market.liquidity_usd) if (market is not None and market.liquidity_usd) else None

        return AggregatedSignal(
            ts=datetime.now(UTC),
            condition_id=condition_id,
            token_id=token_id,
            direction=direction,
            edge=edge,
            conviction=conviction,
            size_pct=size_pct,
            proposed_price=proposed_price,
            category=category,
            market_liquidity_usd=liquidity,
            contributors=contributors,
        )


__all__: list[str] = ["SignalAggregator"]
