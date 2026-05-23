"""Phase 0 smoke tests — confirm scaffold imports and ABC contracts exist."""
from __future__ import annotations

from poly_meridian import __version__
from poly_meridian.domain import Action, Mode, OrderType, Side
from poly_meridian.execution import Executor
from poly_meridian.ingestion import IngestionSource
from poly_meridian.risk import RiskDecision, RiskPolicy
from poly_meridian.strategies import BaseStrategy


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_default_mode_is_paper() -> None:
    from poly_meridian.settings import get_settings

    s = get_settings()
    assert s.mode in (Mode.PAPER, Mode.LIVE_CONSERVATIVE, Mode.LIVE_NORMAL, Mode.KILL)


def test_abcs_are_abstract() -> None:
    # Must raise — these are contracts, not concrete classes.
    import pytest

    for cls in (BaseStrategy, RiskPolicy, Executor, IngestionSource):
        with pytest.raises(TypeError):
            cls()  # type: ignore[abstract,call-arg]


def test_enums_complete() -> None:
    assert set(Side) == {Side.BUY, Side.SELL}
    assert set(OrderType) == {OrderType.GTC, OrderType.GTD, OrderType.FOK, OrderType.FAK}
    assert set(Action) >= {Action.BUY_YES, Action.BUY_NO, Action.SELL, Action.HOLD, Action.EXIT}
    assert RiskDecision.APPROVE != RiskDecision.REJECT
