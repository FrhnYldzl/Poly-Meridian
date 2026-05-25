"""Live slippage drift monitor — closes the loop on `fit_from_fills`.

`slippage_model.fit_from_fills` has shipped since Phase 2 but nothing
ever called it. Audit's #1 live-trading blocker: we estimate slippage
on every order but never measure realized vs predicted → silent drift
when book dynamics change.

This module owns the feedback loop:
  1. PaperExecutor (and later LiveExecutor) calls `monitor.record_fill(...)`
     after each fill with size, depth, expected_price, realized_vwap.
  2. The buffer is a bounded ring; oldest evictions don't hurt the fit.
  3. `latest_fit()` re-runs the regression on the current buffer.
  4. `drift_bps()` compares observed slippage against the *currently
     configured* model (a=50, b=1.2 by default) so we alert when the
     model is materially off.

The async loop in main.py samples + alerts every N minutes.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from poly_meridian.execution.slippage_model import (
    SlippageFit,
    fit_from_fills,
    slippage_from_fill,
)

log = structlog.get_logger("poly_meridian.execution.slippage_monitor")

# Default model coefficients — must match PaperExecutor/RiskPolicy's call to
# `estimate_slippage_bps`. Used as the "predicted" baseline for drift.
DEFAULT_A = 50.0
DEFAULT_B = 1.2


@dataclass(frozen=True)
class SlippageObservation:
    ts: datetime
    token_id: str
    size: float           # number of outcome tokens filled
    depth: float          # book depth within 5% of mid at fill time
    expected_price: float
    realized_vwap: float
    slippage_bps: float


class SlippageMonitor:
    def __init__(self, *, buffer_size: int = 500) -> None:
        self._obs: deque[SlippageObservation] = deque(maxlen=buffer_size)

    def record_fill(
        self,
        *,
        token_id: str,
        size: float,
        depth: float,
        expected_price: float,
        realized_vwap: float,
    ) -> None:
        """Add one fill observation. Cheap — pure append, no fit yet."""
        if size <= 0 or expected_price <= 0 or realized_vwap <= 0:
            return
        slippage = slippage_from_fill(
            expected_price=expected_price,
            realized_vwap=realized_vwap,
        )
        obs = SlippageObservation(
            ts=datetime.now(UTC),
            token_id=token_id,
            size=size,
            depth=max(0.0, depth),
            expected_price=expected_price,
            realized_vwap=realized_vwap,
            slippage_bps=slippage,
        )
        self._obs.append(obs)

    def n_observations(self) -> int:
        return len(self._obs)

    def latest_fit(self) -> SlippageFit | None:
        """Re-fit the log-log regression on all currently buffered obs.
        Returns None until we have ≥10 usable samples."""
        payload = [
            {"size": o.size, "depth": o.depth, "slippage_bps": o.slippage_bps}
            for o in self._obs
        ]
        return fit_from_fills(payload)

    def drift_bps(self) -> float | None:
        """RMSE between (default model predicted slippage) and (observed
        slippage) over the buffer. Returns None when < 10 obs.

        Default model: 50 * (size/depth)^1.2. If realized slippage is
        systematically larger than this prediction, drift_bps grows and
        the operator should retune.
        """
        usable = [o for o in self._obs if o.depth > 0 and o.size > 0 and o.slippage_bps > 0]
        if len(usable) < 10:
            return None
        sq = 0.0
        for o in usable:
            predicted = DEFAULT_A * math.pow(o.size / o.depth, DEFAULT_B)
            sq += (predicted - o.slippage_bps) ** 2
        return math.sqrt(sq / len(usable))

    def summary(self) -> dict[str, Any]:
        """Operator-facing snapshot — used by /api/state."""
        fit = self.latest_fit()
        usable = [o for o in self._obs if o.slippage_bps > 0]
        avg_realized_bps = (
            sum(o.slippage_bps for o in usable) / len(usable) if usable else 0.0
        )
        return {
            "n_observations": len(self._obs),
            "avg_realized_slippage_bps": avg_realized_bps,
            "drift_bps": self.drift_bps(),
            "fitted_a": fit.a if fit else None,
            "fitted_b": fit.b if fit else None,
            "fit_rmse_bps": fit.rmse_bps if fit else None,
            "default_a": DEFAULT_A,
            "default_b": DEFAULT_B,
        }
