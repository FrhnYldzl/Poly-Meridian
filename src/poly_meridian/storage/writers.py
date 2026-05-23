"""Storage writers — idempotent upserts for ingestion-side rows. §12."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.storage.db import Database

log = structlog.get_logger("poly_meridian.storage.writers")


async def upsert_markets(db: Database, rows: list[dict[str, Any]]) -> int:
    """Upsert into `markets`. Returns rowcount."""
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
