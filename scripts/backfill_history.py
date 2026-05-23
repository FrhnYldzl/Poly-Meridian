"""One-shot backfill: pulls active markets from Gamma and upserts them.

Run inside the agent container (so it can reach the `db` service):
    docker compose run --rm agent python -m scripts.backfill_history

Phase 1: only the markets catalog is backfilled — order-book history
requires a long-running WS subscription, which the agent itself does.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from poly_meridian.ingestion import GammaClient
from poly_meridian.ingestion.normalize import gamma_market_to_row
from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.storage import close_db, get_db
from poly_meridian.storage.writers import upsert_markets


async def _run() -> int:
    configure_logging("INFO")
    rows: list[dict[str, Any]] = []
    async with GammaClient() as g:
        raw = await g.iter_active_markets()
    for r in raw:
        row = gamma_market_to_row(r)
        if row is not None:
            rows.append(row)

    if not rows:
        print("backfill: no markets returned from Gamma")
        return 1

    db = await get_db()
    n = await upsert_markets(db, rows)
    print(f"backfill: upserted {n} markets")
    await close_db()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
