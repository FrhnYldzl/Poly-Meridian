# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 5a — Smart Money v2 (Follow-on) ✅ (skeleton complete, awaiting live verification)

### Done

**Spec update — `MASTER_SPEC` v1.0 → v1.1**
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — versioning log with rationale, risks, sections touched
- `MASTER_SPEC.md` updated inline: §11.7 (leaderboard provider), §12 (smart_wallets v2 columns), §14.3 (3-tier rewrite with mandatory filters, latency decay, exit signal tracking)
- Risks listed in spec body: survivorship bias, reflexivity, adverse selection, manipulation, hedge≠strategy

**DB schema (v1.1)**
- `smart_wallets` table extended: `tier`, `category_focus`, `last_7d_pnl`, `recency_score`, `hedge_flag`, `drawdown_7d_pct`
- Migration: [`scripts/migrations/001_smart_wallets_v2.sql`](scripts/migrations/001_smart_wallets_v2.sql) (idempotent ALTER TABLE for existing deployments)
- [`bootstrap_db.sh`](scripts/bootstrap_db.sh) updated for fresh installs
- Indexes: `idx_smart_wallets_tier`, `idx_smart_wallets_category`

**Leaderboard provider** ([`ingestion/leaderboard_provider.py`](src/poly_meridian/ingestion/leaderboard_provider.py))
- `LeaderboardProvider` tries 3 candidate URLs in order (data-api → gamma-api → polymarket.com/api), normalizes envelope shapes (`[]` / `{"data":[]}` / `{"leaderboard":[]}`)
- `LeaderboardEntry` typed dataclass with address, lifetime/recent PnL, win rate, trade count, drawdown, category focus
- `classify_tier()` pure function returning 1/2/3 from configurable `TierThresholds` (Tier 1: >$500K PnL, >55% win, >200 trades, <20% DD; Tier 2: $50K+ last 30d, 52%+ win; else Tier 3)
- Tolerant payload normalization — handles 0..1 vs 0..100 win-rate formats, ISO/timestamp/datetime, missing fields

**Cron** ([`scripts/refresh_smart_wallets.py`](scripts/refresh_smart_wallets.py))
- Fetches leaderboard → classifies tier → upserts via [`storage/smart_wallets.py`](src/poly_meridian/storage/smart_wallets.py)
- Recompute `recency_score` after each refresh
- Suggested schedule: daily 02:00 UTC. Exit code 1 if empty result (operator must populate manually).

**Storage writers** ([`storage/smart_wallets.py`](src/poly_meridian/storage/smart_wallets.py))
- `upsert_smart_wallet` (idempotent, preserves existing fields when new value is NULL)
- `list_wallets_by_tier`, `list_eligible_wallets` (auto-filters by drawdown threshold + hedge_flag)
- `count_by_tier` for Grafana metrics
- `update_recency_scores` (exponential decay over 30-day half-life)

**Cluster state builder** ([`strategies/cluster_builder.py`](src/poly_meridian/strategies/cluster_builder.py))
- `ClusterStateBuilder` consumes `PolygonOnchainSource.events()` queue → per-`condition_id` `ClusterState`
- Token-id → (condition_id, direction) lookup table populated by main loop after Gamma sync
- Latency decay built-in: events outside `decay_sec` excluded from snapshots
- Attaches fresh state to `SmartMoneyStrategy` whenever cluster updates

**SmartMoneyStrategy v2** ([`strategies/smart_money.py`](src/poly_meridian/strategies/smart_money.py))
- 3-tier logic: Tier 1 (≥3 wallets, full Kelly), Tier 2 (≥2 wallets, half Kelly), Tier 3 (observation-only by default; `tier3_auto_trade=true` activates quarter Kelly)
- Mandatory filters: latency decay (default 30min), per-wallet min net USD, hedge flag exclusion, drawdown filter (-20%/7d)
- Conviction scales with cluster size AND tier (Tier 1 factor 1.0, Tier 2 0.7, Tier 3 0.4)
- **Attribution log** — `rationale.copied_from = ["t1_0xabc12345", ...]` records exactly which wallets triggered the copy
- `proposed_size_pct` respects per-tier max + aggregator max + linear cluster-size scaling

**Follow-On Grafana dashboard** ([`infra/grafana/dashboards/follow_on.json`](infra/grafana/dashboards/follow_on.json))
- 11 panels: tracked wallets, per-tier counts, active clusters, leaderboard freshness, signals per tier (timeseries), cluster-size distribution (histogram p50/p90), copy-trades submitted, Tier 3 surfacing rate, filter rejection reasons

**Tests** (3 new files)
- `test_leaderboard_provider.py` — tier classification (1/2/3), threshold customization, payload normalization, candidate-URL fallthrough (mock httpx)
- `test_smart_money_v2.py` — disabled, Tier 1 cluster signal, Tier 2 lower conviction, Tier 3 auto-trade=false silent, latency decay filters stale, hedge flag excludes, drawdown filter, below-min-net-usd filter
- `test_cluster_builder.py` — token_id parse, value parse, receipt detection, event consumption, stale flow filtering

### Phase 5a acceptance gate
| Check | Status | Notes |
|---|---|---|
| All ABCs still abstract | ✅ | Smoke test passes |
| Risk gate enforced on every order (incl. copy-trades) | ✅ | Pipeline.tick unchanged, still routes via DefaultRiskPolicy |
| Tier 3 cannot auto-execute by default | ✅ | `tier3_auto_trade=false` in config, `test_tier3_default_does_not_auto_trade` enforces |
| Latency decay implemented | ✅ | `test_latency_decay_drops_stale_events` |
| Attribution log in copy-trade rationale | ✅ | `rationale.copied_from` populated; visible in our_orders for forensic trace |
| Per-trader cap | ⏳ | Strategy emits; aggregator caps via `proposed_size_pct`; portfolio-level concentration cap will land in §17 rebalancer (Phase 5b) |
| Loss filter (-20%/7d) | ✅ | `attach_wallet_drawdown` + `_filter_eligible` exclude |
| `make test` passes (35 test files now) | ⏳ | All new tests pure compute / asyncio |
| Schema migration runs cleanly | ⏳ | Idempotent SQL ready; needs live DB to verify |
| Leaderboard endpoint discovery | ⚠️ | 3 candidate URLs documented; **operator must inspect Chrome devtools to confirm exact endpoint** before first cron run |

### Deliberate deferrals to Phase 5b
- **Cluster builder wiring in main loop** — `ClusterStateBuilder` is built but not yet hooked into `_pipeline_loop` (no live on-chain feed without real seeded wallets). Phase 5b ties this together after first leaderboard refresh.
- **Per-trader portfolio concentration cap** — currently per-position max (2% Tier 1), but cross-position concentration enforcement is a portfolio-level concern that belongs in `portfolio/rebalancer.py` (Phase 5b).
- **Exit signal tracking** — spec §14.3 mentions it; Phase 5b implements via `WalletFlow` exit detection + rebalancer trigger.
- **Backtest panel** ("what if we blindly copied last month") — Cowork-recommended; Phase 5b after we have some on-chain replay data.
- **Fundamentals strategy / chaos drill / UI scaffold** — original Phase 5 scope, now Phase 5b.

### Operator action items
1. **Inspect Polymarket's leaderboard endpoint:** Chrome devtools → Network → filter XHR while loading https://polymarket.com/leaderboard/finance/today/volume. Find the JSON call, paste its URL into `DEFAULT_CANDIDATE_URLS` in `leaderboard_provider.py` if different from current candidates.
2. **Run schema migration** once `make up` is alive: `docker compose exec -T db psql -U poly -d poly_meridian < scripts/migrations/001_smart_wallets_v2.sql`
3. **Schedule cron** for `refresh_smart_wallets.py` (Railway cron or system cron). Daily 02:00 UTC is sensible.
4. **Decision on Tier 3 auto-trade:** stays `false` by default. Flip in `config/strategies/smart_money.yaml` only after observing 30+ days of Tier 1/2 follow-on performance.

## Overall phase progress (5 of 7+)

| Phase | Durum | Notlar |
|---|---|---|
| 0 Setup | ✅ | Scaffold + Docker + Railway config |
| 1 Data layer | ✅ | Gamma + CLOB + WS + GDELT |
| 2 Arb + Risk + Paper | ✅ | First real alpha + paper trading |
| 3 Sentiment + SmartMoney v1 | ✅ | OpenAI embeddings + Claude scorer + WS pipeline |
| 4 Backtest + StatQuant | ✅ | Replay engine, walk-forward, 4 sub-signals, fees |
| **5a Smart Money v2 (Follow-on)** | ✅ | 3-tier + cluster builder + dashboard |
| **5b Fundamentals + Hardening + UI** | 🔄 | Politics/Sports/Crypto/Macro, chaos drills, Operator UI |
| 6 Kademeli canlı | ⏳ | $500 live → kademeli 25K scale-up |

## Open questions for user
1. **Leaderboard endpoint confirmation** — see operator action item #1. Easy to test once you have devtools open.
2. **Tier 3 auto-trade default** — stays off, per spec. OK?
3. **Phase 5b scope** — Fundamentals + Twitter + chaos drill + Operator UI is still a lot. Split further or run as one phase?
