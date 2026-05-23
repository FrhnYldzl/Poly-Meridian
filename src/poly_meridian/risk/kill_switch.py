"""Kill-switch state machine. See MASTER_SPEC §15.3.

When engaged, RiskPolicy returns REJECT for every signal. Engagement is
triggered by any of:
  - Daily loss exceeds threshold
  - Anomalous slippage (model may be broken)
  - API error rate too high (data may be stale)
  - WS disconnect grace exceeded
  - Manual operator override

Disengagement is **manual only** — operator must explicitly clear.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

log = structlog.get_logger("poly_meridian.risk.kill_switch")


class KillReason(StrEnum):
    DAILY_LOSS = "daily_loss"
    SLIPPAGE_ANOMALY = "slippage_anomaly"
    API_ERROR_RATE = "api_error_rate"
    WS_DISCONNECT = "ws_disconnect"
    MANUAL = "manual"
    WALLET_BALANCE_MISMATCH = "wallet_balance_mismatch"


@dataclass(frozen=True)
class KillSwitchConfig:
    daily_loss_trigger_pct: float = 0.05
    abnormal_slippage_bps: float = 200.0
    api_error_rate_threshold: float = 0.05
    websocket_disconnect_grace_sec: float = 60.0
    manual_override_enabled: bool = True


@dataclass
class KillSwitch:
    """In-memory engagement state. Reload from DB on agent restart."""

    config: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    _engaged: bool = False
    _reason: KillReason | None = None
    _engaged_at: float | None = None
    _detail: dict[str, float | str] = field(default_factory=dict)
    _api_error_count: int = 0
    _api_call_count: int = 0

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> KillReason | None:
        return self._reason

    @property
    def engaged_at(self) -> float | None:
        return self._engaged_at

    @property
    def detail(self) -> dict[str, float | str]:
        return dict(self._detail)

    def _engage(self, reason: KillReason, **detail: float | str) -> None:
        if self._engaged:
            return
        self._engaged = True
        self._reason = reason
        self._engaged_at = time.time()
        self._detail = dict(detail)
        log.error("kill_switch.engaged", reason=str(reason), **detail)

    def disengage(self) -> bool:
        """Manual clear. Returns True if state changed."""
        if not self._engaged:
            return False
        log.warning("kill_switch.disengaged", prior_reason=str(self._reason))
        self._engaged = False
        self._reason = None
        self._engaged_at = None
        self._detail = {}
        return True

    # ---------- triggers ----------

    def observe_daily_pnl(self, daily_pnl_pct: float) -> None:
        if daily_pnl_pct < -self.config.daily_loss_trigger_pct:
            self._engage(
                KillReason.DAILY_LOSS,
                daily_pnl_pct=daily_pnl_pct,
                threshold=-self.config.daily_loss_trigger_pct,
            )

    def observe_slippage(self, observed_bps: float, token_id: str) -> None:
        if observed_bps > self.config.abnormal_slippage_bps:
            self._engage(
                KillReason.SLIPPAGE_ANOMALY,
                observed_bps=observed_bps,
                threshold=self.config.abnormal_slippage_bps,
                token_id=token_id,
            )

    def observe_api_call(self, ok: bool) -> None:
        self._api_call_count += 1
        if not ok:
            self._api_error_count += 1
        if self._api_call_count >= 20:
            rate = self._api_error_count / self._api_call_count
            if rate > self.config.api_error_rate_threshold:
                self._engage(
                    KillReason.API_ERROR_RATE,
                    error_rate=rate,
                    threshold=self.config.api_error_rate_threshold,
                )
            self._api_error_count = 0
            self._api_call_count = 0

    def observe_ws_disconnect(self, disconnected_for_sec: float) -> None:
        if disconnected_for_sec > self.config.websocket_disconnect_grace_sec:
            self._engage(
                KillReason.WS_DISCONNECT,
                disconnected_for_sec=disconnected_for_sec,
                grace=self.config.websocket_disconnect_grace_sec,
            )

    def manual_engage(self, note: str = "") -> None:
        if not self.config.manual_override_enabled:
            log.warning("kill_switch.manual_disabled")
            return
        self._engage(KillReason.MANUAL, note=note)

    def observe_wallet_balance(self, expected_usd: float, actual_usd: float, tol_usd: float = 5.0) -> None:
        if abs(expected_usd - actual_usd) > tol_usd:
            self._engage(
                KillReason.WALLET_BALANCE_MISMATCH,
                expected_usd=expected_usd,
                actual_usd=actual_usd,
                tol_usd=tol_usd,
            )
