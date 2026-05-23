"""Feature registry — end-to-end compute on a synthetic book + time inputs."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from poly_meridian.features import CATALOG, compute_features
from poly_meridian.ingestion.book import LocalBook


def _filled_book() -> LocalBook:
    b = LocalBook(token_id="t")
    b.apply_snapshot({
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "100"}],
    })
    return b


def test_compute_populates_book_features() -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    feats = compute_features(token_id="t", now=now, book=_filled_book())
    for key in ("mid_price", "spread", "microprice"):
        assert key in feats.values, f"missing {key}"
    assert feats.values["mid_price"] == 0.41


def test_compute_populates_time_features() -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    end = now + timedelta(hours=24)
    feats = compute_features(token_id="t", now=now, end_date=end)
    assert feats.values["time_to_resolution_hours"] == 24.0
    assert 0.0 <= feats.values["time_decay_factor"] <= 1.0


def test_catalog_keys_are_unique_and_match_computers() -> None:
    from poly_meridian.features.registry import COMPUTERS

    assert set(CATALOG.keys()) == set(COMPUTERS.keys())
    assert len(CATALOG) >= 8
