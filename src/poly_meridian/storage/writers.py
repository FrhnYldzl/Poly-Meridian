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
