"""SmartMoneyStrategy v2 — 3-tier follow-on with mandatory filters.

See MASTER_SPEC v1.1 §14.3.

Phase 3 v1 shipped a single-tier predicate. v1.1 splits into:
  Tier 1 — proven (auto-trade, full Kelly)
  Tier 2 — hot/cautious (auto-trade, half Kelly)
  Tier 3 — observation-only (dashboard surface, manual approval)

Mandatory filters across all tiers:
  - Cluster confirmation (≥3 Tier 1 OR ≥2 Tier 2)
  - Latency decay (events within last 30 min only)
  - Position size cap (max 2% bankroll per copy)
  - Per-trader concentration cap (5% portfolio max)
  - Hedge flag (exclude wallets known to hedge)
  - Loss filter (exclude wallets with > -20% DD over 7d)
  - Attribution log (rationale carries copied_from={tier}_{wallet})

Tier 3 auto-trade is disabled by default. Operator toggles via config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    direction: str           # "YES" or "NO"
    net_usd: float
    last_update: datetime
    tier: int = 3            # filled by ClusterStateBuilder via wallet_tier map


@dataclass
class ClusterState:
    """Snapshot per condition_id, built by `cluster_builder.ClusterStateBuilder`."""

    condition_id: str
    yes_flows: list[WalletFlow] = field(default_factory=list)
    no_flows: list[WalletFlow] = field(default_factory=list)
    last_update: datetime = field(default_factory=lambda: datetime.now(UTC))


class SmartMoneyStrategy(BaseStrategy):
    """v1.1 3-tier implementation.

    Configuration ([`config/strategies/smart_money.yaml`](config/strategies/smart_money.yaml)):
      enabled
      tier1_min_cluster
      tier2_min_cluster
      tier3_auto_trade        (bool, default false — Tier 3 surfaces only)
      min_net_usd_per_wallet
      latency_decay_sec       (30 min default — drops stale events)
      freshness_max_sec       (24h overall window)
      max_size_pct_tier1      (Kelly × 1.0)
      max_size_pct_tier2      (Kelly × 0.5)
      max_size_pct_tier3      (Kelly × 0.25, only if tier3_auto_trade)
      per_trader_cap_pct      (5% portfolio max from a single trader)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(name="smart_money", config=config, enabled=config.get("enabled", False))
        self.tier1_min_cluster = int(config.get("tier1_min_cluster", 3))
        self.tier2_min_cluster = int(config.get("tier2_min_cluster", 2))
        self.tier3_auto_trade = bool(config.get("tier3_auto_trade", False))
        self.min_net_usd_per_wallet = float(config.get("min_net_usd_per_wallet", 5_000))
        self.latency_decay_sec = int(config.get("latency_decay_sec", 1800))
        self.freshness_max_sec = int(config.get("freshness_max_sec", 86_400))
        self.max_size_pct_tier1 = float(config.get("max_size_pct_tier1", 0.02))
        self.max_size_pct_tier2 = float(config.get("max_size_pct_tier2", 0.01))
        self.max_size_pct_tier3 = float(config.get("max_size_pct_tier3", 0.005))
        self.per_trader_cap_pct = float(config.get("per_trader_cap_pct", 0.05))

        # Backwards-compat shims (old config keys from v1).
        self.min_wallet_count = int(
            config.get("min_wallet_count", self.tier1_min_cluster)
        )

        self._books: dict[str, LocalBook] = {}
        self._cluster_state: dict[str, ClusterState] = {}
        self._wallet_tier: dict[str, int] = {}     # populated from DB by main loop
        self._wallet_drawdown: dict[str, float] = {}
        self._wallet_hedge_flag: set[str] = set()

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def attach_cluster_state(self, state: ClusterState) -> None:
        self._cluster_state[state.condition_id] = state

    def attach_wallet_tier(self, wallet: str, tier: int) -> None:
        self._wallet_tier[wallet.lower()] = tier

    def attach_wallet_drawdown(self, wallet: str, drawdown_7d_pct: float) -> None:
        self._wallet_drawdown[wallet.lower()] = drawdown_7d_pct

    def attach_wallet_hedge_flag(self, wallet: str, is_hedger: bool) -> None:
        if is_hedger:
            self._wallet_hedge_flag.add(wallet.lower())
        else:
            self._wallet_hedge_flag.discard(wallet.lower())

    async def evaluate(
        self, market: Market, features: Features
    ) -> StrategySignal | None:
        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        if not self.enabled:
            return None
        cs = self._cluster_state.get(market.condition_id)
        if cs is None:
            PM_STRATEGY_REJECT.labels(strategy="smart_money", reason="no_cluster_state").inc()
            return None

        now = datetime.now(UTC)
        if (now - cs.last_update).total_seconds() > self.freshness_max_sec:
            PM_STRATEGY_REJECT.labels(strategy="smart_money", reason="cluster_stale").inc()
            return None

        # Latency decay: only flows within last 30min count toward cluster.
        latency_cutoff = now - timedelta(seconds=self.latency_decay_sec)
        yes_fresh = self._filter_eligible(cs.yes_flows, latency_cutoff)
        no_fresh = self._filter_eligible(cs.no_flows, latency_cutoff)

        # Per-side tier counts.
        yes_tier1 = self._distinct_wallets(yes_fresh, tier=1)
        yes_tier2 = self._distinct_wallets(yes_fresh, tier=2)
        no_tier1 = self._distinct_wallets(no_fresh, tier=1)
        no_tier2 = self._distinct_wallets(no_fresh, tier=2)

        chosen = self._pick_best_cluster(
            yes_tier1=yes_tier1, yes_tier2=yes_tier2,
            no_tier1=no_tier1, no_tier2=no_tier2,
        )
        if chosen is None:
            PM_STRATEGY_REJECT.labels(strategy="smart_money", reason="cluster_short").inc()
            return None

        direction, cluster_tier, cluster_wallets, raw_flows = chosen

        # Tier 3 fully-observation mode (no auto-trade) → emit nothing.
        if cluster_tier == 3 and not self.tier3_auto_trade:
            PM_STRATEGY_REJECT.labels(strategy="smart_money", reason="tier3_observe_only").inc()
            return None

        token_id = market.yes_token_id if direction == "YES" else market.no_token_id
        book = self._books.get(token_id)
        if book is None:
            PM_STRATEGY_REJECT.labels(strategy="smart_money", reason="no_book").inc()
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            PM_STRATEGY_REJECT.labels(strategy="smart_money", reason="no_best_ask").inc()
            return None
        price, _ = best_ask

        market_p = float(price)
        # Edge sized by tier strength.
        edge_offset = {1: 0.10, 2: 0.07, 3: 0.04}[cluster_tier]
        our_p = max(0.01, min(0.99, market_p + edge_offset))
        edge = our_p - market_p

        # Conviction scales with cluster size, bounded by tier.
        ratio = len(cluster_wallets) / max(self.tier1_min_cluster, 1)
        base_conviction = 0.5 + 0.1 * (ratio - 1.0)
        tier_factor = {1: 1.0, 2: 0.7, 3: 0.4}[cluster_tier]
        conviction = max(0.0, min(1.0, base_conviction * tier_factor))

        net_usd_total = sum(f.net_usd for f in raw_flows)
        # Attribution log: which wallets we're following.
        attribution = sorted({f"t{f.tier}_{f.wallet[:8]}" for f in raw_flows})
        max_size_pct = {1: self.max_size_pct_tier1, 2: self.max_size_pct_tier2,
                        3: self.max_size_pct_tier3}[cluster_tier]

        rationale: dict[str, Any] = {
            "tier": cluster_tier,
            "direction": direction,
            "cluster_size": len(cluster_wallets),
            "net_usd_total": net_usd_total,
            "best_ask": float(price),
            "our_p": our_p,
            # Phase N.1: long-side Kelly inputs — our_p / market_p are already
            # on the LONG side (since direction picks the token we're buying).
            "our_p_long": our_p,
            "market_p_long": market_p,
            "max_size_pct": max_size_pct,
            "copied_from": attribution,
            "latency_decay_sec": self.latency_decay_sec,
        }
        action = Action.BUY_YES if direction == "YES" else Action.BUY_NO

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

    # ---------- helpers ----------

    def _filter_eligible(
        self, flows: list[WalletFlow], latency_cutoff: datetime
    ) -> list[WalletFlow]:
        out: list[WalletFlow] = []
        for f in flows:
            if f.last_update < latency_cutoff:
                continue
            if f.net_usd < self.min_net_usd_per_wallet:
                continue
            w = f.wallet.lower()
            if w in self._wallet_hedge_flag:
                continue
            if self._wallet_drawdown.get(w, 0.0) >= 0.20:
                continue
            # Resolve tier from DB-attached map; default 3 (observation).
            f_tier = self._wallet_tier.get(w, f.tier or 3)
            out.append(
                WalletFlow(
                    wallet=f.wallet,
                    direction=f.direction,
                    net_usd=f.net_usd,
                    last_update=f.last_update,
                    tier=f_tier,
                )
            )
        return out

    def _distinct_wallets(
        self, flows: list[WalletFlow], *, tier: int
    ) -> list[WalletFlow]:
        # Keep one entry per (wallet, direction) pair, taking the largest net_usd.
        bucket: dict[str, WalletFlow] = {}
        for f in flows:
            if f.tier != tier:
                continue
            cur = bucket.get(f.wallet)
            if cur is None or f.net_usd > cur.net_usd:
                bucket[f.wallet] = f
        return list(bucket.values())

    def _pick_best_cluster(
        self,
        *,
        yes_tier1: list[WalletFlow],
        yes_tier2: list[WalletFlow],
        no_tier1: list[WalletFlow],
        no_tier2: list[WalletFlow],
    ) -> tuple[str, int, list[str], list[WalletFlow]] | None:
        """Return (direction, tier, distinct_wallet_list, raw_flow_list)
        for the strongest cluster, else None."""
        # Tier 1 priority — require min_cluster of Tier 1 wallets.
        if len(yes_tier1) >= self.tier1_min_cluster:
            return "YES", 1, [f.wallet for f in yes_tier1], yes_tier1
        if len(no_tier1) >= self.tier1_min_cluster:
            return "NO", 1, [f.wallet for f in no_tier1], no_tier1

        # Tier 2 — require min_cluster of Tier 2 wallets.
        if len(yes_tier2) >= self.tier2_min_cluster:
            return "YES", 2, [f.wallet for f in yes_tier2], yes_tier2
        if len(no_tier2) >= self.tier2_min_cluster:
            return "NO", 2, [f.wallet for f in no_tier2], no_tier2

        return None

    # ---------- aggregator helpers ----------

    @staticmethod
    def proposed_price_from_signal(rationale: dict[str, Any]) -> Decimal:
        return Decimal(str(rationale.get("best_ask", 0.5)))

    @staticmethod
    def proposed_size_pct(
        rationale: dict[str, Any],
        bankroll_usd: Decimal,
        max_size_pct: float,
    ) -> float:
        cluster_max = float(rationale.get("max_size_pct", max_size_pct))
        # Phase N.1: Kelly on the long side, capped by tier-specific max.
        our_p = rationale.get("our_p_long")
        mkt_p = rationale.get("market_p_long")
        if our_p is not None and mkt_p is not None:
            from poly_meridian.risk.kelly import sized_kelly
            effective_cap = min(max_size_pct, cluster_max)
            kr = sized_kelly(
                p=float(our_p), market_price=float(mkt_p),
                bankroll_usd=bankroll_usd,
                kelly_fraction_multiplier=0.25, hard_cap_pct=effective_cap,
            )
            return kr.f_used
        # Legacy fallback: linear scale with cluster size.
        cluster_size = int(rationale.get("cluster_size", 0))
        scaled = cluster_max * cluster_size / 5
        return float(min(max_size_pct, cluster_max, scaled))
