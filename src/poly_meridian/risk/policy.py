"""RiskPolicy — ABC + concrete `DefaultRiskPolicy`. MASTER_SPEC §15.4.

Every aggregated signal MUST pass through `RiskPolicy.evaluate()` before
reaching the executor. There is no bypass — this is enforced by main loop
wiring, not by convention.

Design contract:
  - Strategies propose `size_pct` + `proposed_price` on AggregatedSignal.
  - Policy validates limits and may REDUCE the size, but NEVER inflates it.
  - Kelly sizing is a *tool* strategies use to set their proposal;
    `risk/kelly.py::sized_kelly` is exported for that purpose.
  - `policy.size()` converts an APPROVE/REDUCE decision into a TradeDecision.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

import structlog

from poly_meridian.domain import (
    Action,
    AggregatedSignal,
    OrderType,
    PortfolioSnapshot,
    Side,
    TradeDecision,
)
from poly_meridian.risk.kill_switch import KillSwitch
from poly_meridian.risk.limits import (
    RiskLimits,
    check_category_exposure,
    check_daily_loss,
    check_market_liquidity,
    check_open_position_count,
    check_position_size_cap,
    check_same_condition,
    check_same_event,
    check_total_exposure,
    reduce_size_if_breached,
)

log = structlog.get_logger("poly_meridian.risk.policy")


class RiskDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REDUCE = "reduce"


class RiskPolicy(ABC):
    """Contract for the risk gate between aggregator and executor."""

    @abstractmethod
    def evaluate(
        self,
        signal: AggregatedSignal,
        portfolio: PortfolioSnapshot,
    ) -> RiskDecision:
        """Decide whether to approve, reject, or reduce the signal."""

    @abstractmethod
    def size(
        self,
        signal: AggregatedSignal,
        portfolio: PortfolioSnapshot,
    ) -> TradeDecision | None:
        """Translate an APPROVE/REDUCE decision into a sized TradeDecision."""

    @abstractmethod
    def is_kill_switch_engaged(self) -> bool:
        """Return True if the kill-switch blocks all new orders."""


class DefaultRiskPolicy(RiskPolicy):
    """Phase 2 reference implementation:

    1. Observe portfolio daily P&L → may engage kill-switch
    2. If kill-switch engaged → REJECT
    3. Run blocking checks (liquidity, daily loss, open count) → REJECT
    4. Run sizing checks (position cap, total expo, category expo) →
       REDUCE if positive headroom, REJECT if zero
    5. APPROVE otherwise
    """

    def __init__(
        self,
        *,
        strategy_name: str,
        limits: RiskLimits | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.strategy_name = strategy_name
        self.limits = limits or RiskLimits()
        self.kill_switch = kill_switch or KillSwitch()
        # condition_id -> reduced size_pct, set during evaluate(), consumed by size()
        self._reduced_size_pct: dict[str, float] = {}
        # Phase N.8: slippage drift reactive cap. When the SlippageMonitor's
        # drift_bps exceeds this threshold, we automatically halve every
        # strategy's size_pct via a multiplier applied in size(). Operator
        # can flip drift_size_multiplier = 1.0 to disable.
        self._slippage_drift_bps: float | None = None
        self.drift_alarm_bps = 200.0     # > 200 bps deviation → react
        self.drift_size_multiplier = 0.5  # halve sizing while drift is bad
        # Phase Q.3 — token → condition_id + token → event_id maps so the
        # concentration checks can resolve a portfolio position back to
        # its Polymarket condition / event. Populated by Pipeline as it
        # learns about markets (register_market on each WS re-sub).
        self._token_to_condition: dict[str, str] = {}
        self._token_to_event: dict[str, str] = {}
        # signal.condition_id → event_id, for the same-event check.
        self._condition_to_event: dict[str, str] = {}

    def register_market(
        self,
        condition_id: str,
        yes_token: str,
        no_token: str,
        event_id: str | None = None,
    ) -> None:
        """Pipeline registers each market it sees so the concentration
        checks can map a position's token_id back to its condition / event."""
        self._token_to_condition[yes_token] = condition_id
        self._token_to_condition[no_token] = condition_id
        if event_id:
            self._token_to_event[yes_token] = event_id
            self._token_to_event[no_token] = event_id
            self._condition_to_event[condition_id] = event_id

    def update_slippage_drift(self, drift_bps: float | None) -> None:
        """Called by main.py's broker_refresh_loop with the latest measured
        drift_bps from SlippageMonitor. None = no data yet."""
        self._slippage_drift_bps = drift_bps

    def is_kill_switch_engaged(self) -> bool:
        return self.kill_switch.engaged

    def evaluate(
        self,
        signal: AggregatedSignal,
        portfolio: PortfolioSnapshot,
    ) -> RiskDecision:
        self.kill_switch.observe_daily_pnl(portfolio.daily_pnl_pct)

        if self.kill_switch.engaged:
            return self._reject(
                signal, "kill_switch_engaged", reason=str(self.kill_switch.reason)
            )

        if signal.direction not in (Action.BUY_YES, Action.BUY_NO):
            # Strategy-emitted SELL signals (Phase N.3 introduces these via
            # the dedicated ExitMonitor path, which BYPASSES this policy).
            # Anything that lands here via a strategy is by definition NOT
            # an exit — it's a strategy emitting a SELL it shouldn't be.
            # Keep the gate closed for strategies.
            return self._reject(
                signal, "non_buy_direction", direction=str(signal.direction)
            )

        if signal.proposed_price is None or signal.proposed_price <= 0:
            return self._reject(signal, "missing_or_invalid_proposed_price")

        # Hard blocks
        for reason in (
            check_daily_loss(portfolio, self.limits),
            check_open_position_count(portfolio, self.limits),
            check_market_liquidity(signal, self.limits),
            # Phase Q.3 — block adding to existing position on the same
            # condition (incl. opposite leg, which would be self-hedge).
            check_same_condition(
                signal, portfolio, self.limits, self._token_to_condition,
            ),
            # Phase Q.3 — cap positions per parent event (related markets).
            check_same_event(
                signal, portfolio, self.limits, self._token_to_event,
                self._condition_to_event.get(signal.condition_id),
            ),
        ):
            if reason:
                return self._reject(signal, reason)

        # Position-size cap: hard reject if WAY over (>1.5x the cap),
        # reduce otherwise. Lets strategies be slightly aggressive without
        # immediately rejecting marginal proposals.
        size_reason = check_position_size_cap(signal, self.limits)
        if size_reason and signal.size_pct > self.limits.max_position_pct_of_bankroll * 1.5:
            return self._reject(signal, size_reason)

        # Reducible breaches
        for reason in (
            check_total_exposure(signal, portfolio, self.limits),
            check_category_exposure(signal, portfolio, self.limits),
        ):
            if reason:
                reduced = reduce_size_if_breached(signal, portfolio, self.limits)
                if reduced is None or reduced <= 0:
                    return self._reject(signal, reason)
                self._reduced_size_pct[signal.condition_id] = reduced
                log.info(
                    "risk.reduce",
                    strategy=self.strategy_name,
                    condition_id=signal.condition_id,
                    original=signal.size_pct,
                    reduced=reduced,
                    cause=reason,
                )
                return RiskDecision.REDUCE

        if size_reason:
            self._reduced_size_pct[signal.condition_id] = (
                self.limits.max_position_pct_of_bankroll
            )
            log.info(
                "risk.reduce.size_cap",
                strategy=self.strategy_name,
                condition_id=signal.condition_id,
                cap=self.limits.max_position_pct_of_bankroll,
            )
            return RiskDecision.REDUCE

        return RiskDecision.APPROVE

    def size(
        self,
        signal: AggregatedSignal,
        portfolio: PortfolioSnapshot,
    ) -> TradeDecision | None:
        """Convert an APPROVE/REDUCE signal into a TradeDecision.

        Does NOT recompute Kelly — the strategy was responsible for setting
        `size_pct` and `proposed_price`. We just enforce the cap.
        """
        if signal.proposed_price is None or signal.proposed_price <= 0:
            return None

        size_pct = self._reduced_size_pct.pop(signal.condition_id, signal.size_pct)
        size_pct = min(size_pct, self.limits.max_position_pct_of_bankroll)
        # Phase N.8: if slippage is materially worse than our model predicts,
        # halve sizing automatically. Prevents amplifying losses during
        # adverse book regimes.
        if (
            self._slippage_drift_bps is not None
            and self._slippage_drift_bps > self.drift_alarm_bps
        ):
            size_pct = size_pct * self.drift_size_multiplier
            log.warning(
                "risk.slippage_drift_throttle",
                drift_bps=self._slippage_drift_bps,
                multiplier=self.drift_size_multiplier,
                reduced_size_pct=size_pct,
            )
        if size_pct <= 0:
            return None

        size_usd = (portfolio.nav_usd * Decimal(str(size_pct))).quantize(Decimal("0.01"))
        if size_usd <= Decimal("0.01"):
            return None

        price = signal.proposed_price
        size_units = (size_usd / price).quantize(Decimal("0.01"))
        if size_units <= 0:
            return None

        # Phase N.4 — pass arb partner-leg through so the router can submit
        # both legs concurrently. Side mapping: BUY_YES→Side.BUY on YES,
        # BUY_NO→Side.BUY on NO (Polymarket has no SELL-to-open, both legs
        # are BUY-side bets on complementary outcomes).
        paired_side: Side | None = None
        if signal.paired_side is not None:
            # Whatever the partner action is, we're BUYING the partner token.
            paired_side = Side.BUY

        return TradeDecision(
            ts=signal.ts,
            strategy=self.strategy_name,
            token_id=signal.token_id,
            side=Side.BUY,
            order_type=OrderType.GTC,
            price=price,
            size=size_units,
            paired_token=signal.paired_token,
            paired_price=signal.paired_price,
            paired_side=paired_side,
        )

    def _reject(self, signal: AggregatedSignal, reason: str, **kw: object) -> RiskDecision:
        log.warning(
            "risk.reject",
            strategy=self.strategy_name,
            condition_id=signal.condition_id,
            reason=reason,
            **kw,
        )
        return RiskDecision.REJECT
