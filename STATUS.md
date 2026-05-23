# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 4 — Backtest + StatQuant ✅ (skeleton complete, awaiting live verification)

### Done
- **Fee schedule** ([`execution/fees.py`](src/poly_meridian/execution/fees.py))
  - Per-category Polymarket Intl taker bps (Crypto 180, Politics 100, Sports 75, Geopolitics 0, etc.) from §2.2
  - `fee_bps_for_price()` scales by closeness to 50¢ (peak at 0.50, drops to ~30% of peak at extremes)
  - US-mode flat 30bps taker / 20bps maker rebate
- **PaperExecutor fee integration** ([`execution/paper_executor.py`](src/poly_meridian/execution/paper_executor.py))
  - `attach_category()` registers per-token category for fee lookup
  - `_apply_fill()` computes per-fill fee, passes (order, filled_qty, vwap, fee) to `on_fill` callback
  - Maker (GTC/GTD) vs taker (FOK/FAK) distinguished automatically
- **Pipeline updated** ([`pipeline.py`](src/poly_meridian/pipeline.py))
  - Categories propagate from `register_market` into executor
  - `on_fill` callback signature widened to accept (qty, price, fee); backwards-compat with old signature preserved
- **TA features** ([`features/ta_features.py`](src/poly_meridian/features/ta_features.py))
  - `rolling_volatility`, `rolling_zscore`, `momentum`, `rsi` (Wilder), `trade_count`, `rolling_volume`
  - `RollingPriceWindow` bounded deque for per-token price history
- **StatQuantStrategy** ([`strategies/stat_quant.py`](src/poly_meridian/strategies/stat_quant.py))
  - 4 sub-signals: `mean_reversion` (z-score), `momentum` (lookback return + volume gate), `vol_breakout` (low-vol → expansion), `time_decay` (< horizon hours + deviation from 50¢)
  - Each sub-signal carries `max_size_pct` in rationale so the aggregator caps per-position size correctly
  - `pipeline.tick()` pushes mid-prices into the rolling window every cycle
- **Aggregator** ([`strategies/aggregator.py`](src/poly_meridian/strategies/aggregator.py))
  - `_resolve_helper` matches `stat_quant.<sub>` strategy names to their helpers via base-prefix lookup
- **Backtest engine** ([`backtest/`](src/poly_meridian/backtest))
  - `metrics.py` — Sharpe, Sortino, Calmar, max DD, total/CAGR/win rate/profit factor/expectancy, `meets_live_gate()` enforcing §18 thresholds
  - `replay.py` — `Replayer` runs strategies + risk + paper executor over `HistoricalDataset`; produces equity curve + trade P&Ls + order list; deterministic
  - `walkforward.py` — `make_folds()` + `slice_dataset()` for rolling train/test splits
  - `reports.py` — markdown + JSON report writer with live-gate verdict
  - `loader.py` — pulls `orderbook_snapshots` from Timescale into a `HistoricalDataset`
- **CLI** ([`cli.py`](src/poly_meridian/cli.py))
  - `backtest --strategy X --days N --nav 100000 --tick-sec 60 --out reports/`
  - `walkforward --total-days 180 --train-days 60 --test-days 15`
  - Both write reports as `<slug>-<timestamp>.md` + `.json`
- **Configs**
  - [`config/strategies/stat_quant.yaml`](config/strategies/stat_quant.yaml) — all 4 sub-signal thresholds
  - [`config/base.yaml`](config/base.yaml) — `stat_quant` added to enabled list
- **Makefile** — new `make backtest` target

### Tests (6 new files, all pure compute / asyncio, no network)
- `test_fees.py` — category presence, geopolitics zero, unknown→default, peak-at-50¢ fall-at-edges, taker/maker USD estimate, US rebate
- `test_ta_features.py` — volatility / z-score / momentum / RSI edge cases + RollingPriceWindow capacity
- `test_stat_quant_strategy.py` — disabled, mean reversion triggers, momentum triggers, window too small, time decay
- `test_metrics.py` — total return, max DD, returns from equity, Sharpe/Sortino consistency, CAGR, trade stats, compute_all, live gate pass/fail
- `test_walkforward.py` — fold generation, empty case, inverted range, slice filter
- `test_replay.py` — synthetic arb opportunity → fill → positive NAV, empty dataset

### Phase 4 acceptance gate
| Check | Status | Notes |
|---|---|---|
| All ABCs still abstract | ✅ | Smoke test still passes |
| Risk gate enforced (incl. backtest replay) | ✅ | Replayer routes signals through DefaultRiskPolicy.evaluate() before executor |
| Fee-aware paper P&L | ✅ | Per-category bps applied per fill, ledger.apply_fill receives fee |
| `make test` passes (32 test files) | ⏳ | All Phase 4 tests pure compute / asyncio |
| Backtest report generates from DB | ⏳ | Needs live DB with captured orderbook_snapshots history |
| 90-day backtest meets §18 live gate (Sharpe>1.5, MaxDD<25%, WinRate>52%, ≥200 trades) | ⏳ | Depends on observed data — gate will be reported per-run |
| Walk-forward folds produced | ✅ | `make_folds` deterministic; CLI `walkforward` enumerates them |

### Deliberate deferrals
- **Slippage re-fit from realized fills:** the §16.2 slippage model still uses a=50, b=1.2 (Phase 2 guesses). Re-fitting requires N≥100 real paper fills; Phase 5 will do this once we have paper run data.
- **Twitter provider real impl** — still stubbed. Phase 5 alongside fundamentals.
- **Cluster state builder** — on-chain feed flows to queue but the builder task that converts CTF transfers → ClusterState isn't wired (needs real seeded smart wallets first).
- **Walk-forward runner** — CLI lists folds today; full multi-fold replay loop ships in Phase 5 alongside hyperparameter sweep.

## Pending design notes
- **Operator UI (Bloomberg Terminal style)** — Phase 5 spec doc + implementation start.

## Open questions
1. Smart wallet seeds — still placeholder. Want a scraper for `polymarket.com/leaderboard` next phase?
2. Railway TimescaleDB (still pending)
3. Twitter provider — bring it on in Phase 5 or push to Phase 6?

## How to verify Phase 4 locally
```bash
# Unit tests (no network, no DB)
make test                       # 32 test files

# Live verification (needs DB with captured history)
make up                         # bring stack up
docker compose run --rm agent python -m poly_meridian.cli walkforward \
  --strategy arbitrage --total-days 180 --train-days 60 --test-days 15

# Once orderbook_snapshots has at least a few days of data:
docker compose run --rm agent python -m poly_meridian.cli backtest \
  --strategy arbitrage --days 30 --nav 100000

# Report lands in ./reports/<slug>-<ts>.md (or .json)
```

## Next: Phase 5 — Fundamentals + Hardening (Hafta 9-10)
- `FundamentalsStrategy` for Politics (poll aggregator + bias correction), Sports (Elo), Crypto (TA + on-chain), Macro (Fed calendar)
- Twitter provider real impl
- Slippage model re-fit from realized fills
- Chaos engineering: API timeout / DB disconnect / RPC fail simulations
- Full disaster recovery drill
- Walk-forward multi-fold runner
- Operator UI scaffold (Bloomberg-style) — Phase 5 spec + Tauri/Textual decision
