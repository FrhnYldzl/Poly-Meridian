"""Double-entry ledger. Every fill creates two entries: a position delta
and a cash delta. Pure in-memory state — persistence is a separate concern
(writers live in `storage/`).

See MASTER_SPEC §17.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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
    # Phase R.8 — LLM-claimed forecast on this position. Stamped by
    # paper_executor.apply_fill_with_prediction() (extended path used
    # by FundamentalsStrategy via the signal rationale). Read by
    # CalibrationStore on settlement to compute Brier score.
    #   claimed_p_long       — LLM's p_yes for the side we BOUGHT
    #                          (0.62 means "62% the side we bought wins")
    #   claimed_confidence   — LLM's self-rated confidence in that estimate
    #   claimed_base_rate    — LLM's base-rate prior (deep-tier only)
    claimed_p_long: float | None = None
    claimed_confidence: float | None = None
    claimed_base_rate: float | None = None

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

    def restore_positions(self, rows: list[dict[str, Any]]) -> int:
        """Phase T — rebuild PositionState from persisted DB rows on boot.

        Each row carries the full Phase R metadata (entry_strategy,
        horizon, fees_paid, claimed_p_long/confidence/base_rate) so a
        Railway restart preserves the audit trail and hold-to-resolution
        contract. Returns the number of positions restored.

        We deliberately do NOT replay individual ledger entries — that
        would be slow and risk re-applying fees. The persisted positions
        table is the source of truth for state at restart; ledger entries
        are the audit log.

        Cash is NOT restored here — it's computed from current ledger
        starting_cash minus net notional carried in the restored positions.
        """
        restored = 0
        for row in rows:
            try:
                qty = Decimal(str(row.get("qty", 0)))
                if qty == 0:
                    continue
                token_id = str(row["token_id"])
                avg_cost = Decimal(str(row.get("avg_cost", 0)))
                last_mark = Decimal(str(row.get("last_mark", 0)))
                last_updated = row.get("last_updated") or datetime.now(UTC)
                pos = PositionState(
                    token_id=token_id,
                    qty=qty,
                    avg_cost=avg_cost,
                    realized_pnl=Decimal(str(row.get("realized_pnl") or 0)),
                    last_mark=last_mark,
                    last_updated=last_updated,
                    fees_paid=Decimal(str(row.get("fees_paid") or 0)),
                    entry_strategy=row.get("entry_strategy"),
                    horizon=row.get("horizon"),
                    claimed_p_long=row.get("claimed_p_long"),
                    claimed_confidence=row.get("claimed_confidence"),
                    claimed_base_rate=row.get("claimed_base_rate"),
                )
                self._positions[token_id] = pos
                # Reduce cash by the cost of acquiring this position so
                # the restored bankroll reflects what's actually deployed.
                self._cash -= qty * avg_cost
                restored += 1
            except Exception:
                continue
        return restored


def has_realized_pnl(order: Order) -> bool:
    """Helper — only SELL fills produce realized P&L for now."""
    return order.side == Side.SELL and order.status in (OrderStatus.PARTIAL, OrderStatus.FILLED)
