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
    """Today's P&L as fraction of START-OF-DAY NAV.

    Phase N.5 fix: the previous implementation defaulted to
    `ledger.starting_cash` which is the session-genesis NAV, never reset.
    So "daily" was really "cumulative since boot" — the 5% kill-switch
    trigger fired on cumulative drawdown not daily, then never reset
    because tomorrow's start was still genesis.

    Caller is expected to pass `day_start_nav` from a snapshot taken at
    UTC midnight; the helper in main.py's persist loop does this. When
    omitted (e.g. early in the day before any snapshot exists), we fall
    back to current NAV (returns 0% — neutral, can't trip the kill-switch
    spuriously).
    """
    if day_start_nav is None:
        return 0.0
    if day_start_nav <= 0:
        return 0.0
    return float((nav_usd(ledger) - day_start_nav) / day_start_nav)


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
    """Per-day P&L roll-up for writing to `pnl_daily`.

    Phase N.3 fix: previously `win_count` was `sum(notional > 0)` — and
    SELL entries always have positive notional (cash in), so the metric
    counted SELL count, not wins. Real wins = SELL entries where
    exit_price > avg_cost_at_sell, which we approximate by sign of the
    realized contribution: gross_per_unit = (price - that-position's-
    avg_cost). Since the LedgerEntry doesn't store avg_cost, we infer
    from the entry's notional (post-fees) sign relative to its qty cost.
    For paper mode, simpler proxy: SELL with price > average of buy-side
    entries on the same token (counts as a "win").
    """
    realized = realized_pnl(ledger)
    unrealized = unrealized_pnl(ledger)
    all_entries = ledger.entries()
    trades_today = [e for e in all_entries if e.ts.date() == day]
    fees_today = sum((e.fee for e in trades_today), start=Decimal(0))

    # Per-token rolling avg_cost computed from full history so we can
    # tell wins from losses on today's SELLs.
    avg_cost_by_token: dict[str, Decimal] = {}
    qty_by_token: dict[str, Decimal] = {}
    win_count = 0
    for e in all_entries:
        if e.ts.date() > day:
            continue
        token = e.token_id
        if e.qty > 0:   # BUY adds at price
            prior_qty = qty_by_token.get(token, Decimal(0))
            prior_cost = avg_cost_by_token.get(token, Decimal(0))
            new_qty = prior_qty + e.qty
            if new_qty != 0:
                avg_cost_by_token[token] = (
                    (prior_qty * prior_cost + e.qty * e.price) / new_qty
                )
            qty_by_token[token] = new_qty
        else:           # SELL reduces; check if profitable
            if e.ts.date() == day:
                ac = avg_cost_by_token.get(token, Decimal(0))
                if ac > 0 and e.price > ac:
                    win_count += 1
            qty_by_token[token] = qty_by_token.get(token, Decimal(0)) + e.qty

    return {
        "date": day,  # type: ignore[dict-item]
        "starting_nav": ledger.starting_cash,
        "ending_nav": nav_usd(ledger),
        "realized": realized,
        "unrealized": unrealized,
        "fees": fees_today,
        "trade_count": len(trades_today),
        "win_count": win_count,
    }
