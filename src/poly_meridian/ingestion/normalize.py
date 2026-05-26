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


# Gamma's /markets endpoint returns `category=None` for everything. The real
# categorization lives in /events as a `tags` array (e.g. ["Politics",
# "Macron", "France", "2025 Predictions"]). We pick the *first* canonical
# Polymarket category in the priority order below — Sports beats Business
# beats Politics, so "Sports + Business + Politics" → "Sports". This matches
# how Polymarket itself displays markets.
_CANONICAL_CATEGORIES: tuple[str, ...] = (
    "Sports",
    "Crypto",
    "Politics",
    "Climate",
    "Science",
    "Tech",
    "Pop Culture",
    "Business",
)


def derive_category_from_tags(tags: Any) -> str | None:
    """Pick the first canonical category tag (case-insensitive) from a tag
    list. Tags can be strings or `{label, ...}` dicts — Gamma uses both
    shapes depending on endpoint. Returns None when no canonical tag matches.
    """
    if not tags or not isinstance(tags, list):
        return None
    labels: list[str] = []
    for t in tags:
        if isinstance(t, str):
            labels.append(t.strip())
        elif isinstance(t, dict):
            lbl = t.get("label") or t.get("name")
            if isinstance(lbl, str):
                labels.append(lbl.strip())
    if not labels:
        return None
    lc_labels = {l.lower() for l in labels}
    for canon in _CANONICAL_CATEGORIES:
        if canon.lower() in lc_labels:
            return canon
    return None


def build_event_category_map(events: list[dict[str, Any]]) -> dict[str, str]:
    """Build event_id → canonical_category. Skips events with no derivable
    category so caller can still default to 'Other'."""
    out: dict[str, str] = {}
    for e in events:
        cat = derive_category_from_tags(e.get("tags"))
        if not cat:
            continue
        eid = e.get("id") or e.get("event_id")
        if eid is None:
            continue
        out[str(eid)] = cat
    return out


def extract_event_id(market: dict[str, Any]) -> str | None:
    """Pull the event id off a Gamma market row.

    Gamma's response shape is annoying here: the top-level `eventId` field
    is consistently `None`, and the actual event id sits inside an embedded
    `events` list as `events[0].id`. Try every shape we've seen in the wild.
    """
    direct = market.get("eventId") or market.get("event_id")
    if direct:
        return str(direct)
    events = market.get("events")
    if isinstance(events, list) and events:
        first = events[0]
        if isinstance(first, dict):
            eid = first.get("id") or first.get("event_id")
            if eid:
                return str(eid)
    return None


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
        # Gamma puts the event id in different places; extract_event_id covers
        # the eventId / event_id / events[0].id shapes we've seen in the wild.
        event_id=extract_event_id(raw),
        yes_token_id=str(yes_tid),
        no_token_id=str(no_tid),
        # Phase Q.2b: Gamma's actual field is `endDate` (ISO-8601 string).
        # `endDateIso` was a guess that returned None for every market and
        # left stat_quant.time_decay permanently dark. Try all known
        # variants and fall back to `end_date_iso` for legacy/test rows.
        end_date_iso=_to_datetime(
            raw.get("endDate")
            or raw.get("endDateIso")
            or raw.get("end_date_iso")
        ),
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
