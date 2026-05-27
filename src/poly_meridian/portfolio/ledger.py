"""Double-entry ledger. Every fill creates two entries: a position delta
and a cash delta. Pure in-memory state — persistence is a separate concern
(writers live in `storage/`).

See MASTER_SPEC §17.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from poly_meridian.domain import Order, OrderStatus, Side

log = structlog.get_logger("poly_meridian.portfolio.ledger")


@dataclass(frozen=True)
class LedgerEntry:
    ts: datetime
    order_id: str
    strategy: str
    token_id: str
    side: Side
    qty: Decimal              # positive for opening, negative for closing
    price: Decimal
    notional: Decimal         # signed cash flow: negative when paying out, positive when receiving
    fee: Decimal = Decimal(0)


@dataclass
class PositionState:
    token_id: str
    qty: Decimal = Decimal(0)
    avg_cost: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    last_mark: Decimal = Decimal(0)
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Phase N.6: cumulative fees paid on the buy side of this position.
    # Used to net realized P&L correctly on the sell side. Without this,
    # `realized_pnl` was gross-of-fees, inflating Sharpe ~2× per fee level.
    fees_paid: Decimal = Decimal(0)
    # Phase R.4 — entry strategy name + horizon hint. Set on the first
    # BUY fill (carried through subsequent BUYs since avg_cost is
    # weighted-avg). ExitMonitor reads `horizon == "to_resolution"` to
    # skip profit_take / stop_loss triggers for LLM-driven entries —
    # those bets are evidence-driven and should ride to binary settle.
    entry_strategy: str | None = None
    horizon: str | None = None   # "intraday" | "to_resolution"

    def unrealized_pnl(self) -> Decimal:
        return self.qty * (self.last_mark - self.avg_cost)


class Ledger:
    """Sole source of truth for positions + cash. All mutations are explicit."""

    def __init__(self, starting_cash_usd: Decimal) -> None:
        self._cash: Decimal = starting_cash_usd
        self._entries: list[LedgerEntry] = []
        self._positions: dict[str, PositionState] = {}
        self._starting_cash: Decimal = starting_cash_usd

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def starting_cash(self) -> Decimal:
        return self._starting_cash

    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def positions(self) -> list[PositionState]:
        return [p for p in self._positions.values() if p.qty != 0]

    def get_position(self, token_id: str) -> PositionState | None:
        p = self._positions.get(token_id)
        return p if p and p.qty != 0 else None

    def apply_fill(
        self,
        *,
        ts: datetime,
        order: Order,
        filled_qty: Decimal,
        fill_price: Decimal,
        fee: Decimal = Decimal(0),
    ) -> LedgerEntry:
        """Apply a fill to cash + position. Returns the LedgerEntry created."""
        pos = self._positions.setdefault(order.token_id, PositionState(token_id=order.token_id))

        if order.side == Side.BUY:
            signed_qty = filled_qty
            notional = -(filled_qty * fill_price) - fee   # cash out
            # weighted-average cost when adding to position
            new_qty = pos.qty + signed_qty
            if new_qty != 0:
                pos.avg_cost = (
                    (pos.qty * pos.avg_cost + signed_qty * fill_price) / new_qty
                ).quantize(Decimal("0.0001"))
            pos.qty = new_qty
            # Track buy-side fees so the SELL leg can net them out (Phase N.6).
            pos.fees_paid += fee
            # Phase R.4 — stamp entry strategy + horizon on FIRST buy.
            # Subsequent BUYs (size topups) keep the original entry tag —
            # the Q.3 concentration guard normally blocks these anyway.
            if pos.entry_strategy is None:
                pos.entry_strategy = order.strategy
                pos.horizon = (
                    "to_resolution"
                    if (order.strategy or "").startswith("fundamentals")
                    else "intraday"
                )
        else:  # SELL — reduce position, realize PnL
            signed_qty = -filled_qty
            notional = (filled_qty * fill_price) - fee    # cash in
            # Realized P&L NET of fees on both legs:
            #   gross = (sell_price - avg_cost) × qty
            #   buy_fee_share = fees_paid × (qty_sold / qty_held_before_sell)
            #   net = gross - buy_fee_share - this_sell_fee
            gross_per_unit = fill_price - pos.avg_cost
            buy_fee_share = (
                pos.fees_paid * filled_qty / pos.qty if pos.qty > 0 else Decimal(0)
            )
            realized_net = gross_per_unit * filled_qty - buy_fee_share - fee
            pos.realized_pnl += realized_net
            # Reduce the carried buy-fee pool by the share we just attributed.
            pos.fees_paid = max(Decimal(0), pos.fees_paid - buy_fee_share)
            pos.qty += signed_qty

        pos.last_mark = fill_price
        pos.last_updated = ts

        self._cash += notional

        entry = LedgerEntry(
            ts=ts,
            order_id=order.order_id,
            strategy=order.strategy,
            token_id=order.token_id,
            side=order.side,
            qty=signed_qty,
            price=fill_price,
            notional=notional,
            fee=fee,
        )
        self._entries.append(entry)
        log.info(
            "ledger.fill",
            order_id=order.order_id,
            token_id=order.token_id,
            side=str(order.side),
            qty=str(signed_qty),
            price=str(fill_price),
            cash_after=str(self._cash),
            pos_qty=str(pos.qty),
        )
        return entry

    def mark(self, token_id: str, mid_price: Decimal, ts: datetime | None = None) -> None:
        pos = self._positions.get(token_id)
        if pos is None or pos.qty == 0:
            return
        pos.last_mark = mid_price
        pos.last_updated = ts or datetime.now(UTC)


def has_realized_pnl(order: Order) -> bool:
    """Helper — only SELL fills produce realized P&L for now."""
    return order.side == Side.SELL and order.status in (OrderStatus.PARTIAL, OrderStatus.FILLED)
