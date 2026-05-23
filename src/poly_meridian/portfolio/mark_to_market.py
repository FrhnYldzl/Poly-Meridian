"""Mark-to-market — refresh `last_mark` on every open position. §17."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from poly_meridian.ingestion.book import LocalBook
from poly_meridian.portfolio.ledger import Ledger


def mark_all(
    ledger: Ledger,
    books: dict[str, LocalBook],
    *,
    ts: datetime | None = None,
) -> None:
    """Refresh marks for every open position whose token has a live book."""
    for pos in ledger.positions():
        book = books.get(pos.token_id)
        if book is None:
            continue
        mid = book.mid()
        if mid is None:
            continue
        ledger.mark(pos.token_id, Decimal(str(mid)), ts=ts)
