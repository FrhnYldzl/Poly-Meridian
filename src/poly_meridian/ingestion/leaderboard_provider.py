"""Polymarket leaderboard polling. See MASTER_SPEC v1.1 §11.7.

Strategy:
  1. Try the documented public data-api endpoint first.
  2. If that 404s or returns nothing, try a list of candidate endpoints
     discovered from network-tab inspection of polymarket.com.
  3. Fall back to a no-op + warning if nothing works (operator must
     populate `config/smart_wallets.yaml` manually).

The actual endpoint shape and pagination vary across Polymarket releases.
We isolate that here behind a typed `LeaderboardEntry` so callers don't
care which endpoint succeeded.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger("poly_meridian.leaderboard")


# Candidate endpoints — tried in order until one returns rows.
# Discovery: paste the leaderboard URL into Chrome devtools, watch the
# XHR tab for the JSON call.
DEFAULT_CANDIDATE_URLS: tuple[str, ...] = (
    "https://data-api.polymarket.com/leaderboard",
    "https://gamma-api.polymarket.com/leaderboard",
    "https://polymarket.com/api/leaderboard",
)


@dataclass(frozen=True)
class LeaderboardEntry:
    address: str
    display_name: str | None
    lifetime_pnl_usd: Decimal | None
    win_rate: float | None              # 0..1
    trade_count: int | None
    last_7d_pnl_usd: Decimal | None
    drawdown_7d_pct: float | None       # 0..1, positive number = drawdown
    category_focus: str | None
    last_trade_ts: datetime | None
    raw: dict[str, Any]                 # original API row for debugging


@dataclass(frozen=True)
class TierThresholds:
    """MASTER_SPEC v1.1 §14.3. Numbers are tunable per ops."""

    tier1_lifetime_pnl_usd: float = 500_000
    tier1_win_rate: float = 0.55
    tier1_trade_count: int = 200
    tier1_active_days_min: int = 90
    tier1_drawdown_7d_max: float = 0.20

    tier2_last_30d_pnl_usd: float = 50_000
    tier2_last_30d_win_rate: float = 0.52


def classify_tier(entry: LeaderboardEntry, t: TierThresholds | None = None) -> int:
    """Return 1, 2, or 3 based on v1.1 thresholds. Defaults to 3 (gözlem)."""
    t = t or TierThresholds()
    pnl = float(entry.lifetime_pnl_usd) if entry.lifetime_pnl_usd is not None else 0.0
    wr = entry.win_rate or 0.0
    tc = entry.trade_count or 0
    dd = entry.drawdown_7d_pct or 0.0
    last7 = float(entry.last_7d_pnl_usd) if entry.last_7d_pnl_usd is not None else 0.0

    if (
        pnl >= t.tier1_lifetime_pnl_usd
        and wr >= t.tier1_win_rate
        and tc >= t.tier1_trade_count
        and dd < t.tier1_drawdown_7d_max
    ):
        return 1
    if last7 >= t.tier2_last_30d_pnl_usd and wr >= t.tier2_last_30d_win_rate:
        return 2
    return 3


class LeaderboardProvider:
    """Polls Polymarket leaderboard endpoints, normalizes rows, returns
    `LeaderboardEntry` objects. Stateless — caller decides how to persist.

    Phase 5a usage: cron task calls `fetch_top_traders(...)`, classifies
    each entry into a tier, upserts into `smart_wallets`.
    """

    def __init__(
        self,
        *,
        candidate_urls: tuple[str, ...] = DEFAULT_CANDIDATE_URLS,
        timeout_sec: float = 20.0,
    ) -> None:
        self._urls = candidate_urls
        self._timeout = timeout_sec
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LeaderboardProvider":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": "poly-meridian/0.1",
                    "Accept": "application/json",
                },
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_top_traders(
        self,
        *,
        category: str | None = None,
        period: str = "month",   # today | week | month | all
        sort: str = "profit",    # profit | volume
        limit: int = 100,
    ) -> list[LeaderboardEntry]:
        """Try each candidate URL with the given params, return first
        non-empty page (after normalization)."""
        assert self._client is not None, "call start() first"
        params: dict[str, str | int] = {
            "period": period,
            "sort": sort,
            "limit": limit,
        }
        if category:
            params["category"] = category

        last_err: Exception | None = None
        for url in self._urls:
            try:
                rows = await self._get_json(url, params=params)
            except Exception as exc:
                last_err = exc
                log.debug("leaderboard.url_failed", url=url, error=str(exc))
                continue
            entries = _normalize_rows(rows)
            if entries:
                log.info("leaderboard.fetch_ok", url=url, n=len(entries))
                return entries
            log.debug("leaderboard.empty", url=url)

        if last_err:
            log.warning("leaderboard.no_endpoint_worked", last_error=str(last_err))
        else:
            log.warning("leaderboard.empty_everywhere")
        return []

    async def _get_json(self, url: str, *, params: dict[str, Any]) -> Any:
        assert self._client is not None
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, asyncio.TimeoutError)),
            stop=stop_after_attempt(2),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
        ):
            with attempt:
                r = await self._client.get(url, params=params)
                if r.status_code == 404:
                    raise RuntimeError("not_found")
                r.raise_for_status()
                return r.json()
        return None


def _normalize_rows(payload: Any) -> list[LeaderboardEntry]:
    """Walk a few common envelope shapes (`[…]` / `{"data": […]}` /
    `{"leaderboard": […]}`) and produce LeaderboardEntry rows."""
    if payload is None:
        return []
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "leaderboard", "rows", "results"):
            if key in payload and isinstance(payload[key], list):
                rows = payload[key]
                break
    return [e for e in (_one(row) for row in rows) if e is not None]


def _one(row: Any) -> LeaderboardEntry | None:
    if not isinstance(row, dict):
        return None
    addr = row.get("address") or row.get("wallet") or row.get("proxyWallet")
    if not isinstance(addr, str) or not addr.startswith("0x"):
        return None
    return LeaderboardEntry(
        address=addr.lower(),
        display_name=row.get("name") or row.get("displayName") or row.get("username"),
        lifetime_pnl_usd=_decimal(row.get("pnl") or row.get("lifetimePnl")),
        win_rate=_float_pct(row.get("winRate") or row.get("win_rate")),
        trade_count=_int(row.get("tradeCount") or row.get("trades")),
        last_7d_pnl_usd=_decimal(row.get("pnl7d") or row.get("last7dPnl") or row.get("week_pnl")),
        drawdown_7d_pct=_float_pct(row.get("drawdown7d") or row.get("dd_7d")),
        category_focus=row.get("primaryCategory") or row.get("categoryFocus"),
        last_trade_ts=_dt(row.get("lastTrade") or row.get("last_active")),
        raw=row,
    )


def _decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _float_pct(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Tolerate 0..1 or 0..100 conventions.
    return f / 100.0 if f > 1.0 else f


def _int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime | None:
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
