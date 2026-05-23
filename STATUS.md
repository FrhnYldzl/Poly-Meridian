# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 3 — Sentiment + SmartMoney + WS ✅ (skeleton complete, awaiting live verification)

### Done
- **Cloud-friendly stack switch:** dropped torch/sentence-transformers from the default path. Embeddings via OpenAI `text-embedding-3-small` (1536-d, fast, cheap). Sentiment scoring via Anthropic Claude Haiku 4.5 (also cheap, fast, structured JSON). Pure Python deps — Railway-friendly.
  - DB schema bumped: `VECTOR(768)` → `VECTOR(1536)`; added HNSW index on `news_articles.embedding` for cosine; new `market_embeddings` table with HNSW index.
  - New `[llm]` optional dep group in `pyproject.toml`.
- **Sentiment subsystem** ([`src/poly_meridian/sentiment/`](src/poly_meridian/sentiment))
  - [`embeddings.py`](src/poly_meridian/sentiment/embeddings.py) — `EmbeddingsBackend` ABC + `OpenAIEmbeddings` (1536-d default) + `StubEmbeddings` for tests.
  - [`scorer.py`](src/poly_meridian/sentiment/scorer.py) — `SentimentScorer` ABC + `ClaudeSentimentScorer` (auto-falls-back to heuristic on API error) + `HeuristicSentimentScorer` (keyword-based, no network) + `score_many()` bounded-concurrency helper + robust JSON parser handling code fences / preamble.
  - [`news_processor.py`](src/poly_meridian/sentiment/news_processor.py) — orchestrates: pull unprocessed articles → batch embed → write embeddings → top-K market matching via pgvector cosine → score each (article, market) → write `news_signals` → mark processed. Lazy `embed_markets_if_stale()` keeps the market vector cache fresh.
- **Storage** — [`writers.py`](src/poly_meridian/storage/writers.py) extended:
  - `set_news_embedding`, `mark_article_processed`, `fetch_unprocessed_articles`
  - `upsert_market_embedding`, `market_needs_embedding`
  - `find_top_k_markets_for_article` (pgvector cosine via `<=>`)
  - `insert_news_signal`, `fetch_recent_news_signals`
- **Features** — [`features/sentiment_features.py`](src/poly_meridian/features/sentiment_features.py) `aggregate_signals()` returns `SentimentAggregate` with impact-weighted avg sentiment + per-direction impact sum + winning direction.
- **Strategies** ([`strategies/`](src/poly_meridian/strategies))
  - [`sentiment.py`](src/poly_meridian/strategies/sentiment.py) — `SentimentStrategy`: filters by `impact_max >= threshold`, computes our_p via market_price + impact-weighted sentiment shift, emits BUY_YES/BUY_NO with conviction = `impact * |sentiment|`. Exposes `proposed_price_from_signal` + `proposed_size_pct` for aggregator.
  - [`smart_money.py`](src/poly_meridian/strategies/smart_money.py) — `SmartMoneyStrategy`: pure-compute predicate on `ClusterState` (wallet flows per direction). Triggers when ≥`min_wallet_count` distinct wallets net-bought same side with ≥`min_net_usd_per_wallet` each. Stale clusters (>`freshness_max_sec`) rejected. Conviction scales smoothly with cluster size.
  - [`aggregator.py`](src/poly_meridian/strategies/aggregator.py) — multi-strategy with per-strategy `_PRICE_HELPERS` + `_SIZE_HELPERS` registry. Conviction-weighted voting, conflict threshold, direction-token mapping for token_id propagation.
- **On-chain** ([`ingestion/onchain_provider.py`](src/poly_meridian/ingestion/onchain_provider.py))
  - `PolygonOnchainSource(IngestionSource)` — JSON-RPC client, polls seeded smart wallets every 60s, fetches Polymarket CTF `TransferSingle` event logs filtered by wallet topic (from or to). Falls back gracefully when no RPC URL or empty wallet list. Per-wallet block cursors so we only re-fetch deltas.
  - [`config/smart_wallets.yaml`](config/smart_wallets.yaml) — seed list (placeholders today; populate with leaderboard scrape before Phase 6 live).
- **Pipeline** ([`pipeline.py`](src/poly_meridian/pipeline.py)) — now wires three strategies. Each is gated on `enabled`. Failure in one strategy doesn't kill the tick (try/except per strategy). Aggregator collects signals across all three.
- **Main loop** ([`main.py`](src/poly_meridian/main.py))
  - **WS pipeline replaces REST polling.** Subscribes via `ClobWebsocketSource` to up to 40 active-market tokens. Re-subscribes whenever the active set changes (every gamma sync). Local books are owned by WS source and shared with strategies via `attach_book()`.
  - News processor task: every 180s, embeds + scores up to 25 unprocessed articles → writes signals.
  - Sentiment cache hydration: every pipeline cycle, pulls last `sentiment_window_sec` of news_signals per sampled market and pushes to `SentimentStrategy`.
  - Auto-degradation: if `OPENAI_API_KEY` missing → no embeddings, sentiment effectively disabled. If `ANTHROPIC_API_KEY` missing but OpenAI present → Claude scorer falls back to heuristic.
  - New gauge: `pm_ws_books_tracked`.
- **Configs**
  - [`base.yaml`](config/base.yaml): enabled strategies = [arbitrage, sentiment, smart_money]
  - [`strategies/sentiment.yaml`](config/strategies/sentiment.yaml): `enabled: true`, threshold 0.6
  - [`strategies/smart_money.yaml`](config/strategies/smart_money.yaml): `enabled: true`, cluster min 3 wallets
- **Tests** (5 new files, all pure compute / asyncio, no network)
  - `test_sentiment_scorer.py` — heuristic positive/negative/neutral; JSON parser handles fence/preamble/garbage; result clipping
  - `test_sentiment_features.py` — aggregation weighted by impact, direction scoring, edge cases
  - `test_sentiment_strategy.py` — disabled / below threshold / strong YES / strong NO / NEUTRAL guards
  - `test_smart_money_strategy.py` — disabled / below cluster size / qualifying YES cluster / stale / below min_net_usd
  - `test_multi_aggregator.py` — three strategies same direction, strong-one beats two-weak, tied-strong conflict, size capping
  - `test_embeddings_stub.py` — StubEmbeddings deterministic + text_hash

### Phase 3 acceptance gate
| Check | Status | Notes |
|---|---|---|
| All ABCs still abstract | ✅ | Phase 0 smoke test still passes |
| Risk gate enforced on every order | ✅ | `pipeline.tick` is the only path; no bypass introduced |
| Sentiment subsystem isolated, falls back to heuristic | ✅ | `scorer.py::ClaudeSentimentScorer` catches everything |
| Multi-strategy conflict actually exercised | ✅ | `test_multi_aggregator.py::test_tied_strong_signals_yield_conflict` |
| pgvector cosine search returns top-K | ⏳ | Needs live DB + `vector` extension to validate |
| WS pipeline maintains local books across reconnects | ⏳ | Reconnect logic from Phase 1 reused; live verification pending |
| `make test` passes (30+ test files) | ⏳ | All Phase 3 tests are pure compute / asyncio |
| Smart wallet seed list is populated | ⏳ | Placeholder today — fill before Phase 6 live |

### Deliberate deferrals
- **Cluster state builder:** on-chain events flow into the queue (`PolygonOnchainSource.events()`), but a builder task that converts CTF transfers → `WalletFlow` → per-condition `ClusterState` isn't wired into main.py yet. Phase 3 ships the strategy + data model; the builder task lands when we have real seeded smart wallets to test against. Until then, SmartMoneyStrategy returns None (no cluster state attached).
- **Twitter provider** — kept as stub. Real impl belongs with Phase 4 backtest fidelity (multi-source sentiment correlation).
- **WS for ALL markets:** Phase 3 subscribes to top 40 active markets. Full firehose comes in Phase 4 when we have backtest infra to validate.
- **Token-to-category persistence:** in-memory map only; survives single agent process. DB-backed map is a Phase 4 nice-to-have.

## Pending design notes (not yet in MASTER_SPEC)
- **Operator UI (Bloomberg Terminal style)** — user flagged 2026-05-23; dense dark multi-panel keyboard-driven terminal. Grafana covers observability only. Separate operator surface designed in a `docs/strategy_specs/ui_terminal.md` spec around Phase 5. Stack TBD (Tauri+React / Textual TUI / Next.js).

## Open questions for user
1. **Smart wallet seeds:** I left [`config/smart_wallets.yaml`](config/smart_wallets.yaml) with placeholder slots. To make `SmartMoneyStrategy` actually fire, populate with real addresses from polymarket.com/leaderboard. Want me to write a scraper script as part of Phase 4?
2. **OpenAI vs Claude embeddings:** If Anthropic ships embeddings before Phase 6, we can switch (one-line change in `OpenAIEmbeddings`). Current choice is pragmatic, not religious.
3. **Railway TimescaleDB still pending** (3 options from STATUS history).
4. **GitHub push for Phase 3:** as before — should I push now or wait for your local verification?

## How to verify Phase 3 locally
```bash
cp .env.example .env             # populate OPENAI_API_KEY + ANTHROPIC_API_KEY + ALCHEMY_API_KEY
make install                     # uv pip install -e ".[dev,polymarket,llm]"
make test                        # ~5 new test files + everything from Phase 0-2 (33 total)
make up                          # docker compose up → DB + agent + Prometheus + Grafana
docker compose logs -f agent
# Expect inside 5-10 min:
#   agent.boot mode=paper
#   agent.ready sentiment_enabled=true db_ok=true
#   gamma_sync.done n=~1500-3000
#   news.market_emb.done n=~1500   (one-time, then idle until questions change)
#   clob_ws.subscribed n=80        (40 markets x 2 tokens each)
#   news.processed n=25            (every 3 min if articles flowing)
#   pipeline.order              (when an arb/sentiment opportunity passes risk)
open http://localhost:3000       # Grafana Overview dashboard
```

## Next: Phase 4 — Backtest engine + StatQuantStrategy
- Historical replay engine over `orderbook_snapshots` + `trades` + `news_signals`
- Walk-forward validation
- `StatQuantStrategy` (mean reversion / momentum / vol breakout / time-decay arb)
- 90-day backtest reports for every strategy
- Re-fit slippage model from realized fills (Phase 2 left a=50, b=1.2 as guesses)
- Per-category fee schedule (§2.2) wired through PaperExecutor for realistic paper P&L
- Optional: Twitter provider real impl
