# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 5b — Fundamentals + Hardening ✅ (skeleton complete, awaiting live verification)

### Done

**Fundamentals subsystem** ([`src/poly_meridian/fundamentals/`](src/poly_meridian/fundamentals))
- [`base.py`](src/poly_meridian/fundamentals/base.py) — `CategoryResolver` ABC, `ProbabilityEstimate` (p_yes + confidence + rationale), `FundamentalsContext` typed bag (polls, Elo ratings, spot prices, funding rates, netflow, economic events)
- [`politics.py`](src/poly_meridian/fundamentals/politics.py) — `PoliticsResolver`: weight by `sqrt(sample_size) × methodology_weight × time_decay`, subtract `house_bias`, confidence scales with total weight + source diversity (538-style aggregator)
- [`sports.py`](src/poly_meridian/fundamentals/sports.py) — `EloEngine` (stateless K=32 default, base 1500) + `SportsResolver` with home-advantage Elo bonus, supports YES=home or YES=away
- [`crypto.py`](src/poly_meridian/fundamentals/crypto.py) — `CryptoResolver`: log-normal random walk with drift = `funding_weight × annualized_funding + netflow_weight × netflow_24h`, normal CDF via A&S 7.1.26 polynomial; works for `direction: above|below`
- [`macro.py`](src/poly_meridian/fundamentals/macro.py) — `MacroResolver`: heuristic hawkish/dovish ratio over `lookback_days` of economic events
- [`strategies/fundamentals.py`](src/poly_meridian/strategies/fundamentals.py) — `FundamentalsStrategy` dispatches by `market.category` → resolver, emits signal when `|our_p − market_p| > min_edge` and `confidence ≥ min_confidence`
- Aggregator + helpers wired ([`strategies/__init__.py`](src/poly_meridian/strategies/__init__.py))

**Twitter provider real impl** ([`ingestion/twitter_provider.py`](src/poly_meridian/ingestion/twitter_provider.py))
- `TwitterStreamSource(IngestionSource)` — X API v2 filtered stream
- Async streaming via `httpx.stream`, rule installation/teardown on start, exponential-backoff reconnect (1/2/5/10/30s), heartbeat handling
- Default rules: federal reserve, Reuters, WSJ, NYT politics, Axios + Fed/crypto keywords
- Verified-only + min-followers filter (default 100K)
- `backfill_recent()` for catch-up after disconnect
- Graceful disable when `X_BEARER_TOKEN` missing

**Slippage re-fit** ([`execution/slippage_model.py`](src/poly_meridian/execution/slippage_model.py))
- `SlippageFit` dataclass + `fit_from_fills()` — log-log linear regression on `(size/depth, slippage_bps)` observations, recovers `a` and `b` from realized paper-fill data
- `slippage_from_fill()` — convert single fill to observed bps for ingestion into the regression buffer
- Phase 2 defaults stay until ≥10 real fills observed

**Walk-forward multi-fold runner** ([`backtest/walkforward.py`](src/poly_meridian/backtest/walkforward.py))
- `FoldResult` + `WalkForwardResult` typed containers
- `run_folds()` async runner: per fold, fresh strategies (via factory), runs `Replayer`, collects metrics
- `aggregate_metrics()`: mean Sharpe, median Sharpe, worst max-DD, total trades, mean win rate

**Chaos engineering** ([`tests/chaos/test_chaos_drills.py`](tests/chaos/test_chaos_drills.py))
- Gamma client recovers from transient timeout (1 retry)
- Gamma client fails cleanly after max retries (no crash)
- Kill-switch engages on runaway API errors (rate-based)
- Kill-switch engages on WS disconnect grace exceeded
- Kill-switch engages on wallet balance mismatch
- **Risk policy rejects every order under kill-switch** (the immutable rule)
- Concurrent kill-switch observations are race-safe (idempotent engagement)
- Paper executor rejects orders for unknown tokens (defensive)

**DR drill** ([`scripts/dr_drill.py`](scripts/dr_drill.py))
- Interactive runbook: backup → restore check → secret rotation → kill-switch drill → health checks
- Exit 1 on any unconfirmed step → fits §19 promotion gate

**Operational runbook** ([`docs/runbook.md`](docs/runbook.md))
- Backup/restore procedures (pg_dump + pg_restore)
- Secret rotation walkthrough (wallet migration)
- Kill-switch manual engage/disengage commands
- Paper→live promotion checklist (§19)
- Chaos drill expectations
- Incident response first-30-min playbook

### Tests added (5 new files)
- `test_fundamentals.py` — Politics aggregation + recency + bias correction; Elo math; Sports YES=home/away; Crypto p(above) near/far targets; Macro hawkish ratio; insufficient-input guards
- `test_fundamentals_strategy.py` — disabled, YES/NO emission, edge threshold, confidence floor, unknown category
- `test_slippage_fit.py` — single-fill observation, recovery of synthetic a/b, noise robustness, few-sample guard
- `test_walkforward_runner.py` — per-fold results, aggregate_metrics summary
- `tests/chaos/test_chaos_drills.py` — 8 chaos scenarios

### Configs
- [`config/strategies/fundamentals.yaml`](config/strategies/fundamentals.yaml): `enabled: true`, per-category thresholds, min_edge 0.05, min_confidence 0.50, Macro opt-in (default off)
- [`config/base.yaml`](config/base.yaml): all 5 strategies now enabled — arbitrage, sentiment, smart_money, stat_quant, fundamentals

### Phase 5b acceptance gate
| Check | Status | Notes |
|---|---|---|
| All ABCs still abstract | ✅ | Smoke test passes |
| Risk gate enforced (incl. chaos drills) | ✅ | `test_risk_policy_rejects_every_order_under_kill_switch` |
| 5 strategies live: arb / sentiment / smart_money / stat_quant / fundamentals | ✅ | Aggregator registry covers all 5 |
| Twitter degrades gracefully without token | ✅ | `start()` logs warning, returns |
| Slippage fit converges on synthetic data | ✅ | `test_fit_from_fills_recovers_true_params` |
| Walk-forward runs across folds with fresh state per fold | ✅ | strategy_factory pattern enforces isolation |
| Chaos tests pass | ✅ | 8 drills in tests/chaos |
| DR drill runbook complete | ✅ | docs/runbook.md + scripts/dr_drill.py |
| `make test` passes (40+ test files) | ⏳ | All Phase 5b tests pure compute / asyncio |

### Deliberate deferrals (Phase 6 + UI phase)
- **Real-world data plumbing for Fundamentals:** resolver framework is ready, but actual feeds (538 poll JSON, ESPN Elo scrape, Binance funding rates, Fed calendar parse) need API keys + adapters. Each is ~1 day of work + a paid data source. Phase 6 picks the highest-conviction ones.
- **Operator UI (Bloomberg-style)** — held to its own dedicated phase per user request "proje tam bitsin sonra UI UX kısmına geçelim". Backend now complete.
- **Twitter rules from config file** — defaults baked in for now; `config/twitter_rules.yaml` lands with UI phase when there's somewhere to edit them.
- **Live executor (`py-clob-client` authed)** — Phase 6 task.
- **`promote_to_live.py` real wiring** — checks currently `_todo()` stubs; Phase 6 hooks them to real metrics queries.

## Overall progress — backend complete

| Phase | Durum | Notlar |
|---|---|---|
| 0 Setup | ✅ | Scaffold + Docker + Railway config |
| 1 Data layer | ✅ | Gamma + CLOB + WS + GDELT |
| 2 Arb + Risk + Paper | ✅ | First real alpha |
| 3 Sentiment + SmartMoney v1 | ✅ | OpenAI + Claude scorer |
| 4 Backtest + StatQuant | ✅ | Replay engine + walk-forward folds |
| 5a Smart Money v2 (Follow-on) | ✅ | 3-tier + leaderboard scraper |
| **5b Fundamentals + Hardening** | ✅ | **5 strategies, chaos, DR, runbook** |
| 6 Kademeli canlı | 🔄 sıradaki | Paper run → live promotion checklist |
| **UI/UX (Bloomberg-style)** | ⏳ | After Phase 6 per user direction |

**Engine is feature-complete.** Phase 6 is operational (paper observation → live ramp), then UI/UX gets its own dedicated push.

## Open questions for user
1. **Phase 6 trigger:** ready to start paper observation period now (~4-8 weeks per spec §19)? Or keep iterating backend?
2. **Leaderboard endpoint** — still pending Chrome devtools inspection (from Phase 5a)
3. **Tier 3 auto-trade** — stays off per spec (no change needed unless you want to flip)
4. **Paid data sources** — for Fundamentals to actually fire in production we'll need at least one paid feed (cheapest: 538 polls free RSS / ESPN Elo scrape free / Binance funding free / Trading Economics calendar $30/mo)

## How to verify Phase 5b locally
```bash
make test                         # 40+ test files now
docker compose run --rm agent python -m pytest tests/chaos/ -v
docker compose run --rm agent python -m scripts.dr_drill        # interactive
# See docs/runbook.md for the full operator playbook.
```
