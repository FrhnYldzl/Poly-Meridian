"""Order-book feature math — small synthetic books with known outputs."""
from __future__ import annotations

from decimal import Decimal

import pytest

from poly_meridian.features import orderbook_features as ob
from poly_meridian.ingestion.book import LocalBook


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> LocalBook:
    b = LocalBook(token_id="t")
    b.apply_snapshot({
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    })
    return b


def test_mid_and_spread() -> None:
    book = _book([("0.40", "1")], [("0.42", "1")])
    assert ob.mid_price(book) == pytest.approx(0.41)
    assert ob.spread(book) == pytest.approx(0.02)


def test_microprice_size_weighted() -> None:
    # bid: $0.40 / 100, ask: $0.50 / 100 → microprice = 0.45
    book = _book([("0.40", "100")], [("0.50", "100")])
    assert ob.microprice(book) == pytest.approx(0.45)

    # Heavier ask size → microprice tilts UP toward ask side
    # bid: 0.40 / 100, ask: 0.50 / 300 → microprice =
    #   (0.40*300 + 0.50*100) / 400 = (120 + 50) / 400 = 0.425
    book = _book([("0.40", "100")], [("0.50", "300")])
    assert ob.microprice(book) == pytest.approx(0.425)


def test_depth_imbalance_signs() -> None:
    # All depth on the bid → imbalance == +1
    book = _book([("0.50", "100")], [("0.50001", "0.0001")])
    di = ob.depth_imbalance(book, Decimal("0.05"))
    assert di is not None and di > 0.9

    # All depth on the ask → imbalance close to -1
    book = _book([("0.49999", "0.0001")], [("0.50", "100")])
    di = ob.depth_imbalance(book, Decimal("0.05"))
    assert di is not None and di < -0.9


def test_features_handle_empty_book() -> None:
    book = LocalBook(token_id="t")
    assert ob.mid_price(book) is None
    assert ob.spread(book) is None
    assert ob.microprice(book) is None
    assert ob.depth_imbalance(book) is None
