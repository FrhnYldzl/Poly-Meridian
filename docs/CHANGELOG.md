# Master Spec Changelog

All notable changes to `docs/MASTER_SPEC.md` are tracked here. The spec
is canonical — version bumps are recorded with date + summary + sections
touched.

## 1.1 — 2026-05-23

Smart Money v2: tiered follow-on system with leaderboard scraper.

**Why:** §14.3 (single-tier SmartMoneyStrategy) was too coarse for real
operation. After analysis of survivorship bias, reflexivity (Polycopy,
PolyTrack, PredictingTop already saturating the space), adverse selection,
and manipulation risk, we split smart-money tracking into 3 tiers with
mandatory filters and dashboard-only Tier 3.

**Sections touched:**
- **§11.6** — `leaderboard_provider.py` added alongside `onchain_provider.py`.
  Polls Polymarket's leaderboard via data-api with HTML fallback. Populates
  `smart_wallets` table on a cron.
- **§12** — `smart_wallets` table gains: `tier`, `category_focus`,
  `last_7d_pnl`, `recency_score`, `hedge_flag`. Backwards-compatible via
  ALTER TABLE in `scripts/migrations/001_smart_wallets_v2.sql`.
- **§14.3** — `SmartMoneyStrategy` rewritten as 3-tier (Tier 1 proven, Tier 2
  hot/cautious, Tier 3 observation-only). Mandatory filters: cluster
  confirmation (≥3 wallets Tier 1, ≥2 wallets Tier 2), latency decay
  (30-min freshness), position size cap (2% bankroll per copy, 5% per
  trader portfolio), hedge check, loss filter (-20% DD/7d → exclude).
  Tier 3 auto-trade is disabled by default; surfaces in dashboard only.
- **§14.6** — Aggregator weights smart-money signals by tier (1.0 / 0.5 /
  flag-only).
- **§20** — New Grafana "Follow-On" dashboard: top 50 trader real-time
  positions with tier badges, aggregate flow chart (24h net YES vs NO),
  "Our copies" panel (last 20 copies + outcomes), "What we missed"
  (top trades not copied + filter reason), backtest panel.
- **§24** — Phase 3 (Smart Money + Sentiment) note: smart-money production
  rollout moved to Phase 5a as a focused sub-phase; Phase 3's pure on-chain
  poller was a scaffold only.

**Risks acknowledged in spec body:**
- Survivorship bias — leaderboard shows winners only
- Reflexivity / crowding — multiple copy-trade tools already in market
- Adverse selection — by the time we see a whale's trade, info is priced
- Hedge ≠ strategy — single position doesn't show full basket
- Manipulation — visible wallets can bait followers
- "Today volume" ≠ PnL — sort by realized profit, not volume
- Time lag — leaderboard is snapshot; on-chain WS still primary feed

**Hard rules preserved:**
- Every order still flows through `RiskPolicy.evaluate()` (immutable rule
  #3 from MASTER_SPEC §15.4)
- `MODE=paper` default (immutable rule #1)
- Tier 3 cannot auto-execute (config-gated)

## 1.0 — Initial spec

Original master document delivered with the project kickoff. See
[`docs/MASTER_SPEC.md`](MASTER_SPEC.md) for the v1.0 baseline content.
