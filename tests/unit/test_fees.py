"""Per-category fee schedule."""
from __future__ import annotations

from decimal import Decimal

import pytest

from poly_meridian.execution.fees import FeeSchedule


def test_default_categories_present() -> None:
    s = FeeSchedule.default()
    for cat in ("Crypto", "Politics", "Sports", "Geopolitics", "Uncategorized"):
        assert cat in s.taker_bps_by_category


def test_geopolitics_is_zero_fee() -> None:
    s = FeeSchedule.default()
    assert s.taker_bps("Geopolitics") == 0.0


def test_unknown_category_uses_uncategorized_default() -> None:
    s = FeeSchedule.default()
    assert s.taker_bps("Klingon") == s.taker_bps("Uncategorized")


def test_fee_peaks_at_50c_and_falls_at_edges() -> None:
    s = FeeSchedule.default()
    peak = s.fee_bps_for_price("Crypto", 0.50)
    edge_low = s.fee_bps_for_price("Crypto", 0.01)
    edge_high = s.fee_bps_for_price("Crypto", 0.99)
    assert peak >= edge_low
    assert peak >= edge_high
    # Edges shouldn't fall below 30% of peak (we floor at 0.30).
    assert edge_low >= peak * 0.30 - 1e-6
    assert edge_high >= peak * 0.30 - 1e-6


def test_taker_fee_estimate_usd() -> None:
    s = FeeSchedule.default()
    # $1000 notional, Politics taker = 100bps = 1% → $10.00
    fee = s.estimate_fee_usd(
        notional_usd=Decimal("1000"),
        category="Politics",
        is_maker=False,
        price=0.50,
    )
    assert fee == pytest.approx(Decimal("10.00"), abs=Decimal("0.01"))


def test_maker_fee_is_zero_on_intl() -> None:
    s = FeeSchedule.default()
    fee = s.estimate_fee_usd(
        notional_usd=Decimal("1000"),
        category="Crypto",
        is_maker=True,
        price=0.50,
    )
    assert fee == Decimal("0.0000")


def test_maker_us_pays_rebate_negative() -> None:
    s = FeeSchedule.default()
    fee = s.estimate_fee_usd(
        notional_usd=Decimal("1000"),
        category="Crypto",
        is_maker=True,
        price=0.50,
        us_mode=True,
    )
    # 20bps rebate on $1000 = -$2.00
    assert fee == pytest.approx(Decimal("-2.00"), abs=Decimal("0.01"))


def test_taker_us_flat_30bps() -> None:
    s = FeeSchedule.default()
    fee = s.estimate_fee_usd(
        notional_usd=Decimal("10000"),
        category="Crypto",
        is_maker=False,
        price=0.50,
        us_mode=True,
    )
    # 30bps on $10K = $30
    assert fee == pytest.approx(Decimal("30.00"), abs=Decimal("0.01"))
