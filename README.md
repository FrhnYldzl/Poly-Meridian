# Poly Meridian

Polymarket quant trading agent — multi-strategy hybrid, paper-first.

Canonical spec: [docs/MASTER_SPEC.md](docs/MASTER_SPEC.md). Every architectural decision references a section number from that file (e.g. "§14.2").

---

## Status

**Phase 0 — Repo scaffold.** ABC contracts, DB schema, Docker setup, config skeletons.
See [STATUS.md](STATUS.md) for the live progress board.

## Stack

Python 3.12 · uv · pydantic v2 · asyncio · structlog · pytest · ruff + black + mypy --strict
PostgreSQL 16 + TimescaleDB + pgvector · Redis · Prometheus + Grafana

## Operating modes

| Mode                | Behavior                                                          |
|---------------------|-------------------------------------------------------------------|
| `paper` (default)   | No real orders. Fills simulated. Same dashboards as live.         |
| `live-conservative` | Real orders. Hard per-trade and per-day caps.                     |
| `live-normal`       | Full capital.                                                     |
| `kill`              | Stop new orders, hold or liquidate per policy.                    |

**Live mode is only entered via `scripts/promote_to_live.py`** (Master Spec §19 checklist).

## Local development

```bash
cp .env.example .env          # fill secrets
make install                  # uv pip install -e ".[dev,polymarket]"
make up                       # docker compose up -d
make test                     # pytest
make check                    # ruff + mypy + pytest
```

Services exposed locally:

- Postgres+TimescaleDB → `localhost:5432`
- Redis → `localhost:6379`
- Prometheus → `localhost:9090`
- Grafana → `localhost:3000` (admin / `$GRAFANA_PASSWORD`)

## Deployment — Railway

Production runs on Railway. The repo ships a `Dockerfile` + `railway.json` so Railway can build and run the agent service directly. For supporting services, Railway plugins / templates are used:

| Service          | Local (docker-compose)     | Railway                                                      |
|------------------|----------------------------|--------------------------------------------------------------|
| Postgres         | `timescale/timescaledb-ha` | Railway TimescaleDB template, or Timescale Cloud, or self-hosted Railway service |
| Redis            | `redis:7-alpine`           | Railway Redis plugin                                         |
| Prometheus       | local container            | Grafana Cloud free tier (push metrics) — preferred over Railway-hosted Prom |
| Grafana          | local container            | Grafana Cloud                                                |
| Agent            | `agent` service            | This repo, deployed via Railway from GitHub                  |

Environment variables: every var in [.env.example](.env.example) must be set on the Railway service. Secrets are stored in Railway's variable store — never commit `.env`.

## Repo layout

See Master Spec §10 for the full file inventory. High-level:

```
src/poly_meridian/
  ingestion/       # Gamma, CLOB, WS, news, twitter, on-chain
  storage/         # asyncpg pool, ORM models, migrations, redis cache
  features/        # orderbook, time, sentiment, smart-money, TA features
  strategies/      # 5 sub-strategies + aggregator
  risk/            # Kelly + limits + kill-switch + policy
  execution/       # router, paper executor, live executor, slippage
  portfolio/       # ledger, MTM, P&L, rebalancer
  backtest/        # replay, walk-forward, metrics, reports
  observability/   # structlog, prometheus, alerts
```

## Immutable rules (do not violate)

1. `MODE=paper` is the default. Live only via `scripts/promote_to_live.py` checklist.
2. Secrets never committed. Only `.env.example` is in git.
3. Every order passes through the risk engine. No bypass code.
4. New module: ABC/Protocol first, then implementation.
5. Every PR: `make check` (ruff + black + mypy + pytest) must pass.
