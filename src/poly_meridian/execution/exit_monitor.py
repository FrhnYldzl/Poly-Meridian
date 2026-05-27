"""Position exit monitor — the missing half of the trade lifecycle.

Audit BUG #6: the agent could only OPEN positions (BUY signals). Every
SELL/EXIT/HOLD aggregated signal hit `_reject("non_buy_direction", ...)`
in `risk/policy.py`, so winning trades never got realized and losers
ran all the way to resolution. Without exit logic, the Sharpe ratio is
not even meaningful — the agent's "return" is whatever resolution
delivers, dominated by luck of resolution rather than trading skill.

ExitMonitor is a separate periodic scanner — NOT a strategy. It walks
each open position every N seconds and emits SELL orders directly to
the executor when any of three triggers fire:

  1. PROFIT-TAKE — MTM PnL% >= profit_take_pct (default +20%)
  2. STOP-LOSS — MTM PnL% <= -stop_loss_pct (default -30%)
  3. TIME-DECAY — hours_to_resolution < safety_hours (default 6h)

The route bypasses the aggregator (no voting needed — single source)
and the risk policy's "non-buy → reject" gate (exits are risk-REDUCING
so blocking them would defeat the purpose). We still respect the
kill-switch — when engaged, exits halt too (operator may want manual
control). The L.1 flatten path handles the "close everything NOW" case.

Each exit produces a signal+order pair that's pushed to the broker so
the dashboard shows the exit reason + trade context. Ledger update
happens via the existing on_fill plumbing.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import (
    Mode,
    Order,
    OrderType,
    Side,
    TradeDecision,
)
from poly_meridian.execution.base import Executor
from poly_meridian.ingestion.normalize import gamma_market_to_domain
from poly_meridian.portfolio.ledger import Ledger, PositionState

log = structlog.get_logger("poly_meridian.execution.exit_monitor")


class ExitMonitor:
    def __init__(
        self,
        *,
        ledger: Ledger,
        executor: Executor,
        broker: Any | None = None,
        market_cache: dict[str, Any] | None = None,
        kill_switch: Any | None = None,   # so we halt when engaged
        profit_take_pct: float = 0.20,    # close when MTM ≥ +20% of cost
        stop_loss_pct: float = 0.30,      # close when MTM ≤ -30% of cost
        time_decay_safety_hours: float = 6.0,
        # Q.6a: was 25.0 — silently masked all exits when NAV was small
        # enough that no single position cleared $25. Default sized for
        # paper-mode $250 NAV reality. Caller can raise it in production.
        min_position_notional_usd: float = 1.0,
        scan_interval_sec: int = 10,
    ) -> None:
        self.ledger = ledger
        self.executor = executor
        self.broker = broker
        self.market_cache = market_cache or {"markets": []}
        self.kill_switch = kill_switch
        self.profit_take_pct = float(profit_take_pct)
        self.stop_loss_pct = float(stop_loss_pct)
        self.time_decay_safety_hours = float(time_decay_safety_hours)
        self.min_position_notional_usd = float(min_position_notional_usd)
        self.scan_interval_sec = int(scan_interval_sec)
        # Per-position cooldown — once we emit an exit, suppress further
        # exits on the same token for this many seconds (executor takes
        # time to fill; we don't want spam).
        self._cooldown_until: dict[str, datetime] = {}
        self._cooldown_sec = 30.0

    # ---------------- public API ----------------

    async def scan_once(self) -> int:
        """Walk open positions, emit exits where triggered. Returns count."""
        # Respect the kill-switch — when engaged, the operator is in control;
        # auto-exits would surprise them and may close at bad prices.
        if self.kill_switch is not None and getattr(self.kill_switch, "engaged", False):
            return 0
        n_exits = 0
        # Build a token → end_date lookup once per scan.
        tok_to_end: dict[str, datetime] = {}
        for raw_m in self.market_cache.get("markets", []) or []:
            m = gamma_market_to_domain(raw_m)
            if m is None or m.end_date_iso is None:
                continue
            tok_to_end[m.yes_token_id] = m.end_date_iso
            tok_to_end[m.no_token_id] = m.end_date_iso

        now = datetime.now(UTC)
        for pos in self.ledger.positions():
            # Skip tiny positions (rounding dust).
            notional = float(abs(pos.qty)) * float(pos.last_mark or 0)
            if notional < self.min_position_notional_usd:
                continue
            # Per-position cooldown.
            cd = self._cooldown_until.get(pos.token_id)
            if cd is not None and cd > now:
                continue

            reason, mtm_pct = self._decide(pos, tok_to_end.get(pos.token_id), now)
            if reason is None:
                continue

            try:
                await self._emit_exit(pos, reason, mtm_pct)
                n_exits += 1
                self._cooldown_until[pos.token_id] = datetime.fromtimestamp(
                    now.timestamp() + self._cooldown_sec, tz=UTC,
                )
            except Exception as exc:
                log.warning("exit.emit_failed", token_id=pos.token_id, error=str(exc)[:200])

        return n_exits

    async def run_loop(self, stop: asyncio.Event) -> None:
        """Periodic scanner. Cancelled cleanly via the stop event."""
        log.info(
            "exit_monitor.start",
            profit_take_pct=self.profit_take_pct,
            stop_loss_pct=self.stop_loss_pct,
            time_decay_hours=self.time_decay_safety_hours,
            scan_interval_sec=self.scan_interval_sec,
        )
        while not stop.is_set():
            try:
                n = await self.scan_once()
                if n > 0:
                    log.info("exit_monitor.cycle", n_exits=n)
            except Exception as exc:
                log.warning("exit_monitor.cycle_error", error=str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.scan_interval_sec)
                return
            except asyncio.TimeoutError:
                continue

    # ---------------- internals ----------------

    def _decide(
        self,
        pos: PositionState,
        end_date: datetime | None,
        now: datetime,
    ) -> tuple[str | None, float]:
        """Apply the three triggers in order. Returns (reason, mtm_pct).

        Phase R.4 — when the entry was Fundamentals (LLM-driven) the
        thesis is "hold to binary settle." We skip profit_take and
        stop_loss because:
          - the LLM picked p_yes ≠ market_p based on evidence, not noise
          - intermediate price swings are just spread + book churn,
            not new information that invalidates the thesis
          - exit slippage on Polymarket is ~50-100 bps; flipping in-out
            burns the edge.
        time_decay (closing before resolution chaos) still fires — the
        agent should be flat before the binary settlement event.
        """
        if pos.avg_cost <= 0:
            return None, 0.0

        mtm_pct = float((pos.last_mark - pos.avg_cost) / pos.avg_cost)
        hold_to_resolution = (pos.horizon == "to_resolution")

        if not hold_to_resolution:
            if mtm_pct >= self.profit_take_pct:
                return "profit_take", mtm_pct
            if mtm_pct <= -self.stop_loss_pct:
                return "stop_loss", mtm_pct

        if end_date is not None:
            hours_left = (end_date - now).total_seconds() / 3600.0
            if hours_left < self.time_decay_safety_hours:
                return "time_decay_close", mtm_pct

        return None, mtm_pct

    async def _emit_exit(
        self,
        pos: PositionState,
        reason: str,
        mtm_pct: float,
    ) -> None:
        """Build SELL TradeDecision, submit, surface on dashboard."""
        # SELL at current mark (paper executor walks the bid side).
        price = Decimal(str(pos.last_mark)).quantize(Decimal("0.0001"))
        size = Decimal(str(abs(float(pos.qty)))).quantize(Decimal("0.01"))
        if size <= 0:
            return

        decision = TradeDecision(
            ts=datetime.now(UTC),
            strategy=f"exit.{reason}",
            token_id=pos.token_id,
            side=Side.SELL,
            order_type=OrderType.FAK,    # we want to flatten — fill what you can
            price=price,
            size=size,
        )
        log.warning(
            "exit.emit",
            reason=reason,
            token_id=pos.token_id[:14],
            qty=float(pos.qty),
            avg_cost=float(pos.avg_cost),
            mark=float(pos.last_mark),
            mtm_pct=round(mtm_pct, 4),
        )
        order: Order = await self.executor.submit(decision)

        # Mirror to the broker so the dashboard shows the exit.
        if self.broker is not None:
            try:
                self.broker.push_signal({
                    "ts": decision.ts.isoformat(),
                    "strategy": decision.strategy,
                    "condition_id": "",   # exit isn't tied to a strategy signal
                    "token_id": pos.token_id,
                    "edge": 0.0,
                    "conviction": 1.0,
                    "suggested_action": "SELL",
                    "rationale": {
                        "reason": reason,
                        "mtm_pct": mtm_pct,
                        "avg_cost": float(pos.avg_cost),
                        "last_mark": float(pos.last_mark),
                        "qty": float(pos.qty),
                    },
                })
                self.broker.push_order({
                    "ts": (order.ts_filled or order.ts_created).isoformat(),
                    "order_id": order.order_id,
                    "strategy": decision.strategy,
                    "contributors": [decision.strategy],
                    "condition_id": "",
                    "token_id": order.token_id,
                    "side": order.side.value,
                    "status": order.status.value,
                    "price": float(order.price) if order.price is not None else None,
                    "size": float(order.size),
                    "filled_size": float(order.filled_size),
                    "avg_fill_price": float(order.avg_fill_price)
                        if order.avg_fill_price is not None else None,
                    "mode": order.mode.value,
                    "edge": 0.0,
                    "conviction": 1.0,
                    "size_pct": 0.0,
                    "market_question": None,
                })
            except Exception as exc:
                log.debug("exit.broker_push_failed", error=str(exc)[:120])
