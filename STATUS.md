# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 1 — Data layer ✅ (skeleton complete, awaiting live verification)

### Done
- **Storage layer**
  - [storage/db.py](src/poly_meridian/storage/db.py) — `Database` with asyncpg pool + SQLAlchemy async engine, singleton helpers (`get_db`/`close_db`), password redacted in logs
  - [storage/models.py](src/poly_meridian/storage/models.py) — SQLAlchemy 2.0 typed `Mapped[...]` models mirroring §12 (Market, OrderbookSnapshot, Trade, NewsArticle/Signal, SmartWallet, FeatureSnapshot, StrategySignalRow, OurOrder, PositionRow, PnLDaily)
  - [storage/cache.py](src/poly_meridian/storage/cache.py) — `Cache` Redis wrapper, JSON set/get/delete with TTL
  - [storage/writers.py](src/poly_meridian/storage/writers.py) — idempotent upsert for markets, append for orderbook/feature snapshots, ON CONFLICT DO NOTHING for news articles
- **Ingestion clients**
  - [ingestion/gamma_client.py](src/poly_meridian/ingestion/gamma_client.py) — httpx async, tenacity retry, pagination (50-page bound), `iter_active_markets()`, `get_market()`, `list_active_events()`
  - [ingestion/clob_ws.py](src/poly_meridian/ingestion/clob_ws.py) — `ClobWebsocketSource` implementing `IngestionSource`: subscribe→reconnect-with-backoff loop, 10s heartbeat, queue-based event emission, per-token `LocalBook`
  - [ingestion/book.py](src/poly_meridian/ingestion/book.py) — pure-compute `LocalBook` snapshot/incremental updates, depth-within helpers
  - [ingestion/clob_client.py](src/poly_meridian/ingestion/clob_client.py) — public read-only methods (server_time, book_snapshot, midpoint); auth path deferred to Phase 2 (PaperExecutor) and Phase 6 (live)
  - [ingestion/news_provider.py](src/poly_meridian/ingestion/news_provider.py) — `GdeltNewsSource`: 15min polling, deduped by URL-SHA1, bounded `_seen_ids`, queue-based events
  - [ingestion/twitter_provider.py](src/poly_meridian/ingestion/twitter_provider.py) + [onchain_provider.py](src/poly_meridian/ingestion/onchain_provider.py) — stubs (NotImplementedError until Phase 3)
  - [ingestion/normalize.py](src/poly_meridian/ingestion/normalize.py) — `gamma_market_to_domain/_to_row` (tolerates both `clobTokenIds` formats), `book_snapshot_to_domain` (sorts bids desc / asks asc, filters zero-size)
- **Feature pipeline**
  - [features/orderbook_features.py](src/poly_meridian/features/orderbook_features.py) — mid, spread, microprice, depth imbalance, bid/ask depth within 5%
  - [features/time_features.py](src/poly_meridian/features/time_features.py) — time_to_resolution_hours, log_decay, decay_factor (168h horizon default)
  - [features/registry.py](src/poly_meridian/features/registry.py) — central `CATALOG` + `compute_features()` orchestrator, dependency-aware (skips features without their inputs)
  - sentiment/smart_money/TA features = empty stubs (Phase 3/4)
- **Main loop** ([main.py](src/poly_meridian/main.py))
  - boots logging + prometheus + `/health`
  - eagerly connects DB+Redis (graceful degrade if missing)
  - gamma sync loop (5min interval) → `upsert_markets`
  - GDELT news loop → `insert_news_article`
  - clean shutdown on SIGINT/SIGTERM
  - Prometheus metrics: `pm_markets_total`, `pm_news_ingested_total`
- **Backfill** ([scripts/backfill_history.py](scripts/backfill_history.py)) — one-shot Gamma → DB sync
- **Tests** (under `tests/unit/`)
  - `test_local_book.py` — snapshot apply, zero-size removal, snapshot replaces state, depth cutoffs, empty book → None
  - `test_orderbook_features.py` — mid/spread/microprice/depth-imbalance arithmetic on known inputs
  - `test_time_features.py` — past/future, log domain, decay clamping
  - `test_normalize.py` — direct token IDs, clobTokenIds JSON string, missing fields, book sort order
  - `test_gamma_client.py` — httpx MockTransport, pagination, data-envelope handling
  - `test_feature_registry.py` — catalog/computer key parity, end-to-end compute

### Phase 1 acceptance gate
| Check                                           | Status | Notes                                                                  |
|-------------------------------------------------|--------|------------------------------------------------------------------------|
| All Phase 0 ABCs still abstract                 | ✅     | unchanged                                                              |
| Gamma client paginates active markets           | ⏳     | unit-tested with mock; live hit pending user run                       |
| WS consumer reconstructs book from snapshot+diff| ⏳     | unit-tested on LocalBook; live WS pending user run                     |
| `make test` passes                              | ⏳     | 6 new test files, all pure compute or mocked — should pass locally     |
| `make up` brings agent up, healthcheck green    | ⏳     | needs Docker + a TimescaleDB image pull on first run                   |
| Markets table fills after first sync loop       | ⏳     | run `make up && docker compose logs -f agent` to see `gamma_sync.done` |
| No secrets / private keys leaked in code        | ✅     | grep clean; `.env.example` only                                        |

### Deliberate deferrals (still Phase 1-eligible work, pushed for safety)
- **CLOB authed client** — concrete `py-clob-client` wiring deferred to Phase 2. The library has two import paths in the wild (`py_clob_client` vs `py_clob_client_v2`) and the constructor signature has changed across releases. Phase 2 will pin the exact version after we install in the user's env.
- **Twitter + on-chain providers** — stubs only. Spec §24 lists smart-money tracker as Phase 1, but the real implementation belongs with the smart-money strategy in Phase 3 (no point ingesting until something consumes).
- **Alembic migrations** — `scripts/bootstrap_db.sh` is the canonical schema today; Alembic can come when we need real migrations.

## Pending design notes (not yet in MASTER_SPEC)
- **Operator UI (Bloomberg Terminal style).** User flagged 2026-05-23: dashboard must be a dense, dark, multi-panel, keyboard-driven trading terminal — trading + portfolio focus, NOT a prediction-market betting UI. Grafana (§20) covers observability only; this is a separate operator surface to be designed in a `docs/strategy_specs/ui_terminal.md` spec around Phase 5. Stack TBD (Tauri+React / Textual TUI / Next.js).

## Open questions for user
1. **Railway TimescaleDB:** (a) Railway TimescaleDB template, (b) Timescale Cloud (free tier 6mo), (c) self-hosted Postgres on Railway + install timescaledb extension manually. Recommend (b) for prod. Decide before Phase 6.
2. **Observability on Railway:** docker-compose runs Prometheus+Grafana locally. On Railway, recommend Grafana Cloud free tier (push metrics) rather than hosting both as Railway services. Decide before Phase 5.
3. **CLOB client library:** `py-clob-client` vs `py-clob-client-v2`. Will pin in Phase 2 once `pip index` is verifiable in user's env.
4. **GitHub push:** still pending — need git identity (name + GitHub email or noreply email) to commit + push. Once provided, all Phase 0+1 work goes up in one initial commit.

## How to verify Phase 1 locally
```bash
cp .env.example .env
make install            # uv pip install -e ".[dev,polymarket]"
make test               # all unit tests should pass (no network, no DB)
make up                 # docker compose up -d → DB hypertables created, agent boots
docker compose logs -f agent
# Expect inside the first 5min:
#   agent.boot mode=paper version=0.1.0
#   agent.ready health_port=8000 db_ok=true cache_ok=true
#   gamma_sync.done n=<some number, typically 1000-3000>
curl localhost:8000/health
# {"status":"ok"}
```

## Next: Phase 2 — Arbitrage strategy + risk engine + paper executor
- `ArbitrageStrategy` (single-market, complete-set, cross-market within event)
- `RiskPolicy` concrete impl: Quarter Kelly + hard caps + kill-switch
- `PaperExecutor`: simulate fills against `LocalBook`, write to `our_orders` with `mode='paper'`
- `Portfolio` skeleton: ledger, MTM, NAV
- First Grafana dashboard
- 1 week paper run on real market data
