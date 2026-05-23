"""Normalize raw ingestion events into typed domain objects + DB-ready rows.

Every IngestionSource emits dicts shaped `{source, type, ts, payload, ...}`.
This module converts those into:
  - typed `Market` / `OrderBook` objects for in-memory consumers (features, strategies)
  - JSON-serializable row dicts for storage writers

See §11 + §12.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from poly_meridian.domain import Market, OrderBook, OrderBookLevel


def _to_decimal(v: Any, default: Decimal | None = None) -> Decimal | None:
    if v is None or v == "":
        return default
    try:
        return Decimal(str(v))
    except Exception:
        return default


def _to_datetime(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=UTC)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def gamma_market_to_domain(raw: dict[str, Any]) -> Market | None:
    """Convert a Gamma /markets row into a typed Market.

    Gamma exposes both `clobTokenIds` (a JSON-encoded list string) and
    direct fields. We tolerate either.
    """
    cond_id = raw.get("conditionId") or raw.get("condition_id")
    question = raw.get("question") or raw.get("title")
    if not cond_id or not question:
        return None

    yes_tid, no_tid = _extract_token_ids(raw)
    if not yes_tid or not no_tid:
        return None

    return Market(
        condition_id=str(cond_id),
        question=str(question),
        category=raw.get("category"),
        sub_category=raw.get("subCategory") or raw.get("sub_category"),
        event_id=str(raw["eventId"]) if raw.get("eventId") else None,
        yes_token_id=str(yes_tid),
        no_token_id=str(no_tid),
        end_date_iso=_to_datetime(raw.get("endDateIso") or raw.get("end_date_iso")),
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        liquidity_usd=_to_decimal(raw.get("liquidityNum") or raw.get("liquidity")),
        volume_usd=_to_decimal(raw.get("volumeNum") or raw.get("volume")),
    )


def _extract_token_ids(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    # Direct fields
    yes = raw.get("yesTokenId") or raw.get("yes_token_id")
    no = raw.get("noTokenId") or raw.get("no_token_id")
    if yes and no:
        return str(yes), str(no)

    # clobTokenIds is sometimes a JSON-encoded string, sometimes a list.
    tids = raw.get("clobTokenIds")
    if isinstance(tids, str):
        import json

        try:
            tids = json.loads(tids)
        except json.JSONDecodeError:
            tids = None
    if isinstance(tids, list) and len(tids) >= 2:
        return str(tids[0]), str(tids[1])

    # `outcomes` paired with `tokens`
    tokens = raw.get("tokens")
    if isinstance(tokens, list) and len(tokens) >= 2:
        return str(tokens[0].get("token_id")), str(tokens[1].get("token_id"))

    return None, None


def gamma_market_to_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Storage-writable row for the `markets` table. Idempotent upsert key = condition_id."""
    m = gamma_market_to_domain(raw)
    if m is None:
        return None
    return {
        "condition_id": m.condition_id,
        "question": m.question,
        "category": m.category,
        "sub_category": m.sub_category,
        "event_id": m.event_id,
        "yes_token_id": m.yes_token_id,
        "no_token_id": m.no_token_id,
        "end_date_iso": m.end_date_iso,
        "active": m.active,
        "closed": m.closed,
        "liquidity_num": m.liquidity_usd,
        "volume_num": m.volume_usd,
        "raw": raw,
        "updated_at": datetime.now(UTC),
    }


def book_snapshot_to_domain(payload: dict[str, Any]) -> OrderBook | None:
    """Convert a CLOB `book` snapshot into a typed OrderBook."""
    tid = payload.get("asset_id") or payload.get("token_id")
    if not tid:
        return None

    def _lvls(rows: Any) -> list[OrderBookLevel]:
        out: list[OrderBookLevel] = []
        if not isinstance(rows, list):
            return out
        for r in rows:
            price = _to_decimal(r.get("price"))
            size = _to_decimal(r.get("size"))
            if price is None or size is None or size <= 0:
                continue
            out.append(OrderBookLevel(price=price, size=size))
        return out

    bids = sorted(_lvls(payload.get("bids")), key=lambda lvl: lvl.price, reverse=True)
    asks = sorted(_lvls(payload.get("asks")), key=lambda lvl: lvl.price)
    ts = _to_datetime(payload.get("timestamp")) or datetime.now(UTC)
    return OrderBook(token_id=str(tid), ts=ts, bids=bids, asks=asks)
