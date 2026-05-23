"""Signal aggregator — combine per-strategy signals into one AggregatedSignal.

See MASTER_SPEC §14.6.

Phase 3: multi-strategy aggregation with conviction-weighted voting and
per-strategy proposal helpers (proposed_price, proposed_size_pct).

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
from typing import Any, Callable

import structlog

from poly_meridian.domain import Action, AggregatedSignal, Market, StrategySignal
from poly_meridian.strategies.arbitrage import ArbitrageStrategy
from poly_meridian.strategies.fundamentals import FundamentalsStrategy
from poly_meridian.strategies.sentiment import SentimentStrategy
from poly_meridian.strategies.smart_money import SmartMoneyStrategy
from poly_meridian.strategies.stat_quant import StatQuantStrategy

log = structlog.get_logger("poly_meridian.aggregator")


# Per-strategy helper registry: each strategy contributes (price, size_pct) for
# the aggregator without the aggregator knowing strategy internals.
_PRICE_HELPERS: dict[str, Callable[[dict[str, Any]], Decimal]] = {
    "arbitrage":    ArbitrageStrategy.proposed_price_from_signal,
    "sentiment":    SentimentStrategy.proposed_price_from_signal,
    "smart_money":  SmartMoneyStrategy.proposed_price_from_signal,
    "stat_quant":   StatQuantStrategy.proposed_price_from_signal,
    "fundamentals": FundamentalsStrategy.proposed_price_from_signal,
}
_SIZE_HELPERS: dict[str, Callable[[dict[str, Any], Decimal, float], float]] = {
    "arbitrage":    ArbitrageStrategy.proposed_size_pct,
    "sentiment":    SentimentStrategy.proposed_size_pct,
    "smart_money":  SmartMoneyStrategy.proposed_size_pct,
    "stat_quant":   StatQuantStrategy.proposed_size_pct,
    "fundamentals": FundamentalsStrategy.proposed_size_pct,
}


def _resolve_helper(strategy_name: str, table: dict[str, Any]) -> Any:
    """StatQuant emits as `stat_quant.<sub>` — resolve by prefix."""
    base = strategy_name.split(".", 1)[0]
    return table.get(base)


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
        sigs = [s for s in signals if s.suggested_action != Action.HOLD]
        if not sigs:
            return None

        score: dict[Action, float] = defaultdict(float)
        weight: dict[Action, float] = defaultdict(float)
        edge_sum: dict[Action, float] = defaultdict(float)
        prices: list[Decimal] = []
        size_pcts: list[float] = []
        contributors: list[str] = []

        condition_id = sigs[0].condition_id

        # Direction → token_id mapping. All same-direction signals should agree
        # on token_id (one of YES or NO).
        direction_token: dict[Action, str] = {}

        for s in sigs:
            score[s.suggested_action] += s.conviction
            weight[s.suggested_action] += s.conviction
            edge_sum[s.suggested_action] += s.conviction * s.edge
            contributors.append(s.strategy)
            direction_token.setdefault(s.suggested_action, s.token_id)

            price_helper = _resolve_helper(s.strategy, _PRICE_HELPERS)
            size_helper = _resolve_helper(s.strategy, _SIZE_HELPERS)
            if price_helper is not None:
                prices.append(price_helper(s.rationale))
            if size_helper is not None:
                size_pcts.append(size_helper(s.rationale, bankroll_usd, self.max_size_pct))

        if not score:
            return None

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

        w = weight[direction]
        edge = edge_sum[direction] / w if w > 0 else 0.0
        conviction = min(1.0, top)
        proposed_price = max(prices) if prices else None
        size_pct = min(self.max_size_pct, sum(size_pcts)) if size_pcts else 0.0

        category = market.category if market is not None else None
        liquidity = float(market.liquidity_usd) if (market is not None and market.liquidity_usd) else None
        token_id = direction_token.get(direction, sigs[0].token_id)

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
