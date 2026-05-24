"""Idempotent schema bootstrap. Runs on every agent boot.

Equivalent to `scripts/bootstrap_db.sh` but Python-side so the agent can
self-provision its DB on first deploy without an operator running psql.

Graceful with optional features:
- TimescaleDB extension is optional. If missing, hypertables become plain
  tables (works fine for <10M rows, the only thing we lose is automatic
  retention/compression).
- pgvector is optional. If missing, embedding column is plain bytea / text;
  semantic search falls back to keyword matching.
- pgcrypto is optional (used only for UUID generation in some indexes).

Schema mirrors MASTER_SPEC §12 + v1.1 §14.3 smart_wallets columns.
"""
from __future__ import annotations

import structlog

from poly_meridian.storage.db import Database

log = structlog.get_logger("poly_meridian.schema")


_EXTENSIONS = [
    "timescaledb",
    "vector",
    "pgcrypto",
]


_BASE_TABLES = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id    TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    category        TEXT,
    sub_category    TEXT,
    event_id        TEXT,
    yes_token_id    TEXT NOT NULL,
    no_token_id     TEXT NOT NULL,
    end_date_iso    TIMESTAMPTZ,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    closed          BOOLEAN NOT NULL DEFAULT FALSE,
    liquidity_num   NUMERIC,
    volume_num      NUMERIC,
    raw             JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, closed, end_date_iso);
CREATE INDEX IF NOT EXISTS idx_markets_event  ON markets(event_id);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    best_bid        NUMERIC,
    best_ask        NUMERIC,
    mid             NUMERIC,
    microprice      NUMERIC,
    bid_depth_5pct  NUMERIC,
    ask_depth_5pct  NUMERIC,
    raw_levels      JSONB
);
CREATE INDEX IF NOT EXISTS idx_obs_token_ts ON orderbook_snapshots(token_id, ts DESC);

CREATE TABLE IF NOT EXISTS trades (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price           NUMERIC NOT NULL,
    size            NUMERIC NOT NULL,
    maker_address   TEXT,
    taker_address   TEXT,
    tx_hash         TEXT,
    is_ours         BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_addr  ON trades(maker_address, taker_address);

CREATE TABLE IF NOT EXISTS news_articles (
    article_id      TEXT PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    source          TEXT,
    title           TEXT,
    body            TEXT,
    url             TEXT,
    processed       BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_articles(ts DESC);

CREATE TABLE IF NOT EXISTS news_signals (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    article_id      TEXT NOT NULL REFERENCES news_articles(article_id),
    condition_id    TEXT NOT NULL REFERENCES markets(condition_id),
    sentiment       NUMERIC NOT NULL,
    impact          NUMERIC NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('YES','NO','NEUTRAL'))
);
CREATE INDEX IF NOT EXISTS idx_news_sig_cond_ts ON news_signals(condition_id, ts DESC);

CREATE TABLE IF NOT EXISTS smart_wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    lifetime_pnl    NUMERIC,
    win_rate        NUMERIC,
    trade_count     INT,
    last_updated    TIMESTAMPTZ,
    tier            INT NOT NULL DEFAULT 3 CHECK (tier IN (1, 2, 3)),
    category_focus  TEXT,
    last_7d_pnl     NUMERIC,
    recency_score   NUMERIC NOT NULL DEFAULT 0,
    hedge_flag      BOOLEAN NOT NULL DEFAULT FALSE,
    drawdown_7d_pct NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_smart_wallets_tier ON smart_wallets(tier, last_updated DESC);
CREATE INDEX IF NOT EXISTS idx_smart_wallets_category ON smart_wallets(category_focus);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    features        JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feat_token_ts ON feature_snapshots(token_id, ts DESC);

CREATE TABLE IF NOT EXISTS strategy_signals (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    edge            NUMERIC NOT NULL,
    conviction      NUMERIC NOT NULL,
    suggested_action TEXT NOT NULL CHECK (suggested_action IN ('BUY_YES','BUY_NO','SELL','HOLD','EXIT')),
    rationale       JSONB
);
CREATE INDEX IF NOT EXISTS idx_strat_sig_ts ON strategy_signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_strat_sig_cond ON strategy_signals(condition_id, ts DESC);

CREATE TABLE IF NOT EXISTS our_orders (
    order_id        TEXT PRIMARY KEY,
    ts_created      TIMESTAMPTZ NOT NULL,
    ts_filled       TIMESTAMPTZ,
    strategy        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type      TEXT NOT NULL CHECK (order_type IN ('GTC','GTD','FOK','FAK')),
    price           NUMERIC,
    size            NUMERIC,
    filled_size     NUMERIC NOT NULL DEFAULT 0,
    avg_fill_price  NUMERIC,
    status          TEXT NOT NULL CHECK (status IN ('PENDING','LIVE','PARTIAL','FILLED','CANCELLED','REJECTED')),
    mode            TEXT NOT NULL CHECK (mode IN ('paper','live-conservative','live-normal'))
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON our_orders(status, ts_created DESC);
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON our_orders(strategy, ts_created DESC);

CREATE TABLE IF NOT EXISTS positions (
    token_id        TEXT PRIMARY KEY,
    qty             NUMERIC NOT NULL,
    avg_cost        NUMERIC NOT NULL,
    last_mark       NUMERIC NOT NULL,
    last_updated    TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS pnl_daily (
    date            DATE PRIMARY KEY,
    starting_nav    NUMERIC NOT NULL,
    ending_nav      NUMERIC NOT NULL,
    realized        NUMERIC NOT NULL,
    unrealized      NUMERIC NOT NULL,
    fees            NUMERIC NOT NULL,
    trade_count     INT NOT NULL,
    win_count       INT NOT NULL
);
"""


_OPTIONAL_VECTOR_COLUMNS = """
-- pgvector-aware columns; added when the extension is available.
ALTER TABLE news_articles
    ADD COLUMN IF NOT EXISTS embedding VECTOR(1536);

CREATE INDEX IF NOT EXISTS idx_news_embedding_cos ON news_articles
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS market_embeddings (
    condition_id    TEXT PRIMARY KEY REFERENCES markets(condition_id),
    embedding       VECTOR(1536) NOT NULL,
    text_hash       TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_emb_cos ON market_embeddings
    USING hnsw (embedding vector_cosine_ops);
"""


_TIMESCALE_HYPERTABLES = [
    "orderbook_snapshots",
    "trades",
    "feature_snapshots",
]


async def initialize_schema(db: Database) -> dict[str, bool]:
    """Run schema on `db`. Returns a dict of features successfully enabled.

    Designed to be called on every agent boot — every statement is
    idempotent via IF NOT EXISTS.
    """
    enabled: dict[str, bool] = {
        "timescaledb": False,
        "pgvector": False,
        "pgcrypto": False,
        "tables": False,
    }

    async with db.acquire() as conn:
        # Try each extension independently. Most Railway / managed Postgres
        # instances don't have timescaledb or pgvector by default.
        for ext in _EXTENSIONS:
            try:
                await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
                enabled[_ext_key(ext)] = True
            except Exception as exc:
                log.info("schema.ext_missing", ext=ext, reason=str(exc)[:120])

        # Base tables — always succeeds on stock Postgres.
        try:
            await conn.execute(_BASE_TABLES)
            enabled["tables"] = True
        except Exception as exc:
            log.error("schema.tables_failed", error=str(exc))
            return enabled

        # Vector columns only if pgvector is available.
        if enabled.get("pgvector"):
            try:
                await conn.execute(_OPTIONAL_VECTOR_COLUMNS)
            except Exception as exc:
                log.warning("schema.vector_setup_failed", error=str(exc))

        # Hypertables only if TimescaleDB is available.
        if enabled.get("timescaledb"):
            for tbl in _TIMESCALE_HYPERTABLES:
                try:
                    await conn.execute(
                        f"SELECT create_hypertable('{tbl}', 'ts', if_not_exists => TRUE)"
                    )
                except Exception as exc:
                    log.warning("schema.hypertable_failed", table=tbl, error=str(exc))

    log.info(
        "schema.ready",
        timescaledb=enabled["timescaledb"],
        pgvector=enabled["pgvector"],
        pgcrypto=enabled["pgcrypto"],
        tables=enabled["tables"],
    )
    return enabled


def _ext_key(ext: str) -> str:
    return {"timescaledb": "timescaledb", "vector": "pgvector", "pgcrypto": "pgcrypto"}[ext]
