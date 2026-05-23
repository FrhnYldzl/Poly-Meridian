"""Kelly sizing math. Pure compute."""
from __future__ import annotations

from decimal import Decimal

import pytest

from poly_meridian.risk.kelly import kelly_fraction, sized_kelly


def test_kelly_zero_when_no_edge() -> None:
    assert kelly_fraction(p=0.50, market_price=0.50) == 0.0
    assert kelly_fraction(p=0.40, market_price=0.50) == 0.0  # negative edge → 0


def test_kelly_positive_when_edge_positive() -> None:
    # p=0.70, market=0.40: b = 0.60/0.40 = 1.5; q = 0.30
    # f* = (1.5*0.70 - 0.30) / 1.5 = (1.05 - 0.30) / 1.5 = 0.5
    f = kelly_fraction(p=0.70, market_price=0.40)
    assert f == pytest.approx(0.5, abs=1e-9)


def test_kelly_rejects_invalid_inputs() -> None:
    assert kelly_fraction(p=1.1, market_price=0.5) == 0.0
    assert kelly_fraction(p=-0.1, market_price=0.5) == 0.0
    assert kelly_fraction(p=0.5, market_price=0.0) == 0.0
    assert kelly_fraction(p=0.5, market_price=1.0) == 0.0


def test_quarter_kelly_default_caps_at_5pct() -> None:
    # Same example as above: full Kelly = 0.5, quarter = 0.125. But hard cap = 0.05.
    res = sized_kelly(
        p=0.70,
        market_price=0.40,
        bankroll_usd=Decimal("100000"),
        kelly_fraction_multiplier=0.25,
        hard_cap_pct=0.05,
    )
    assert res.f_star == pytest.approx(0.5)
    assert res.f_used == pytest.approx(0.05)  # capped, not 0.125
    assert res.size_usd == Decimal("5000.00")


def test_quarter_kelly_no_edge_returns_zero() -> None:
    res = sized_kelly(
        p=0.30,
        market_price=0.40,
        bankroll_usd=Decimal("100000"),
    )
    assert res.f_star == 0.0
    assert res.size_usd == Decimal("0.00")


def test_edge_and_ev_in_result() -> None:
    res = sized_kelly(
        p=0.70,
        market_price=0.40,
        bankroll_usd=Decimal("10000"),
    )
    assert res.edge == pytest.approx(0.30)
    # EV per $1 = p*(1-mkt) - q*mkt = 0.7*0.6 - 0.3*0.4 = 0.42 - 0.12 = 0.30
    assert res.expected_value == pytest.approx(0.30)
