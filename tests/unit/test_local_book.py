"""LocalBook reconstruction — pure-compute correctness tests."""
from __future__ import annotations

from decimal import Decimal

from poly_meridian.ingestion.book import LocalBook


def test_apply_snapshot_populates_levels() -> None:
    book = LocalBook(token_id="tid")
    book.apply_snapshot({
        "bids": [{"price": "0.42", "size": "100"}, {"price": "0.41", "size": "200"}],
        "asks": [{"price": "0.43", "size": "150"}, {"price": "0.44", "size": "300"}],
    })
    assert book.best_bid() == (Decimal("0.42"), Decimal("100"))
    assert book.best_ask() == (Decimal("0.43"), Decimal("150"))
    assert book.mid() == Decimal("0.425")


def test_zero_size_removes_level() -> None:
    book = LocalBook(token_id="tid")
    book.apply_snapshot({
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.51", "size": "10"}],
    })
    book.apply_price_change({
        "changes": [
            {"price": "0.50", "side": "BUY", "size": "0"},
            {"price": "0.49", "side": "BUY", "size": "5"},
        ]
    })
    assert Decimal("0.50") not in book.bids
    assert book.bids[Decimal("0.49")] == Decimal("5")


def test_snapshot_replaces_state() -> None:
    book = LocalBook(token_id="tid")
    book.apply_snapshot({"bids": [{"price": "0.10", "size": "1"}], "asks": []})
    book.apply_snapshot({"bids": [{"price": "0.90", "size": "5"}], "asks": [{"price": "0.91", "size": "2"}]})
    assert Decimal("0.10") not in book.bids
    assert book.best_bid() == (Decimal("0.90"), Decimal("5"))


def test_depth_within_respects_cutoff() -> None:
    book = LocalBook(token_id="tid")
    book.apply_snapshot({
        "bids": [
            {"price": "0.50", "size": "10"},   # at mid
            {"price": "0.49", "size": "20"},   # ~2% below mid
            {"price": "0.40", "size": "100"},  # 20% below mid
        ],
        "asks": [
            {"price": "0.50", "size": "10"},
            {"price": "0.51", "size": "20"},
            {"price": "0.60", "size": "100"},
        ],
    })
    bid_depth_5pct = book.depth_within("bid", Decimal("0.05"))
    assert bid_depth_5pct == Decimal("30")
    ask_depth_5pct = book.depth_within("ask", Decimal("0.05"))
    assert ask_depth_5pct == Decimal("30")


def test_empty_book_returns_none() -> None:
    book = LocalBook(token_id="tid")
    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.mid() is None
