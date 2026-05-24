# Railway deployment — Poly Meridian

End-to-end checklist for shipping the agent + dashboard to Railway from a
clean account. Each step matches a Railway concept; if you get stuck, the
section heading should make the failure mode obvious.

> **Status:** verified locally on `1310b38` — agent autonomous loop runs,
> SSE pushes snapshot every 5s, web dashboard receives live updates.

---

## Project topology on Railway

You'll create **one Railway project** containing **3 services**:

```
Project: poly-meridian
├── 🐍 agent       (this repo, Dockerfile)        ← serves /api/* + /health
├── 🟦 web         (this repo, root=web/)         ← Next.js dashboard
└── 🐘 db          (Railway plugin: Postgres)     ← markets, orderbooks, signals
```

Optional (add when ready):
- `redis` plugin (caching) — agent will skip cache cleanly if absent
- `prometheus` + `grafana` services (or use Grafana Cloud free tier)

---

## Step 1 — Create the project & connect GitHub

1. Railway → **New Project** → **Deploy from GitHub repo**
2. Pick `FrhnYldzl/Poly-Meridian`
3. Railway detects [`railway.json`](../railway.json) and proposes Dockerfile build. Confirm.
4. Service name: `agent`. Branch: `main` (default).

## Step 2 — Add Postgres

1. In the project → **+ New** → **Database** → **Add PostgreSQL**
2. Railway auto-provisions, exposes `DATABASE_URL` to the project.
3. **Important:** the agent expects `POSTGRES_URL` in `asyncpg` format. In
   the `agent` service variables tab:
   ```
   POSTGRES_URL=${{Postgres.DATABASE_URL.replace('postgresql://','postgresql+asyncpg://')}}
   ```
   Or hardcode the URL Railway shows you, prefixed with `postgresql+asyncpg://`.

> **Note on TimescaleDB extension:** Railway's stock Postgres image doesn't
> include TimescaleDB. Options, in order of preference:
> - **Option A (recommended for now):** run `scripts/bootstrap_db.sh` against
>   the DB with the `timescaledb` lines deleted — hypertables become plain
>   tables. Acceptable for ≤ 30M rows.
> - **Option B:** swap to a Timescale Cloud free-tier instance and use its
>   URL instead.
> - **Option C:** self-host TimescaleDB on Railway via the
>   `timescale/timescaledb-ha:pg16` Docker image as a private service.

Apply schema:
```bash
# From your machine, with the agent service deployed once:
railway run --service agent bash -c "psql \"$POSTGRES_URL\" < scripts/bootstrap_db.sh"
```

## Step 3 — (Optional) Add Redis

1. **+ New** → **Database** → **Add Redis**
2. In agent service variables:
   ```
   REDIS_URL=${{Redis.REDIS_URL}}
   ```

The agent caches hot order books and last-mark prices here; without it, the
pipeline runs slightly slower but never fails.

## Step 4 — Configure agent env vars

Open the `agent` service → **Variables** tab → paste all relevant entries
from [`.env.example`](../.env.example). Critical ones:

| Variable                  | What it does                            | Required for                |
|---------------------------|-----------------------------------------|-----------------------------|
| `MODE`                    | `paper` / `live-conservative` / `live-normal` | Always — default `paper` |
| `POLYMARKET_PRIVATE_KEY`  | Wallet that signs orders                | Live mode only              |
| `POLYMARKET_API_KEY/SECRET/PASSPHRASE` | L2 creds (auto-derived) | Live mode (cached after first boot) |
| `POLYGON_RPC_URL` or `ALCHEMY_API_KEY` | On-chain tracker            | Smart Money strategy        |
| `OPENAI_API_KEY`          | Embeddings for news semantic match      | Sentiment strategy          |
| `ANTHROPIC_API_KEY`       | Sentiment scoring via Claude            | Sentiment strategy (falls back to keyword heuristic if missing) |
| `X_BEARER_TOKEN`          | Twitter filtered stream                 | Twitter sentiment           |
| `SLACK_WEBHOOK_URL` or `TELEGRAM_*` | Alerts                        | Required to pass §19 gate before going live |
| `LOG_LEVEL`               | `INFO` / `DEBUG`                        | Optional, defaults INFO     |

**Do NOT set `PORT` manually** — Railway sets it automatically and the agent reads it via `settings.prometheus_port` (which has `validation_alias=("PORT", "PROMETHEUS_PORT")`).

## Step 5 — Deploy the agent

Railway should auto-deploy from `main` on every push. Confirm:
- Build succeeds (look for `uv pip install` lines in build log)
- Container starts and `agent.boot mode=paper version=0.1.0` appears in deploy logs
- Healthcheck passes (`GET /health` → 200)
- Logs show `gamma.iter_active_markets count=…` within 1-2 minutes
- Logs show `pipeline.books_bootstrapped n=…` confirming WS + REST bootstrap

Grab the public URL Railway assigns. Example: `https://poly-meridian.up.railway.app`.

## Step 6 — Deploy the web dashboard

1. In the same project → **+ New** → **Empty Service** named `web`
2. Settings → **Source** → connect to the same `FrhnYldzl/Poly-Meridian` repo
3. Settings → **Root Directory:** set to `web` (so Nixpacks builds Next.js)
4. Settings → **Watch Paths:** `web/**` (so it only rebuilds on UI changes)
5. Variables tab:
   ```
   NEXT_PUBLIC_API_URL=https://<your-agent-service>.up.railway.app
   ```
   (replace with the actual URL from Step 5)
6. Optional but recommended:
   ```
   AGENT_API_URL=https://<your-agent-service>.up.railway.app
   ```
   (used by the Next.js server-side rewrite, redundant safety)
7. Deploy. Open the URL Railway assigns — you should see the dashboard with
   live NAV, ticks counter climbing, markets watched > 0.

## Step 7 — Verify autonomous trading

The agent should now be:
- ✅ Polling Gamma every 5 min
- ✅ Subscribed to ~80 active-market tokens via WS
- ✅ Bootstrapping book snapshots via REST after subscribe
- ✅ Running 5 strategies × 40 markets × every 5s pipeline tick
- ✅ Risk-gating every signal before submission
- ✅ Writing paper orders to `our_orders` with `mode='paper'`

You'll see signal activity when:
- An arbitrage opportunity opens (rare on tight markets)
- News article → Claude sentiment hits a market with high impact
- ≥3 Tier 1 wallets cluster the same direction (requires populated `smart_wallets`)

## Step 8 — Promote to live (when paper passes §19 gate)

After ≥30 days of stable paper trading with positive Sharpe:

```bash
# Mark each drill complete:
railway run --service agent python -m poly_meridian.cli mark-drill kill_switch
railway run --service agent python -m poly_meridian.cli mark-drill reconnect
railway run --service agent python -m poly_meridian.cli mark-drill secrets
railway run --service agent python -m poly_meridian.cli mark-drill backup
railway run --service agent python -m poly_meridian.cli mark-drill legal

# Run the gate:
railway run --service agent python -m poly_meridian.cli promote-to-live --proposed-live-usd 500
```

If PASS:
1. Set `MODE=live-conservative` in the agent service variables
2. Set `POLYMARKET_PRIVATE_KEY` to a wallet with **exactly $500** in USDC
3. Redeploy
4. Watch the first 24 hours **very closely** — Grafana dashboards, alert
   channel, and the `risk.reject` log lines

---

## Troubleshooting

- **`db.boot_skip` warning on every boot:** `POSTGRES_URL` env var missing or
  not in `postgresql+asyncpg://` format. Fix the value, redeploy.

- **`clob.no_private_key` warning:** Expected in paper mode — agent doesn't
  need to sign orders. Becomes blocking error when you flip to live mode.

- **No signals after 1 hour of paper:** Normal. Arbitrage opportunities are
  rare on active markets. Check `pipeline_ticks_total` is climbing (it should
  hit thousands per hour) — that's proof the autonomous loop is running.

- **Web shows "RECONNECTING":** SSE connection dropped. Check the agent
  service is still healthy. Refresh the browser. If persistent, the agent's
  public URL changed — update `NEXT_PUBLIC_API_URL` and redeploy `web`.

- **`uv pip install` fails on Railway:** the base image needs `build-essential`
  (it has it in our Dockerfile) and `git`. If you customized the Dockerfile,
  re-add `apt-get install -y --no-install-recommends build-essential curl ca-certificates`.

---

## Cost estimate (Railway, Mar 2026 pricing)

| Service        | RAM   | CPU   | Monthly est. |
|----------------|-------|-------|-------------:|
| agent          | 512MB | 0.5   | ~$5          |
| web            | 256MB | 0.25  | ~$2          |
| Postgres      | 512MB | 0.5   | ~$5          |
| Redis (opt)   | 256MB | 0.25  | ~$2          |
| **Total**     |       |       | **~$14/mo**  |

If you're using Hobby plan ($5/mo with $5 of free usage), this will land
around the floor of the paid tier.
