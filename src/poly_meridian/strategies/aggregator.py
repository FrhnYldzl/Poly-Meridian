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
        conflict_threshold: float = 0.05,
        max_size_pct_per_position: float = 0.05,
        consensus_boost: float = 0.15,
        cross_strategy_disagreement_blocks: bool = True,
    ) -> None:
        # Lowered from 0.10 — at the original threshold solo strategy signals
        # with conviction in the 0.05–0.10 band (common from STAT_QUANT
        # mean_reversion near zscore_threshold) were dropped even though
        # there was no actual conflict. 0.05 still rejects close two-strategy
        # ties while letting borderline solo signals through.
        self.conflict_threshold = conflict_threshold
        self.max_size_pct = max_size_pct_per_position
        # Phase S.2 — when 2+ DISTINCT base strategies (fundamentals/
        # arbitrage/sentiment/smart_money/stat_quant — not sub-variants
        # of stat_quant) agree on direction, boost the aggregate conviction
        # by this fraction (additive, capped at 1.0). 3 strategies = 2×
        # the boost. Caps adoption of consensus signals without over-
        # weighting a single noisy strategy.
        self.consensus_boost = consensus_boost
        # Phase S.2 — when DISTINCT base strategies fire OPPOSITE directions
        # on the same market, this is information conflict and the trade
        # should be SKIPPED entirely (one of them is wrong; we don't know
        # which). Without this guard the conviction-weighted vote could
        # pick the wrong side. Default on.
        self.cross_disagreement_blocks = cross_strategy_disagreement_blocks

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
        # Phase S.2 — track DISTINCT base strategies (not sub-variants)
        # per direction so we can detect cross-strategy consensus AND
        # cross-strategy disagreement.
        bases_per_direction: dict[Action, set[str]] = defaultdict(set)

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
            base = s.strategy.split(".", 1)[0]
            bases_per_direction[s.suggested_action].add(base)

            price_helper = _resolve_helper(s.strategy, _PRICE_HELPERS)
            size_helper = _resolve_helper(s.strategy, _SIZE_HELPERS)
            if price_helper is not None:
                prices.append(price_helper(s.rationale))
            if size_helper is not None:
                size_pcts.append(size_helper(s.rationale, bankroll_usd, self.max_size_pct))

        if not score:
            return None

        # Phase S.2 — cross-strategy disagreement check. If two DISTINCT
        # base strategies fire on OPPOSITE directions, we have an
        # information conflict — block the trade entirely. The within-
        # strategy conflict_threshold below is a secondary guard for
        # conviction ties, but distinct-base-strategy disagreement is a
        # binary block.
        if self.cross_disagreement_blocks:
            distinct_bases_all = {b for bases in bases_per_direction.values() for b in bases}
            buy_yes_bases = bases_per_direction.get(Action.BUY_YES, set())
            buy_no_bases = bases_per_direction.get(Action.BUY_NO, set())
            if buy_yes_bases and buy_no_bases:
                log.info(
                    "aggregator.cross_strategy_disagreement",
                    yes_bases=sorted(buy_yes_bases),
                    no_bases=sorted(buy_no_bases),
                )
                from poly_meridian.pipeline import PM_STRATEGY_REJECT
                PM_STRATEGY_REJECT.labels(
                    strategy="aggregator", reason="cross_strategy_disagreement"
                ).inc()
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

        # Phase S.2 — multi-strategy consensus boost. Each additional
        # DISTINCT base strategy beyond the first adds `consensus_boost`
        # to the conviction (capped at 1.0). Two strategies agreeing is
        # markedly more reliable than one; three is gold.
        n_distinct_winners = len(bases_per_direction.get(direction, set()))
        if n_distinct_winners >= 2:
            conviction = min(
                1.0, conviction + (n_distinct_winners - 1) * self.consensus_boost
            )
            log.info(
                "aggregator.consensus_boost",
                n_strategies=n_distinct_winners,
                bases=sorted(bases_per_direction[direction]),
                final_conviction=round(conviction, 4),
            )
        proposed_price = max(prices) if prices else None
        size_pct = min(self.max_size_pct, sum(size_pcts)) if size_pcts else 0.0

        category = market.category if market is not None else None
        liquidity = float(market.liquidity_usd) if (market is not None and market.liquidity_usd) else None
        token_id = direction_token.get(direction, sigs[0].token_id)

        # Phase N.4 — if an arbitrage signal contributed to this aggregate,
        # lift the partner-token info (NO leg for a YES-side arb signal)
        # so the router can submit both legs atomically. Without this the
        # arb is unhedged directional (BUG #5).
        paired_token: str | None = None
        paired_price: Decimal | None = None
        paired_side: Action | None = None
        for s in sigs:
            if not s.strategy.startswith("arbitrage"):
                continue
            partner = s.rationale.get("partner_token")
            no_ask = s.rationale.get("no_ask")
            if partner and no_ask:
                paired_token = str(partner)
                try:
                    paired_price = Decimal(str(no_ask))
                except Exception:
                    paired_price = None
                # If primary leg is BUY_YES, partner is BUY_NO (complete set arb).
                paired_side = (
                    Action.BUY_NO if s.suggested_action == Action.BUY_YES
                    else Action.BUY_YES
                )
                break

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
            paired_token=paired_token,
            paired_price=paired_price,
            paired_side=paired_side,
            contributors=contributors,
        )


__all__: list[str] = ["SignalAggregator"]
