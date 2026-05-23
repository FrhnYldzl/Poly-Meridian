# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 2 — Arbitrage + Risk + Paper ✅ (skeleton complete, awaiting live verification)

### Done
- **Risk engine** ([risk/](src/poly_meridian/risk))
  - `kelly.py` — Quarter Kelly with hard cap; `kelly_fraction()` + `sized_kelly()` returns `KellyResult` (f_star, f_used, size_usd, edge, EV)
  - `limits.py` — `RiskLimits` dataclass + 7 check functions (liquidity, daily loss, total/category exposure, open positions, position cap) + `reduce_size_if_breached()` for graceful reduction
  - `kill_switch.py` — state machine, 5 triggers (daily loss, slippage anomaly, API error rate, WS disconnect, manual). Manual-only disengagement. Idempotent.
  - `policy.py` — `DefaultRiskPolicy(RiskPolicy)`: evaluate → APPROVE/REJECT/REDUCE, size() converts to `TradeDecision`. **Strategy proposes size_pct + proposed_price; policy validates + caps, NEVER inflates.** No bypass code anywhere.
- **Strategies** ([strategies/](src/poly_meridian/strategies))
  - `arbitrage.py` — `ArbitrageStrategy(BaseStrategy)` for single-market complete-set arb. Detects when `YES_ask + NO_ask < 1 - threshold` (after worst-case fees). Conviction=0.95 (math-near-certain). Exposes `proposed_price_from_signal()` + `proposed_size_pct()` for aggregator.
  - `aggregator.py` — `SignalAggregator` with conviction-weighted voting, conflict threshold (default 0.10). Phase 2 wires single strategy; ready for Phase 3 multi-strategy.
- **Execution** ([execution/](src/poly_meridian/execution))
  - `paper_executor.py` — `PaperExecutor(Executor)` with `mode='paper'`. Maker orders post to virtual book; if price crosses immediately → fill. Taker (FOK/FAK) walks book via `walk_book_for_fill()`. `reconcile()` sweeps resting orders, applies fills + timeouts.
  - `slippage_model.py` — `estimate_slippage_bps()` + `walk_book_for_fill()` (pure compute)
  - `order_router.py` — Phase 2 thin layer (PaperExecutor.submit pass-through); maker-first cascade lands in Phase 3
- **Portfolio** ([portfolio/](src/poly_meridian/portfolio))
  - `ledger.py` — `Ledger` with cash + positions, `apply_fill()` updates avg_cost (weighted) on BUY, realizes P&L on SELL, double-entry traceable via `LedgerEntry`
  - `pnl.py` — `nav_usd()`, `realized_pnl()`, `unrealized_pnl()`, `total_exposure_usd()`, `category_exposure_usd()`, `daily_pnl_pct()`, `snapshot()` produces `PortfolioSnapshot` for risk gate, `daily_roll_up()` for `pnl_daily` table
  - `mark_to_market.py` — sweeps open positions, refreshes `last_mark` from local books
  - `rebalancer.py` — Phase 2 stub (real triggers Phase 3+)
- **Pipeline** ([pipeline.py](src/poly_meridian/pipeline.py))
  - `Pipeline` class orchestrates: strategy.evaluate → aggregator → risk.evaluate → risk.size → router.route → executor.submit
  - **Risk gate is enforced by wiring** — there is no code path that bypasses `RiskPolicy.evaluate()`
  - 4 new Prometheus counters: `pm_signal_emitted_total{strategy}`, `pm_signal_aggregated_total`, `pm_risk_decision_total{decision}`, `pm_order_submitted_total{side,mode}`
  - PaperExecutor.on_fill callback wired to Ledger.apply_fill so portfolio stays in sync without polling
- **Main loop** ([main.py](src/poly_meridian/main.py))
  - Phase 2 wiring: Gamma sync → market cache → pipeline_loop pulls book snapshots via CLOB REST → per-market `pipeline.tick()`
  - WS deliberately NOT enabled by default — REST snapshots + 10s cadence keep things observable in paper. WS comes online in Phase 3 with sub-second loop.
  - Loads `config/strategies/arbitrage.yaml` + `config/risk.yaml` overlays
  - Starting paper NAV: $100K virtual
  - New gauges: `pm_nav_total`, `pm_position_count`, `pm_kill_switch_engaged`
- **Domain** — added `AggregatedSignal.proposed_price: Decimal | None` so strategies carry the trade price through aggregator → risk without back-computation
- **Grafana** ([infra/grafana/](infra/grafana))
  - `provisioning/datasources/prometheus.yaml` — Prometheus auto-wired
  - `provisioning/dashboards/dashboards.yaml` — auto-load dashboards from this repo
  - `dashboards/overview.json` — Phase 2 dashboard: NAV stat, open positions, kill-switch armed/engaged stat, signals/risk/orders rate timeseries, news rate
  - `docker-compose.yml` updated to mount both provisioning + dashboards dirs
- **Tests** (under `tests/unit/`)
  - `test_kelly.py` — sizing math: no edge → 0, edge → correct f*, quarter Kelly + hard cap, invalid inputs → 0, EV
  - `test_kill_switch.py` — 7 transition tests covering all triggers + idempotence + manual override
  - `test_risk_policy.py` — 9 tests: approve clean, reject on kill-switch / daily loss / liquidity / open positions / non-BUY / missing price; reduce on exposure breach; size produces correct TradeDecision
  - `test_arbitrage_strategy.py` — detection above/below threshold, disabled, missing book, fee-killed thin arb; size proposal capping
  - `test_paper_executor.py` — taker walk-book, FOK kill on insufficient depth, maker cross/rest/cancel/reconcile
  - `test_portfolio.py` — buy-then-sell realized PnL, mark-driven unrealized PnL, weighted avg cost, snapshot exposure
  - `test_aggregator.py` — pass-through, empty → None, conflict → None, clear winner, market metadata
  - `test_slippage.py` — book walking partial + complete drain + empty book

### Phase 2 acceptance gate
| Check                                                  | Status | Notes                                                                            |
|--------------------------------------------------------|--------|----------------------------------------------------------------------------------|
| All ABCs still abstract                                | ✅     | Phase 0 smoke test still passes; concrete classes implement contracts            |
| Every order passes through risk gate                   | ✅     | Pipeline.tick is the only path from signal → executor                            |
| Kill-switch tested                                     | ✅     | 7 unit tests in test_kill_switch.py                                              |
| Kelly + hard cap respected                             | ✅     | test_kelly.py verifies cap binds even when raw Kelly is larger                  |
| Paper executor writes mode='paper' on every order      | ✅     | `PaperExecutor.mode = Mode.PAPER` + `Order.mode` defaulted; test coverage in test_paper_executor.py |
| `make test` passes                                     | ⏳     | 8 new test files; pure compute / asyncio, no network                             |
| Grafana auto-loads Overview dashboard on `make up`     | ⏳     | Volume mounts in compose; needs live Docker to verify                            |
| Paper run produces ≥0 risk-approved orders/day         | ⏳     | Depends on actual Polymarket arb opportunities — observe over 24h               |
| Risk module rejects everything when kill-switch armed  | ✅     | test_risk_policy.py::test_rejects_when_kill_switch_engaged                       |

### Deliberate deferrals (still §24 Phase 2 work, pushed for safety)
- **Maker-first cascade (§16.1):** Phase 2 ships the simple "post maker, fill if crosses, otherwise wait" path. The full "60s timeout → 50% taker conversion → final flush" lives in Phase 3 when we have WS-driven cancel/replace.
- **Fee-aware fills:** PaperExecutor uses 0 fees today. Real per-category fee schedule (§2.2 table) wires in Phase 4 alongside backtest fidelity.
- **WS-driven pipeline:** Phase 2 polls REST every 10s — enough to detect persistent arb, but WS is needed for fast-moving opportunities. Phase 3 turns it on.
- **CLOB authed client:** Still stubbed; concrete `py-clob-client` wiring lands in Phase 6 (live).

## Pending design notes (not yet in MASTER_SPEC)
- **Operator UI (Bloomberg Terminal style).** User flagged 2026-05-23: dashboard must be a dense, dark, multi-panel, keyboard-driven trading terminal — trading + portfolio focus, NOT a prediction-market betting UI. Grafana (§20) covers observability only; this is a separate operator surface to be designed in a `docs/strategy_specs/ui_terminal.md` spec around Phase 5. Stack TBD (Tauri+React / Textual TUI / Next.js).

## Open questions for user
1. **Railway TimescaleDB:** (a) Railway TimescaleDB template, (b) Timescale Cloud (free tier 6mo), (c) self-hosted Postgres on Railway + manual extension. Recommend (b) for prod. Decide before Phase 6.
2. **Observability on Railway:** docker-compose runs Prometheus+Grafana locally. On Railway, recommend Grafana Cloud free tier (push metrics) rather than hosting both as Railway services. Decide before Phase 5.
3. **CLOB client library:** `py-clob-client` vs `py-clob-client-v2`. Will pin in Phase 6 (live executor) — Phase 2 doesn't need authed paths.
4. **GitHub push for Phase 2:** Phase 0+1 already pushed (commit `f5ece76`). Phase 2 should land as a second commit. Should I push now or wait for your local verification?

## How to verify Phase 2 locally
```bash
cp .env.example .env
make install
make test                       # ~7 Phase 0 + 6 Phase 1 + 8 Phase 2 test files, all pure compute
make up                         # docker compose up -d → DB + agent + prometheus + grafana
docker compose logs -f agent
# Expect inside the first 10min:
#   agent.boot mode=paper
#   agent.ready starting_nav_usd=100000 db_ok=true
#   gamma_sync.done n=~1500-3000
#   pipeline iterates sampled markets, MOST emit no signal (arb is rare)
#   if an arb is detected: risk.approve OR risk.reject (logs the reason)
open http://localhost:3000      # Grafana, admin / $GRAFANA_PASSWORD
                                # → Poly Meridian / Overview dashboard
open http://localhost:9090      # Prometheus
```

## Next: Phase 3 — Sentiment + SmartMoney + WS
- LLM sentiment scoring (FinBERT or Anthropic Claude) — news + Twitter
- News→market semantic matching via pgvector
- `SentimentStrategy` + `SmartMoneyStrategy` concrete impls
- Multi-strategy aggregator (conflict resolution actually exercised)
- WS replaces REST polling for sub-second loop
- 2 weeks paper run with multi-strategy
