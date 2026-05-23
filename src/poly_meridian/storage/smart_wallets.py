"""Smart-wallet table writers. See MASTER_SPEC v1.1 §12 + §14.3."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.storage.db import Database

log = structlog.get_logger("poly_meridian.storage.smart_wallets")


async def upsert_smart_wallet(
    db: Database,
    *,
    address: str,
    tier: int,
    label: str | None = None,
    lifetime_pnl: Decimal | None = None,
    win_rate: Decimal | None = None,
    trade_count: int | None = None,
    last_7d_pnl: Decimal | None = None,
    category_focus: str | None = None,
    recency_score: float = 0.0,
    hedge_flag: bool = False,
    drawdown_7d_pct: Decimal | None = None,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO smart_wallets (
                address, label, lifetime_pnl, win_rate, trade_count, last_updated,
                tier, category_focus, last_7d_pnl, recency_score, hedge_flag,
                drawdown_7d_pct
            )
            VALUES (
                $1, $2, $3, $4, $5, NOW(), $6, $7, $8, $9, $10, $11
            )
            ON CONFLICT (address) DO UPDATE SET
                label           = COALESCE(EXCLUDED.label, smart_wallets.label),
                lifetime_pnl    = COALESCE(EXCLUDED.lifetime_pnl, smart_wallets.lifetime_pnl),
                win_rate        = COALESCE(EXCLUDED.win_rate, smart_wallets.win_rate),
                trade_count     = COALESCE(EXCLUDED.trade_count, smart_wallets.trade_count),
                last_updated    = NOW(),
                tier            = EXCLUDED.tier,
                category_focus  = COALESCE(EXCLUDED.category_focus, smart_wallets.category_focus),
                last_7d_pnl     = EXCLUDED.last_7d_pnl,
                recency_score   = EXCLUDED.recency_score,
                hedge_flag      = EXCLUDED.hedge_flag,
                drawdown_7d_pct = EXCLUDED.drawdown_7d_pct
            """,
            address, label, lifetime_pnl, win_rate, trade_count,
            tier, category_focus, last_7d_pnl, Decimal(str(recency_score)),
            hedge_flag, drawdown_7d_pct,
        )


async def list_wallets_by_tier(
    db: Database, *, tier: int, limit: int = 200
) -> list[dict[str, Any]]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                address, label, tier, category_focus,
                lifetime_pnl, win_rate, trade_count,
                last_7d_pnl, recency_score, hedge_flag, drawdown_7d_pct,
                last_updated
            FROM smart_wallets
            WHERE tier = $1
            ORDER BY lifetime_pnl DESC NULLS LAST
            LIMIT $2
            """,
            tier, limit,
        )
        return [dict(r) for r in rows]


async def list_eligible_wallets(
    db: Database, *, drawdown_7d_max: float = 0.20
) -> list[dict[str, Any]]:
    """Wallets in Tier 1 or 2, with loss filter (-DD/7d under threshold) applied."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                address, label, tier, category_focus,
                lifetime_pnl, win_rate, trade_count, last_7d_pnl,
                recency_score, hedge_flag, drawdown_7d_pct
            FROM smart_wallets
            WHERE tier IN (1, 2)
              AND COALESCE(drawdown_7d_pct, 0) < $1
              AND hedge_flag = FALSE
            ORDER BY tier ASC, lifetime_pnl DESC NULLS LAST
            """,
            Decimal(str(drawdown_7d_max)),
        )
        return [dict(r) for r in rows]


async def count_by_tier(db: Database) -> dict[int, int]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tier, COUNT(*) AS n FROM smart_wallets GROUP BY tier ORDER BY tier"
        )
        return {r["tier"]: r["n"] for r in rows}


async def update_recency_scores(db: Database, *, ts: datetime | None = None) -> int:
    """Recompute recency_score = exp(-age_days / 30), where age_days =
    (now - last_updated). Lazy refresh — call from cron."""
    now = ts or datetime.now(UTC)
    async with db.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE smart_wallets
            SET recency_score = GREATEST(0, EXP(
                -EXTRACT(EPOCH FROM ($1 - last_updated)) / (30 * 24 * 3600)
            ))
            WHERE last_updated IS NOT NULL
            """,
            now,
        )
        return int(result.split()[-1]) if result else 0
