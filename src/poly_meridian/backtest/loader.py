"""Load historical data from Timescale into a `HistoricalDataset`.

Phase 4 reads only what's already in the schema. The replay is bounded by
how much history the agent has captured (Phase 1's WS subscription has
been writing `orderbook_snapshots` rows; that's the source of truth).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from poly_meridian.backtest.replay import HistoricalDataset, HistoricalTick
from poly_meridian.domain import Market
from poly_meridian.ingestion.normalize import gamma_market_to_domain
from poly_meridian.storage.db import Database

log = structlog.get_logger("poly_meridian.backtest.loader")


async def load_dataset_from_db(
    db: Database,
    *,
    start_ts: datetime,
    end_ts: datetime,
    condition_ids: list[str] | None = None,
    max_ticks: int = 1_000_000,
) -> HistoricalDataset:
    """Pull `orderbook_snapshots` between `[start_ts, end_ts)` for the
    given markets (or every active market if not specified).
    """
    async with db.acquire() as conn:
        if condition_ids:
            market_rows = await conn.fetch(
                "SELECT * FROM markets WHERE condition_id = ANY($1::text[])",
                condition_ids,
            )
        else:
            market_rows = await conn.fetch(
                "SELECT * FROM markets WHERE active = TRUE AND closed = FALSE LIMIT 200"
            )

        token_ids: list[str] = []
        markets: list[Market] = []
        for row in market_rows:
            raw = row.get("raw") if isinstance(row, dict) else dict(row).get("raw")
            base = dict(row)
            m = gamma_market_to_domain(raw or base)
            if m is None:
                continue
            markets.append(m)
            token_ids.extend([m.yes_token_id, m.no_token_id])

        if not token_ids:
            return HistoricalDataset(markets=[], ticks=[])

        snap_rows = await conn.fetch(
            """
            SELECT ts, token_id, raw_levels
            FROM orderbook_snapshots
            WHERE token_id = ANY($1::text[])
              AND ts >= $2 AND ts < $3
            ORDER BY ts ASC
            LIMIT $4
            """,
            token_ids, start_ts, end_ts, max_ticks,
        )

    ticks: list[HistoricalTick] = []
    for r in snap_rows:
        levels = _decode_levels(r["raw_levels"])
        ticks.append(
            HistoricalTick(
                ts=r["ts"],
                token_id=r["token_id"],
                bids=levels.get("bids", []),
                asks=levels.get("asks", []),
            )
        )

    log.info("backtest.loader.done", markets=len(markets), ticks=len(ticks))
    return HistoricalDataset(markets=markets, ticks=ticks)


def _decode_levels(raw: Any) -> dict[str, list[tuple[float, float]]]:
    if not raw:
        return {"bids": [], "asks": []}
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"bids": [], "asks": []}
    if not isinstance(raw, dict):
        return {"bids": [], "asks": []}

    def _decode(side_rows: Any) -> list[tuple[float, float]]:
        if not isinstance(side_rows, list):
            return []
        out: list[tuple[float, float]] = []
        for lvl in side_rows:
            try:
                p = float(lvl.get("price"))
                s = float(lvl.get("size"))
            except (TypeError, ValueError, AttributeError):
                continue
            if s > 0:
                out.append((p, s))
        return out

    return {
        "bids": _decode(raw.get("bids")),
        "asks": _decode(raw.get("asks")),
    }
