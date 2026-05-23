"""Cron: refresh smart_wallets from the Polymarket leaderboard.

Schedule (suggested):  daily at 02:00 UTC.

Usage:
    docker compose run --rm agent python -m scripts.refresh_smart_wallets
"""
from __future__ import annotations

import asyncio
import sys

import structlog

from poly_meridian.ingestion.leaderboard_provider import (
    LeaderboardEntry,
    LeaderboardProvider,
    TierThresholds,
    classify_tier,
)
from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.storage import close_db, get_db
from poly_meridian.storage.smart_wallets import (
    count_by_tier,
    update_recency_scores,
    upsert_smart_wallet,
)

log = structlog.get_logger("poly_meridian.scripts.refresh_smart_wallets")


async def _refresh(limit: int = 200) -> int:
    """Fetch leaderboard, classify, upsert. Returns rows written."""
    thresholds = TierThresholds()
    written = 0
    async with LeaderboardProvider() as lb:
        entries: list[LeaderboardEntry] = await lb.fetch_top_traders(
            period="month", sort="profit", limit=limit
        )

    if not entries:
        log.warning("refresh.empty", msg="leaderboard returned 0 entries — manual seed needed")
        return 0

    db = await get_db()
    for e in entries:
        tier = classify_tier(e, thresholds)
        try:
            await upsert_smart_wallet(
                db,
                address=e.address,
                tier=tier,
                label=e.display_name,
                lifetime_pnl=e.lifetime_pnl_usd,
                win_rate=e.win_rate and __import__("decimal").Decimal(str(e.win_rate)),
                trade_count=e.trade_count,
                last_7d_pnl=e.last_7d_pnl_usd,
                category_focus=e.category_focus,
                drawdown_7d_pct=e.drawdown_7d_pct
                    and __import__("decimal").Decimal(str(e.drawdown_7d_pct)),
            )
            written += 1
        except Exception as exc:
            log.warning("refresh.upsert_failed", addr=e.address, error=str(exc))

    await update_recency_scores(db)
    counts = await count_by_tier(db)
    await close_db()
    log.info("refresh.done", written=written, counts=counts)
    return written


def main() -> None:
    configure_logging("INFO")
    written = asyncio.run(_refresh())
    sys.exit(0 if written > 0 else 1)


if __name__ == "__main__":
    main()
