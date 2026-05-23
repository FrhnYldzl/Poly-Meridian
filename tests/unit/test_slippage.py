"""Slippage estimation + book walking."""
from __future__ import annotations

from decimal import Decimal

from poly_meridian.execution.slippage_model import estimate_slippage_bps, walk_book_for_fill
from poly_meridian.ingestion.book import LocalBook


def _book() -> LocalBook:
    b = LocalBook(token_id="t")
    b.apply_snapshot({
        "bids": [{"price": "0.39", "size": "100"}],
        "asks": [
            {"price": "0.40", "size": "100"},
            {"price": "0.41", "size": "100"},
            {"price": "0.45", "size": "100"},
        ],
    })
    return b


def test_walk_book_partial_then_full() -> None:
    book = _book()
    vwap, filled = walk_book_for_fill(book, "BUY", Decimal("150"))
    assert filled == Decimal("150")
    # 100 @ 0.40 + 50 @ 0.41 = 40 + 20.5 = 60.5 / 150 = 0.4033
    assert vwap == Decimal("0.4033")


def test_walk_book_drains_completely() -> None:
    book = _book()
    vwap, filled = walk_book_for_fill(book, "BUY", Decimal("500"))
    assert filled == Decimal("300")   # only 300 units of ask depth
    # 100 @ 0.40 + 100 @ 0.41 + 100 @ 0.45 = 40 + 41 + 45 = 126 / 300 = 0.4200
    assert vwap == Decimal("0.4200")


def test_walk_empty_book_returns_none() -> None:
    book = LocalBook(token_id="t")
    vwap, filled = walk_book_for_fill(book, "BUY", Decimal("10"))
    assert vwap is None
    assert filled == 0


def test_slippage_inf_on_no_depth() -> None:
    book = LocalBook(token_id="t")
    # Need a mid for depth_within to compute — leave book empty so depth=0
    res = estimate_slippage_bps(book=book, side="BUY", size=Decimal("10"))
    assert res == float("inf")
