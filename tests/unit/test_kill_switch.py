"""Kill-switch state transitions."""
from __future__ import annotations

from poly_meridian.risk.kill_switch import KillReason, KillSwitch, KillSwitchConfig


def test_starts_disengaged() -> None:
    ks = KillSwitch()
    assert ks.engaged is False
    assert ks.reason is None


def test_daily_loss_engages() -> None:
    ks = KillSwitch(config=KillSwitchConfig(daily_loss_trigger_pct=0.05))
    ks.observe_daily_pnl(-0.04)
    assert ks.engaged is False
    ks.observe_daily_pnl(-0.06)
    assert ks.engaged is True
    assert ks.reason == KillReason.DAILY_LOSS


def test_slippage_anomaly_engages() -> None:
    ks = KillSwitch(config=KillSwitchConfig(abnormal_slippage_bps=200))
    ks.observe_slippage(observed_bps=150, token_id="t")
    assert ks.engaged is False
    ks.observe_slippage(observed_bps=300, token_id="t")
    assert ks.engaged is True
    assert ks.reason == KillReason.SLIPPAGE_ANOMALY


def test_api_error_rate_engages_only_after_window() -> None:
    ks = KillSwitch(config=KillSwitchConfig(api_error_rate_threshold=0.10))
    # First 10 calls with 50% errors — not yet enough samples (<20).
    for _ in range(10):
        ks.observe_api_call(ok=False)
    assert ks.engaged is False
    # Cross 20-call window with high error rate.
    for _ in range(15):
        ks.observe_api_call(ok=False)
    assert ks.engaged is True
    assert ks.reason == KillReason.API_ERROR_RATE


def test_ws_disconnect_engages_after_grace() -> None:
    ks = KillSwitch(config=KillSwitchConfig(websocket_disconnect_grace_sec=60))
    ks.observe_ws_disconnect(disconnected_for_sec=30)
    assert ks.engaged is False
    ks.observe_ws_disconnect(disconnected_for_sec=120)
    assert ks.engaged is True
    assert ks.reason == KillReason.WS_DISCONNECT


def test_manual_engage_and_disengage() -> None:
    ks = KillSwitch()
    ks.manual_engage("operator stopped trading")
    assert ks.engaged is True
    assert ks.reason == KillReason.MANUAL
    changed = ks.disengage()
    assert changed is True
    assert ks.engaged is False


def test_manual_disabled_does_not_engage() -> None:
    ks = KillSwitch(config=KillSwitchConfig(manual_override_enabled=False))
    ks.manual_engage("attempt")
    assert ks.engaged is False


def test_engage_is_idempotent() -> None:
    ks = KillSwitch()
    ks.observe_daily_pnl(-0.10)
    first_reason = ks.reason
    # Another trigger should NOT overwrite the first reason.
    ks.observe_slippage(observed_bps=1000, token_id="t")
    assert ks.reason == first_reason
