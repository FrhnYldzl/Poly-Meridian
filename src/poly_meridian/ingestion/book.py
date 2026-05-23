"""Local order book replica. Built from `book` snapshots + `price_change`
incrementals on the CLOB WebSocket. Pure logic — no I/O. See §11.3."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class LocalBook:
    """Two sorted dicts: bids descending by price, asks ascending by price."""

    token_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Replace local state from a `book` message.

        Payload shape (per Polymarket WS docs):
          {
            "event_type": "book",
            "asset_id": "...",
            "bids": [{"price": "0.42", "size": "120"}, ...],
            "asks": [{"price": "0.43", "size":  "80"}, ...]
          }
        """
        self.bids.clear()
        self.asks.clear()
        for lvl in snapshot.get("bids", []):
            price = Decimal(str(lvl["price"]))
            size = Decimal(str(lvl["size"]))
            if size > 0:
                self.bids[price] = size
        for lvl in snapshot.get("asks", []):
            price = Decimal(str(lvl["price"]))
            size = Decimal(str(lvl["size"]))
            if size > 0:
                self.asks[price] = size

    def apply_price_change(self, update: dict[str, Any]) -> None:
        """Apply an incremental update. Size 0 removes the level.

        Payload shape:
          {
            "event_type": "price_change",
            "asset_id": "...",
            "changes": [
              {"price": "0.42", "side": "BUY",  "size": "100"},
              {"price": "0.43", "side": "SELL", "size": "0"}
            ]
          }
        """
        for change in update.get("changes", []):
            price = Decimal(str(change["price"]))
            size = Decimal(str(change["size"]))
            side = change["side"].upper()
            target = self.bids if side in ("BUY", "BID") else self.asks
            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size

    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return (p, self.bids[p])

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return (p, self.asks[p])

    def mid(self) -> Decimal | None:
        b = self.best_bid()
        a = self.best_ask()
        if b is None or a is None:
            return None
        return (b[0] + a[0]) / Decimal(2)

    def depth_within(self, side: str, pct_from_mid: Decimal) -> Decimal:
        """Sum size on `side` ('bid'/'ask') within `pct_from_mid` of mid."""
        m = self.mid()
        if m is None:
            return Decimal(0)
        if side.lower().startswith("b"):
            cutoff = m * (Decimal(1) - pct_from_mid)
            return sum(
                (sz for p, sz in self.bids.items() if p >= cutoff),
                start=Decimal(0),
            )
        cutoff = m * (Decimal(1) + pct_from_mid)
        return sum(
            (sz for p, sz in self.asks.items() if p <= cutoff),
            start=Decimal(0),
        )
