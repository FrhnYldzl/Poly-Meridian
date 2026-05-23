"""SmartMoneyStrategy — cluster-flow detector across seeded smart wallets. §14.3.

Trigger: in the last `window_sec`, at least `min_wallet_count` distinct smart
wallets have net-bought the same outcome with `>= min_net_usd_per_wallet` each.

Phase 3 simplification: the main loop pushes a cached `cluster_state` per
condition_id via `attach_cluster_state()`. The cluster state is what the
on-chain processor + per-wallet trade log feed produces.

Future phases will wire real-time cluster updates from `PolygonOnchainSource`
events through a `cluster_state_builder` task; the strategy itself stays a
pure-compute predicate so it's easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, Features, Market, StrategySignal
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.base import BaseStrategy

log = structlog.get_logger("poly_meridian.strategies.smart_money")


@dataclass
class WalletFlow:
    wallet: str
    direction: str           # "YES" or "NO" (token side they accumulated)
    net_usd: float           # net buy notional in USD
    last_update: datetime


@dataclass
class ClusterState:
    """Snapshot per condition_id, built by the cluster_state_builder."""

    condition_id: str
    yes_flows: list[WalletFlow] = field(default_factory=list)
    no_flows: list[WalletFlow] = field(default_factory=list)
    last_update: datetime = field(default_factory=lambda: datetime.now(UTC))


class SmartMoneyStrategy(BaseStrategy):
    """Configuration:
      - enabled
      - min_wallet_count (default 3)
      - min_net_usd_per_wallet (default 5000)
      - freshness_max_sec (default 86400 — drop signals older than 24h)
      - max_size_pct (default 0.025)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(name="smart_money", config=config, enabled=config.get("enabled", False))
        self.min_wallet_count = int(config.get("min_wallet_count", 3))
        self.min_net_usd_per_wallet = float(config.get("min_net_usd_per_wallet", 5_000))
        self.freshness_max_sec = int(config.get("freshness_max_sec", 86_400))
        self.max_size_pct = float(config.get("max_size_pct", 0.025))
        self._books: dict[str, LocalBook] = {}
        self._cluster_state: dict[str, ClusterState] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def attach_cluster_state(self, state: ClusterState) -> None:
        self._cluster_state[state.condition_id] = state

    async def evaluate(
        self, market: Market, features: Features
    ) -> StrategySignal | None:
        if not self.enabled:
            return None

        cs = self._cluster_state.get(market.condition_id)
        if cs is None:
            return None

        now = datetime.now(UTC)
        if (now - cs.last_update).total_seconds() > self.freshness_max_sec:
            return None

        yes_qualifying = [f for f in cs.yes_flows if f.net_usd >= self.min_net_usd_per_wallet]
        no_qualifying = [f for f in cs.no_flows if f.net_usd >= self.min_net_usd_per_wallet]

        yes_count = len({f.wallet for f in yes_qualifying})
        no_count = len({f.wallet for f in no_qualifying})

        if max(yes_count, no_count) < self.min_wallet_count:
            return None

        if yes_count > no_count:
            direction = "YES"
            cluster_size = yes_count
            net_usd_total = sum(f.net_usd for f in yes_qualifying)
            token_id = market.yes_token_id
            action = Action.BUY_YES
        else:
            direction = "NO"
            cluster_size = no_count
            net_usd_total = sum(f.net_usd for f in no_qualifying)
            token_id = market.no_token_id
            action = Action.BUY_NO

        book = self._books.get(token_id)
        if book is None:
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            return None
        price, _ = best_ask

        # Conviction grows with cluster size: 0.5 at min, asymptotes to 1.0
        # by 5x min. Smooth bounded function.
        ratio = cluster_size / self.min_wallet_count
        conviction = min(1.0, 0.5 + 0.1 * (ratio - 1.0))

        # Edge estimate: smart-money cluster's net buy tells us they think
        # the market underprices this side; assume they're right by ~10%.
        market_p = float(price)
        our_p = max(0.01, min(0.99, market_p + 0.10))
        edge = our_p - market_p

        rationale: dict[str, Any] = {
            "direction": direction,
            "cluster_size": cluster_size,
            "net_usd_total": net_usd_total,
            "best_ask": float(price),
            "our_p": our_p,
        }

        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=self.name,
            condition_id=market.condition_id,
            token_id=token_id,
            edge=edge,
            conviction=conviction,
            suggested_action=action,
            rationale=rationale,
        )

    def capacity_estimate(self) -> float:
        return 3_000.0

    @staticmethod
    def proposed_price_from_signal(rationale: dict[str, Any]) -> Decimal:
        return Decimal(str(rationale.get("best_ask", 0.5)))

    @staticmethod
    def proposed_size_pct(
        rationale: dict[str, Any],
        bankroll_usd: Decimal,
        max_size_pct: float,
    ) -> float:
        cluster_size = int(rationale.get("cluster_size", 0))
        # Scale linearly with cluster size, cap at max.
        return float(min(max_size_pct, max_size_pct * cluster_size / 5))
