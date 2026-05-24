"""Storage writers — idempotent upserts for ingestion-side rows. §12."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.storage.db import Database

log = structlog.get_logger("poly_meridian.storage.writers")


def _vector_literal(vec: list[float]) -> str:
    """pgvector accepts `[v1,v2,...]` as a text literal."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


async def upsert_markets(db: Database, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    async with db.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO markets (
                condition_id, question, category, sub_category, event_id,
                yes_token_id, no_token_id, end_date_iso, active, closed,
                liquidity_num, volume_num, raw, updated_at
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14
            )
            ON CONFLICT (condition_id) DO UPDATE SET
                question = EXCLUDED.question,
                category = EXCLUDED.category,
                sub_category = EXCLUDED.sub_category,
                event_id = EXCLUDED.event_id,
                yes_token_id = EXCLUDED.yes_token_id,
                no_token_id = EXCLUDED.no_token_id,
                end_date_iso = EXCLUDED.end_date_iso,
                active = EXCLUDED.active,
                closed = EXCLUDED.closed,
                liquidity_num = EXCLUDED.liquidity_num,
                volume_num = EXCLUDED.volume_num,
                raw = EXCLUDED.raw,
                updated_at = EXCLUDED.updated_at
            """,
            [
                (
                    r["condition_id"], r["question"], r.get("category"),
                    r.get("sub_category"), r.get("event_id"),
                    r["yes_token_id"], r["no_token_id"], r.get("end_date_iso"),
                    r.get("active", True), r.get("closed", False),
                    r.get("liquidity_num"), r.get("volume_num"),
                    json.dumps(r.get("raw") or {}, default=str),
                    r.get("updated_at"),
                )
                for r in rows
            ],
        )
    log.info("storage.upsert_markets", n=len(rows))
    return len(rows)


async def insert_orderbook_snapshot(
    db: Database,
    *,
    ts: datetime,
    token_id: str,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    mid: Decimal | None,
    microprice: Decimal | None,
    bid_depth_5pct: Decimal | None,
    ask_depth_5pct: Decimal | None,
    raw_levels: dict[str, Any] | None = None,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO orderbook_snapshots (
                ts, token_id, best_bid, best_ask, mid, microprice,
                bid_depth_5pct, ask_depth_5pct, raw_levels
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
            """,
            ts, token_id, best_bid, best_ask, mid, microprice,
            bid_depth_5pct, ask_depth_5pct,
            json.dumps(raw_levels or {}, default=str),
        )


async def insert_feature_snapshot(
    db: Database,
    *,
    ts: datetime,
    token_id: str,
    features: dict[str, float],
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO feature_snapshots (ts, token_id, features) VALUES ($1,$2,$3::jsonb)",
            ts, token_id, json.dumps(features),
        )


async def insert_news_article(
    db: Database,
    *,
    article_id: str,
    ts: datetime,
    source: str | None,
    title: str | None,
    body: str | None,
    url: str | None,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO news_articles (article_id, ts, source, title, body, url, processed)
            VALUES ($1,$2,$3,$4,$5,$6, FALSE)
            ON CONFLICT (article_id) DO NOTHING
            """,
            article_id, ts, source, title, body, url,
        )


async def set_news_embedding(
    db: Database,
    *,
    article_id: str,
    embedding: list[float],
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            f"UPDATE news_articles SET embedding = '{_vector_literal(embedding)}'::vector "
            "WHERE article_id = $1",
            article_id,
        )


async def mark_article_processed(db: Database, article_id: str) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE news_articles SET processed = TRUE WHERE article_id = $1",
            article_id,
        )


async def fetch_unprocessed_articles(
    db: Database, *, limit: int = 50
) -> list[dict[str, Any]]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT article_id, ts, source, title, body, url
            FROM news_articles
            WHERE processed = FALSE
            ORDER BY ts DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def upsert_market_embedding(
    db: Database,
    *,
    condition_id: str,
    embedding: list[float],
    text_hash: str,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO market_embeddings (condition_id, embedding, text_hash, updated_at)
            VALUES ($1, '{_vector_literal(embedding)}'::vector, $2, NOW())
            ON CONFLICT (condition_id) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                text_hash = EXCLUDED.text_hash,
                updated_at = NOW()
            WHERE market_embeddings.text_hash IS DISTINCT FROM EXCLUDED.text_hash
            """,
            condition_id, text_hash,
        )


async def market_needs_embedding(
    db: Database, *, condition_id: str, text_hash: str
) -> bool:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT text_hash FROM market_embeddings WHERE condition_id = $1",
            condition_id,
        )
    return row is None or row["text_hash"] != text_hash


async def find_top_k_markets_for_article(
    db: Database,
    *,
    article_id: str,
    k: int = 5,
    min_similarity: float = 0.4,
) -> list[dict[str, Any]]:
    """Cosine-similarity search across market_embeddings using pgvector."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH a AS (
                SELECT embedding FROM news_articles WHERE article_id = $1
            )
            SELECT
                m.condition_id,
                mk.question,
                mk.category,
                mk.yes_token_id,
                mk.no_token_id,
                1 - (m.embedding <=> a.embedding) AS similarity
            FROM market_embeddings m
            CROSS JOIN a
            JOIN markets mk ON mk.condition_id = m.condition_id
            WHERE mk.active = TRUE AND mk.closed = FALSE
              AND (1 - (m.embedding <=> a.embedding)) >= $2
            ORDER BY m.embedding <=> a.embedding ASC
            LIMIT $3
            """,
            article_id, min_similarity, k,
        )
        return [dict(r) for r in rows]


async def insert_news_signal(
    db: Database,
    *,
    ts: datetime,
    article_id: str,
    condition_id: str,
    sentiment: float,
    impact: float,
    direction: str,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO news_signals (
                ts, article_id, condition_id, sentiment, impact, direction
            ) VALUES ($1,$2,$3,$4,$5,$6)
            """,
            ts, article_id, condition_id,
            Decimal(str(sentiment)), Decimal(str(impact)), direction,
        )


async def fetch_recent_news_signals(
    db: Database,
    *,
    condition_id: str,
    window_sec: int,
) -> list[dict[str, Any]]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ts, sentiment, impact, direction, article_id
            FROM news_signals
            WHERE condition_id = $1
              AND ts >= NOW() - ($2 || ' seconds')::INTERVAL
            ORDER BY ts DESC
            """,
            condition_id, str(window_sec),
        )
        return [dict(r) for r in rows]


# ---------- strategy_signals + our_orders persistence ----------
# These survive Railway restarts so the dashboard backfills from DB on boot
# instead of going blank every deploy.


async def insert_strategy_signal(
    db: Database,
    *,
    ts: datetime,
    strategy: str,
    condition_id: str,
    token_id: str,
    edge: float,
    conviction: float,
    suggested_action: str,
    rationale: dict[str, Any] | None,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO strategy_signals (
                ts, strategy, condition_id, token_id,
                edge, conviction, suggested_action, rationale
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
            """,
            ts, strategy, condition_id, token_id,
            Decimal(str(edge)), Decimal(str(conviction)),
            suggested_action,
            json.dumps(rationale or {}, default=str),
        )


async def fetch_recent_strategy_signals(
    db: Database, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Most-recent strategy signals across all markets. Used by the broker
    on agent boot to backfill last_signals — otherwise the dashboard is
    blank after every Railway restart."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ts, strategy, condition_id, token_id,
                   edge::float8 AS edge, conviction::float8 AS conviction,
                   suggested_action, rationale
            FROM strategy_signals
            ORDER BY ts DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def upsert_order(
    db: Database,
    *,
    order_id: str,
    ts_created: datetime,
    ts_filled: datetime | None,
    strategy: str,
    token_id: str,
    side: str,
    order_type: str,
    price: Decimal | None,
    size: Decimal,
    filled_size: Decimal,
    avg_fill_price: Decimal | None,
    status: str,
    mode: str,
) -> None:
    """Upsert because pipeline pushes the same order_id on every state
    transition (PENDING → LIVE → FILLED). ON CONFLICT updates the mutable
    fields (status, fills) and leaves the identity fields alone."""
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO our_orders (
                order_id, ts_created, ts_filled, strategy, token_id, side,
                order_type, price, size, filled_size, avg_fill_price, status, mode
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
            )
            ON CONFLICT (order_id) DO UPDATE SET
                ts_filled       = EXCLUDED.ts_filled,
                filled_size     = EXCLUDED.filled_size,
                avg_fill_price  = EXCLUDED.avg_fill_price,
                status          = EXCLUDED.status
            """,
            order_id, ts_created, ts_filled, strategy, token_id, side,
            order_type, price, size, filled_size, avg_fill_price, status, mode,
        )


async def fetch_recent_orders(
    db: Database, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Most-recent orders for the dashboard's boot backfill."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT order_id, ts_created, ts_filled, strategy, token_id, side,
                   order_type,
                   price::float8 AS price,
                   size::float8 AS size,
                   filled_size::float8 AS filled_size,
                   avg_fill_price::float8 AS avg_fill_price,
                   status, mode
            FROM our_orders
            ORDER BY ts_created DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


# ---------- positions + pnl_daily persistence ----------
# Audit's #1 blocker: without these the promotion gate's "7 days paper"
# check has no data to query and live trading is forever-blocked.


async def upsert_position(
    db: Database,
    *,
    token_id: str,
    qty: Decimal,
    avg_cost: Decimal,
    last_mark: Decimal,
    last_updated: datetime,
) -> None:
    """Mirror a single position to the `positions` table. Called on every
    apply_fill + mark cycle in the ledger. Deletes the row when qty == 0
    so the table only carries open positions."""
    async with db.acquire() as conn:
        if qty == 0:
            await conn.execute(
                "DELETE FROM positions WHERE token_id = $1", token_id
            )
            return
        await conn.execute(
            """
            INSERT INTO positions (token_id, qty, avg_cost, last_mark, last_updated)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (token_id) DO UPDATE SET
                qty          = EXCLUDED.qty,
                avg_cost     = EXCLUDED.avg_cost,
                last_mark    = EXCLUDED.last_mark,
                last_updated = EXCLUDED.last_updated
            """,
            token_id, qty, avg_cost, last_mark, last_updated,
        )


async def fetch_positions(db: Database) -> list[dict[str, Any]]:
    """Restore positions on agent boot — used by ledger replay."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT token_id,
                   qty::float8 AS qty,
                   avg_cost::float8 AS avg_cost,
                   last_mark::float8 AS last_mark,
                   last_updated
            FROM positions
            """
        )
        return [dict(r) for r in rows]


async def upsert_pnl_daily(
    db: Database,
    *,
    date: datetime,
    starting_nav: Decimal,
    ending_nav: Decimal,
    realized: Decimal,
    unrealized: Decimal,
    fees: Decimal,
    trade_count: int,
    win_count: int,
) -> None:
    """One row per calendar day. Idempotent — re-running with a same-day
    snapshot just refreshes the values. Drives the promotion gate's
    paper-track metrics."""
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pnl_daily (
                date, starting_nav, ending_nav, realized, unrealized,
                fees, trade_count, win_count
            ) VALUES ($1::date,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (date) DO UPDATE SET
                ending_nav  = EXCLUDED.ending_nav,
                realized    = EXCLUDED.realized,
                unrealized  = EXCLUDED.unrealized,
                fees        = EXCLUDED.fees,
                trade_count = EXCLUDED.trade_count,
                win_count   = EXCLUDED.win_count
            """,
            date.date() if hasattr(date, "date") else date,
            starting_nav, ending_nav, realized, unrealized, fees,
            trade_count, win_count,
        )


async def fetch_pnl_daily(
    db: Database, *, days: int = 30
) -> list[dict[str, Any]]:
    """Recent daily P&L rows for the promotion gate + portfolio chart."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date,
                   starting_nav::float8 AS starting_nav,
                   ending_nav::float8 AS ending_nav,
                   realized::float8 AS realized,
                   unrealized::float8 AS unrealized,
                   fees::float8 AS fees,
                   trade_count, win_count
            FROM pnl_daily
            WHERE date >= NOW() - ($1 || ' days')::INTERVAL
            ORDER BY date DESC
            """,
            str(days),
        )
        return [dict(r) for r in rows]
