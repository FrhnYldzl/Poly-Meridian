"""Normalization: Gamma raw → typed Market; book payload → OrderBook."""
from __future__ import annotations

from poly_meridian.ingestion.normalize import (
    book_snapshot_to_domain,
    gamma_market_to_domain,
    gamma_market_to_row,
)


def test_gamma_market_with_direct_token_ids() -> None:
    raw = {
        "conditionId": "0xabc",
        "question": "Will X happen by Y?",
        "category": "Politics",
        "yesTokenId": "1111",
        "noTokenId": "2222",
        "active": True,
        "closed": False,
    }
    m = gamma_market_to_domain(raw)
    assert m is not None
    assert m.condition_id == "0xabc"
    assert m.yes_token_id == "1111"
    assert m.no_token_id == "2222"


def test_gamma_market_with_clob_token_ids_json_string() -> None:
    raw = {
        "conditionId": "0xdef",
        "question": "q",
        "clobTokenIds": '["7777","8888"]',
    }
    m = gamma_market_to_domain(raw)
    assert m is not None
    assert m.yes_token_id == "7777"
    assert m.no_token_id == "8888"


def test_gamma_market_missing_required_returns_none() -> None:
    assert gamma_market_to_domain({"question": "missing condition"}) is None
    assert gamma_market_to_domain({"conditionId": "x", "question": "q"}) is None


def test_gamma_market_to_row_has_updated_at() -> None:
    raw = {
        "conditionId": "x",
        "question": "q",
        "yesTokenId": "1",
        "noTokenId": "2",
    }
    row = gamma_market_to_row(raw)
    assert row is not None
    assert row["condition_id"] == "x"
    assert row["updated_at"] is not None


def test_book_snapshot_to_domain_sorts_correctly() -> None:
    payload = {
        "asset_id": "tid",
        "bids": [
            {"price": "0.40", "size": "1"},
            {"price": "0.42", "size": "1"},
            {"price": "0.41", "size": "0"},  # filtered
        ],
        "asks": [
            {"price": "0.44", "size": "1"},
            {"price": "0.43", "size": "1"},
        ],
    }
    ob = book_snapshot_to_domain(payload)
    assert ob is not None
    assert ob.token_id == "tid"
    # bids descending
    assert [float(b.price) for b in ob.bids] == [0.42, 0.40]
    # asks ascending
    assert [float(a.price) for a in ob.asks] == [0.43, 0.44]
