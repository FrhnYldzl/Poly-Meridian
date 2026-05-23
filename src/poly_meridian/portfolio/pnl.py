"""P&L decomposition + portfolio snapshot helpers. §17."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

from poly_meridian.domain import Position, PortfolioSnapshot
from poly_meridian.portfolio.ledger import Ledger


def nav_usd(ledger: Ledger) -> Decimal:
    """Cash + sum(qty × last_mark)."""
    total_pos_value = sum(
        (p.qty * p.last_mark for p in ledger.positions()),
        start=Decimal(0),
    )
    return ledger.cash + total_pos_value


def realized_pnl(ledger: Ledger) -> Decimal:
    return sum((p.realized_pnl for p in ledger.positions()), start=Decimal(0))


def unrealized_pnl(ledger: Ledger) -> Decimal:
    return sum((p.unrealized_pnl() for p in ledger.positions()), start=Decimal(0))


def total_exposure_usd(ledger: Ledger) -> Decimal:
    """Absolute value of all open positions at mark."""
    return sum(
        (abs(p.qty) * p.last_mark for p in ledger.positions()),
        start=Decimal(0),
    )


def category_exposure_usd(
    ledger: Ledger,
    token_to_category: dict[str, str],
) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for p in ledger.positions():
        cat = token_to_category.get(p.token_id, "uncategorized")
        out[cat] += abs(p.qty) * p.last_mark
    return dict(out)


def daily_pnl_pct(ledger: Ledger, day_start_nav: Decimal | None = None) -> float:
    """Today's P&L as fraction of starting NAV. Defaults to using ledger's starting cash."""
    start = day_start_nav or ledger.starting_cash
    if start <= 0:
        return 0.0
    return float((nav_usd(ledger) - start) / start)


def snapshot(
    ledger: Ledger,
    *,
    token_to_category: dict[str, str] | None = None,
    day_start_nav: Decimal | None = None,
    ts: datetime | None = None,
) -> PortfolioSnapshot:
    """Build the PortfolioSnapshot the RiskPolicy reads from. §15.4."""
    ts = ts or datetime.now(UTC)
    nav = nav_usd(ledger)
    total_expo = total_exposure_usd(ledger)
    total_expo_pct = float(total_expo / nav) if nav > 0 else 0.0

    cat_pct: dict[str, float] = {}
    if token_to_category:
        cat_expo = category_exposure_usd(ledger, token_to_category)
        cat_pct = {k: float(v / nav) for k, v in cat_expo.items() if nav > 0}

    positions = [
        Position(
            token_id=p.token_id,
            qty=p.qty,
            avg_cost=p.avg_cost,
            last_mark=p.last_mark,
            last_updated=p.last_updated,
        )
        for p in ledger.positions()
    ]

    return PortfolioSnapshot(
        ts=ts,
        nav_usd=nav,
        cash_usd=ledger.cash,
        positions=positions,
        daily_pnl_pct=daily_pnl_pct(ledger, day_start_nav),
        total_exposure_pct=total_expo_pct,
        category_exposure_pct=cat_pct,
        open_position_count=len(positions),
    )


def daily_roll_up(ledger: Ledger, day: date) -> dict[str, Decimal | int]:
    """Per-day P&L roll-up for writing to `pnl_daily`."""
    realized = realized_pnl(ledger)
    unrealized = unrealized_pnl(ledger)
    trades_today = [e for e in ledger.entries() if e.ts.date() == day]
    fees_today = sum((e.fee for e in trades_today), start=Decimal(0))

    return {
        "date": day,  # type: ignore[dict-item]
        "starting_nav": ledger.starting_cash,
        "ending_nav": nav_usd(ledger),
        "realized": realized,
        "unrealized": unrealized,
        "fees": fees_today,
        "trade_count": len(trades_today),
        "win_count": sum(1 for e in trades_today if e.notional > 0),  # type: ignore[misc]
    }
