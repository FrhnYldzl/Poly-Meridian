# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 6 — Live executor + promotion gate ✅ (code complete, awaiting paper observation)

### Done

**CLOB authed client** ([`ingestion/clob_client.py`](src/poly_meridian/ingestion/clob_client.py))
- `_try_import_clob()` resolves `py_clob_client_v2` first, falls back to `py_clob_client`.
- `init_authed()` brings up the authed client: tries L2 first, falls back to L1-derive on the fly with a warning to save derived creds to `.env`.
- `_build_creds()` accommodates multiple shapes — library-version-tolerant.

**LiveExecutor** ([`execution/live_executor.py`](src/poly_meridian/execution/live_executor.py))
- `Executor(ABC)` impl with `mode = settings.mode` (live-conservative or live-normal).
- **Hard safety:** constructor raises in paper mode.
- `submit()` → posts via `py-clob-client`'s `create_order` + `post_order` (or market variant for FOK/FAK), wraps sync calls in `loop.run_in_executor`.
- `_invoke()` tries multiple library method names for cross-version compatibility.
- `cancel()` calls venue `cancel`/`cancel_order` on the venue-side ID.
- `reconcile()` fallback path: if a tracked order is no longer in `get_orders()` → mark FILLED (real fills come via user-channel WS).
- Fee schedule integration (same as PaperExecutor).
- Graceful submit-error handling: any exception → `Order.status = REJECTED` + structured log, agent stays up.

**User-channel WS** ([`ingestion/clob_user_ws.py`](src/poly_meridian/ingestion/clob_user_ws.py))
- `ClobUserChannel(IngestionSource)` streams `wss://ws-subscriptions-clob.polymarket.com/ws/user`.
- HMAC auth (api_key + secret + passphrase).
- Dispatches `order`/`trade` events to caller callbacks → LiveExecutor wires these to its order book + ledger.
- Exponential backoff reconnect (1/2/5/10/30s + jitter).
- Graceful disable when API creds missing.

**Promotion gate** ([`promotion.py`](src/poly_meridian/promotion.py))
- **Real DB-backed checks (no more stubs):**
  - `check_paper_history_age` — oldest `our_orders` row with mode='paper' ≥ N days
  - `check_paper_metrics` — Sharpe + Max DD computed from `pnl_daily` series via `backtest.metrics.compute_all()`
  - `check_initial_cap_ratio` — proposed live capital ≤ 5% of latest paper NAV
  - `check_alerting` — Slack/Telegram webhook configured
  - `check_drill(name)` — file-based confirmation (operator runs `poly-meridian mark-drill <name>`)
- `mark_drill()` + `.promotion_flags/` directory (gitignored)
- `run_gate()` runs all checks in one shot, returns `PromotionReport(passed, checks, render)`
- **Fail-closed:** any unknown/erroring check → `passed=False`

**CLI** ([`cli.py`](src/poly_meridian/cli.py))
- `poly-meridian promote-to-live --proposed-live-usd 500 --min-paper-days 30` — runs the gate, exits 0/1
- `poly-meridian mark-drill <name>` — flips a drill flag (kill_switch / reconnect / secrets / backup / legal)
- Also: `run`, `status`, `backtest`, `walkforward`

**Mode-aware executor selection** ([`main.py`](src/poly_meridian/main.py))
- `_build_executor()` is the ONE place that branches paper vs live.
- Starting NAV: $100K virtual in paper, $500 conservative in live.
- All other wiring (strategies, risk, aggregator, pipeline) is mode-agnostic.

**Scripts** ([`scripts/promote_to_live.py`](scripts/promote_to_live.py)) replaced — wraps real `promotion.run_gate()` with typer CLI.

### Tests (2 new, 43 total)
- `test_live_executor.py` — refuses paper mode; limit + market order paths; cancel; reconcile FILLED detection; submit-error → REJECTED (mocked CLOB throughout)
- `test_promotion.py` — drill mark/check, paper history age (zero / sufficient), initial cap ratio (pass/fail), alerting (env-toggled), report render PASS/FAIL

### Phase 6 acceptance gate
| Check | Status | Notes |
|---|---|---|
| LiveExecutor cannot start in paper mode | ✅ | Constructor raises; `test_live_executor_refuses_in_paper_mode` enforces |
| Risk gate still enforced on live mode | ✅ | Pipeline unchanged; `RiskPolicy.evaluate` before `executor.submit` |
| Promotion gate fails closed | ✅ | Any check error → `passed=False` |
| Promotion gate checks are real, not stubs | ✅ | DB-backed paper_history_age + paper_metrics; alerting reads env; drills are flag files |
| Mode-aware executor wired | ✅ | `_build_executor()` is the single branch point |
| `make test` passes (43 test files) | ⏳ | All Phase 6 tests pure compute / async with mocks |
| py-clob-client method names verified | ⚠️ | We try multiple method names per library version; operator should run `poly-meridian status` after `uv pip install -e ".[polymarket]"` to confirm authed init succeeds |

### Operator next steps (THE actual go-live path)

1. **Run paper observation 30+ days.**
   - `make up` with `MODE=paper`
   - Let it run continuously. Confirm `pm_signal_emitted_total`, `pm_order_submitted_total{mode="paper"}`, `pm_news_processed_total` grow on Grafana.
2. **Run drills, mark each as done:**
   ```bash
   docker compose run --rm agent python -m poly_meridian.cli mark-drill kill_switch
   docker compose run --rm agent python -m poly_meridian.cli mark-drill reconnect
   docker compose run --rm agent python -m poly_meridian.cli mark-drill secrets
   docker compose run --rm agent python -m poly_meridian.cli mark-drill backup
   docker compose run --rm agent python -m poly_meridian.cli mark-drill legal
   ```
3. **Configure alerting:** set `SLACK_WEBHOOK_URL` (or `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID`) in Railway env vars.
4. **Run the gate when ready:**
   ```bash
   docker compose run --rm agent python -m poly_meridian.cli promote-to-live \
       --proposed-live-usd 500 --min-paper-days 30
   ```
5. **If PASS:** flip `MODE=live-conservative` in Railway env, redeploy. Watch first 24h closely.
6. **Scale-up plan** (§24 Phase 6): $500 → $5K (after 1 month positive) → $25K (after 6 months).

### Backend complete — what's left

| Phase | Durum |
|---|---|
| 0-5 | ✅ |
| **6 Kademeli canlı** | ✅ **code-side done** — paper run → gate → live is now operator-driven |
| **UI/UX (Bloomberg-style)** | 🔄 Sıradaki — proper visual interface |

## Open questions for user
1. **py-clob-client install + verification** — `uv pip install -e ".[polymarket]"` includes it; run agent locally in paper mode and verify `clob.authed.init_ok` log line appears after setting `POLYMARKET_PRIVATE_KEY`.
2. **Smart wallet seeds + leaderboard endpoint** — still pending from Phase 5a operator action items.
3. **Paid data feeds for Fundamentals** — decide which categories you want live (Politics polls / Sports Elo / Crypto funding / Macro calendar).
4. **UI scope** — when we get there: Bloomberg-style operator terminal. Stack candidates: Tauri+React / Textual TUI / Next.js. Wanted feature list?
