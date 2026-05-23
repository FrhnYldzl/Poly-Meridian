#!/usr/bin/env bash
# Bootstrap Poly Meridian database schema.
# Idempotent: safe to re-run. Picks up POSTGRES_USER / POSTGRES_DB from env.
#
# Spec: docs/MASTER_SPEC.md §12.
set -euo pipefail

DB_USER="${POSTGRES_USER:-poly}"
DB_NAME="${POSTGRES_DB:-poly_meridian}"

psql -v ON_ERROR_STOP=1 --username "$DB_USER" --dbname "$DB_NAME" <<-'EOSQL'

-- ============================================================================
-- Extensions
-- ============================================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector for news embeddings
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- markets — metadata from Polymarket Gamma
-- ============================================================================
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

-- ============================================================================
-- orderbook_snapshots — TimescaleDB hypertable
-- ============================================================================
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
SELECT create_hypertable('orderbook_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_obs_token_ts ON orderbook_snapshots(token_id, ts DESC);

-- ============================================================================
-- trades — TimescaleDB hypertable (own + on-chain global)
-- ============================================================================
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
SELECT create_hypertable('trades', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_addr  ON trades(maker_address, taker_address);

-- ============================================================================
-- news_articles — raw articles + embeddings
-- ============================================================================
CREATE TABLE IF NOT EXISTS news_articles (
    article_id      TEXT PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    source          TEXT,
    title           TEXT,
    body            TEXT,
    url             TEXT,
    embedding       VECTOR(768),
    processed       BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_articles(ts DESC);

-- ============================================================================
-- news_signals — article → market mapping with sentiment/impact
-- ============================================================================
CREATE TABLE IF NOT EXISTS news_signals (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    article_id      TEXT NOT NULL REFERENCES news_articles(article_id),
    condition_id    TEXT NOT NULL REFERENCES markets(condition_id),
    sentiment       NUMERIC NOT NULL,        -- -1..1
    impact          NUMERIC NOT NULL,        --  0..1
    direction       TEXT NOT NULL CHECK (direction IN ('YES','NO','NEUTRAL'))
);
CREATE INDEX IF NOT EXISTS idx_news_sig_cond_ts ON news_signals(condition_id, ts DESC);

-- ============================================================================
-- smart_wallets — tracked top-trader addresses
-- ============================================================================
CREATE TABLE IF NOT EXISTS smart_wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    lifetime_pnl    NUMERIC,
    win_rate        NUMERIC,
    trade_count     INT,
    last_updated    TIMESTAMPTZ
);

-- ============================================================================
-- feature_snapshots — per-tick feature vectors
-- ============================================================================
CREATE TABLE IF NOT EXISTS feature_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    features        JSONB NOT NULL
);
SELECT create_hypertable('feature_snapshots', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_feat_token_ts ON feature_snapshots(token_id, ts DESC);

-- ============================================================================
-- strategy_signals — per-strategy output
-- ============================================================================
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

-- ============================================================================
-- our_orders — every order we submit (paper or live)
-- ============================================================================
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

-- ============================================================================
-- positions — current portfolio snapshot
-- ============================================================================
CREATE TABLE IF NOT EXISTS positions (
    token_id        TEXT PRIMARY KEY,
    qty             NUMERIC NOT NULL,
    avg_cost        NUMERIC NOT NULL,
    last_mark       NUMERIC NOT NULL,
    last_updated    TIMESTAMPTZ NOT NULL
);

-- ============================================================================
-- pnl_daily — daily P&L roll-ups
-- ============================================================================
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

-- ============================================================================
-- TimescaleDB retention / compression policies (idempotent attempts)
-- ============================================================================
DO $$
BEGIN
    PERFORM add_retention_policy('orderbook_snapshots', INTERVAL '90 days', if_not_exists => TRUE);
    PERFORM add_retention_policy('feature_snapshots',   INTERVAL '90 days', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN
    -- Older Timescale versions may not support if_not_exists; ignore.
    NULL;
END$$;

EOSQL

echo "[bootstrap_db] schema OK"
