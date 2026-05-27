"""Trading pipeline — orchestrates the per-tick decision flow.

ingest → features → strategies → aggregator → risk → executor → portfolio

This module is the single place that wires strategies, the risk gate, and
the executor together. Main loop drives it on a clock or on book updates.

Critical invariant (MASTER_SPEC, immutable rule #3):
  **Every trade decision passes through `RiskPolicy.evaluate()`. No bypass.**
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from prometheus_client import Counter

from poly_meridian.domain import Market, Order
from poly_meridian.execution import OrderRouter, PaperExecutor
from poly_meridian.features import compute_features
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.portfolio import Ledger, mark_all, snapshot
from poly_meridian.risk import DefaultRiskPolicy, RiskDecision
from poly_meridian.risk.trade_metrics import compute_trade_metrics
from poly_meridian.strategies import (
    ArbitrageStrategy,
    FundamentalsStrategy,
    SentimentStrategy,
    SignalAggregator,
    SmartMoneyStrategy,
    StatQuantStrategy,
)

log = structlog.get_logger("poly_meridian.pipeline")

PM_SIGNAL_EMITTED = Counter(
    "pm_signal_emitted_total",
    "strategy signals emitted",
    ["strategy"],
)
PM_SIGNAL_AGGREGATED = Counter(
    "pm_signal_aggregated_total",
    "aggregated signals reaching risk gate",
)
PM_RISK_DECISION = Counter(
    "pm_risk_decision_total",
    "risk gate decisions",
    ["decision"],
)
PM_ORDER_SUBMITTED = Counter(
    "pm_order_submitted_total",
    "orders submitted to executor",
    ["side", "mode"],
)
# Phase Q.1 — instrument WHY each strategy returns None on a tick.
# Labels: strategy name (arbitrage, sentiment, smart_money, stat_quant,
# fundamentals) + reason (no_book, z_below_thr, no_history, cluster_short,
# resolver_none, edge_too_small, etc.). Aggregating these tells the
# operator EXACTLY where evaluations die, so the binding constraint can
# be loosened intentionally instead of by guesswork.
PM_STRATEGY_REJECT = Counter(
    "pm_strategy_reject_total",
    "per-strategy reject reasons (why evaluate() returned None)",
    ["strategy", "reason"],
)


class Pipeline:
    """Holds the wired strategies → aggregator → risk → executor chain."""

    def __init__(
        self,
        *,
        arbitrage: ArbitrageStrategy,
        sentiment: SentimentStrategy | None,
        smart_money: SmartMoneyStrategy | None,
        aggregator: SignalAggregator,
        risk: DefaultRiskPolicy,
        executor: PaperExecutor,
        ledger: Ledger,
        token_to_category: dict[str, str] | None = None,
        stat_quant: StatQuantStrategy | None = None,
        fundamentals: FundamentalsStrategy | None = None,
    ) -> None:
        self.arbitrage = arbitrage
        self.sentiment = sentiment
        self.smart_money = smart_money
        self.stat_quant = stat_quant
        self.fundamentals = fundamentals
        self.aggregator = aggregator
        self.risk = risk
        self.router = OrderRouter(executor)
        self.executor = executor
        self.ledger = ledger
        self.token_to_category: dict[str, str] = token_to_category or {}
        self._books: dict[str, LocalBook] = {}
        # Phase Q.6b+ — give risk a way to read live (bid, ask) for a
        # token so the spread-quality check works.
        if hasattr(self.risk, "set_book_lookup"):
            self.risk.set_book_lookup(self._lookup_bid_ask)

    def _lookup_bid_ask(self, token_id: str) -> tuple[float, float] | None:
        """Return (best_bid, best_ask) as floats, or None if either side
        is missing. Used by RiskPolicy.check_book_spread."""
        b = self._books.get(token_id)
        if b is None:
            return None
        bid = b.best_bid()
        ask = b.best_ask()
        if bid is None or ask is None:
            return None
        return float(bid[0]), float(ask[0])

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book
        self.arbitrage.attach_book(token_id, book)
        if self.sentiment is not None:
            self.sentiment.attach_book(token_id, book)
        if self.smart_money is not None:
            self.smart_money.attach_book(token_id, book)
        if self.stat_quant is not None:
            self.stat_quant.attach_book(token_id, book)
        if self.fundamentals is not None:
            self.fundamentals.attach_book(token_id, book)
        self.executor.attach_book(token_id, book)
        # Propagate category for fee schedule.
        cat = self.token_to_category.get(token_id)
        if cat is not None:
            self.executor.attach_category(token_id, cat)

    def register_market(self, market: Market) -> None:
        self.arbitrage.register_pair(
            market.condition_id, market.yes_token_id, market.no_token_id
        )
        # Phase Q.3 — feed the risk policy's concentration map so it can
        # map a portfolio position's token_id back to its condition + event
        # and block self-hedge / same-event piling.
        if hasattr(self.risk, "register_market"):
            self.risk.register_market(
                condition_id=market.condition_id,
                yes_token=market.yes_token_id,
                no_token=market.no_token_id,
                event_id=market.event_id,
            )
        if market.category:
            self.token_to_category[market.yes_token_id] = market.category
            self.token_to_category[market.no_token_id] = market.category
            self.executor.attach_category(market.yes_token_id, market.category)
            self.executor.attach_category(market.no_token_id, market.category)

    def attach_news_signals(self, condition_id: str, signals: list[dict[str, Any]]) -> None:
        if self.sentiment is not None:
            self.sentiment.attach_recent_signals(condition_id, signals)
        # Phase R.5 — also feed the LLMResolver a compact news summary
        # so Claude can reason about *why* the market is moving rather
        # than reading from scratch.
        if self.fundamentals is not None and signals:
            try:
                # Build a tight 2-3 sentence summary from the most-impactful
                # signals (sorted desc by impact). Keep ≤400 chars so we
                # don't blow the LLM prompt budget.
                top = sorted(signals, key=lambda r: r.get("impact", 0.0), reverse=True)[:3]
                lines = []
                for r in top:
                    title = (r.get("article_title") or "")[:120]
                    rationale = (r.get("rationale") or "")[:120]
                    direction = r.get("direction") or "?"
                    lines.append(f"[{direction}] {title} — {rationale}")
                summary = " | ".join(lines)
                self.fundamentals.attach_news_summary(condition_id, summary)
            except Exception:
                pass

    def attach_cluster_state(self, state: Any) -> None:
        if self.smart_money is not None:
            self.smart_money.attach_cluster_state(state)
        # Phase R.5 — push aggregated direction per condition into the LLM
        # context so Claude can read "smart money leaning YES on X" as a
        # structured signal. cluster_state is expected to expose
        # `per_condition: dict[condition_id, ClusterSummary]` or similar.
        if self.fundamentals is not None and state is not None:
            try:
                per_cond = getattr(state, "per_condition", None) or {}
                for cid, summary in per_cond.items():
                    direction = getattr(summary, "direction", None)
                    if direction:
                        self.fundamentals.attach_smart_money(cid, str(direction))
            except Exception:
                pass

    async def tick(self, market: Market) -> Order | None:
        """One pass through the full pipeline for a single market."""
        now = datetime.now(UTC)

        # Resolution-window gate (Phase I.0). Polymarket positions resolve to
        # $1 or $0 at end_date_iso — they are NOT continuously-held assets.
        # Markets resolving >max_resolution_days out lock capital with high
        # variance; markets <min_resolution_days have only intraday noise
        # left and adverse selection. Skip both ends here so no strategy
        # has to know about time semantics.
        if market.end_date_iso is not None:
            days_left = (market.end_date_iso - now).total_seconds() / 86_400.0
            max_d = self.risk.limits.max_resolution_days if hasattr(self.risk, "limits") else None
            min_d = self.risk.limits.min_resolution_days if hasattr(self.risk, "limits") else None
            if max_d is not None and days_left > max_d:
                return None
            if min_d is not None and days_left < min_d:
                return None

        book = self._books.get(market.yes_token_id)

        feats = compute_features(
            token_id=market.yes_token_id,
            now=now,
            book=book,
            end_date=market.end_date_iso,
        )

        # Push fresh mid prices into StatQuant's rolling windows. Done BEFORE
        # evaluate() so the strategy sees the current tick in its history.
        if self.stat_quant is not None and self.stat_quant.enabled:
            for tid in (market.yes_token_id, market.no_token_id):
                b = self._books.get(tid)
                if b is None:
                    continue
                mid = b.mid()
                if mid is not None:
                    self.stat_quant.push_price(tid, float(mid))

        # Evaluate enabled strategies in parallel-friendly sequence.
        signals = []
        for strat in (
            self.arbitrage, self.sentiment, self.smart_money,
            self.stat_quant, self.fundamentals,
        ):
            if strat is None or not getattr(strat, "enabled", False):
                continue
            try:
                s = await strat.evaluate(market, feats)
            except Exception as exc:
                log.warning("pipeline.strategy_error", strategy=strat.name, error=str(exc))
                continue
            if s is not None:
                signals.append(s)
                PM_SIGNAL_EMITTED.labels(strategy=strat.name).inc()
                # Surface the signal on the operator dashboard with full
                # rationale so the user can SEE what fired and WHY.
                broker = getattr(self, "broker", None)
                if broker is not None:
                    try:
                        broker.push_signal({
                            "ts": s.ts.isoformat(),
                            "strategy": s.strategy,
                            "condition_id": s.condition_id,
                            "token_id": s.token_id,
                            "edge": s.edge,
                            "conviction": s.conviction,
                            # Use .value so the UI gets clean "BUY_YES"
                            # rather than "Action.BUY_YES".
                            "suggested_action": s.suggested_action.value,
                            "rationale": s.rationale,
                            "market_question": market.question,
                        })
                    except Exception as exc:
                        log.debug("pipeline.push_signal_failed", error=str(exc)[:120])

        if not signals:
            return None

        # Mark all positions BEFORE the risk snapshot — fresh MTM on every tick.
        mark_all(self.ledger, self._books, ts=now)

        agg = self.aggregator.aggregate(
            signals,
            market=market,
            bankroll_usd=self.ledger.cash + Decimal("1"),
        )
        if agg is None:
            return None
        PM_SIGNAL_AGGREGATED.inc()

        port = snapshot(self.ledger, token_to_category=self.token_to_category, ts=now)
        decision = self.risk.evaluate(agg, port)
        PM_RISK_DECISION.labels(decision=str(decision)).inc()

        if decision == RiskDecision.REJECT:
            return None

        trade = self.risk.size(agg, port)
        if trade is None:
            return None

        order = await self.router.route(trade)
        PM_ORDER_SUBMITTED.labels(side=str(order.side), mode=str(order.mode)).inc()

        # Phase R.8 — stamp LLM probability claim on the position so
        # that on settlement we can compute Brier score. Find the
        # contributing fundamentals signal and lift its rationale.
        try:
            llm_sig = next(
                (s for s in signals if s.strategy == "fundamentals"
                 and "our_p_long" in (s.rationale or {})),
                None,
            )
            if llm_sig is not None:
                pos = self.ledger.get_position(order.token_id)
                if pos is not None:
                    rat = llm_sig.rationale or {}
                    pos.claimed_p_long = float(rat.get("our_p_long", 0.5))
                    pos.claimed_confidence = float(rat.get("resolver_confidence", 0.0))
                    if rat.get("base_rate") is not None:
                        pos.claimed_base_rate = float(rat["base_rate"])
        except Exception as exc:
            log.debug("pipeline.calibration_stamp_failed", error=str(exc)[:120])
        # Surface the order on the dashboard. Use contributors from the agg
        # so the UI shows the *combined* strategy attribution rather than just
        # the last leg name.
        broker = getattr(self, "broker", None)
        if broker is not None:
            # Risk / reward / EV — answers "neden girdik, ne kazanırız,
            # ne kaybederiz" without operator math. Uses fill price if
            # available, otherwise the limit price. Edge stays as-is.
            entry_price = (
                float(order.avg_fill_price)
                if order.avg_fill_price is not None
                else (float(order.price) if order.price is not None else None)
            )
            tm = compute_trade_metrics(
                entry_price=entry_price,
                size_units=float(order.size),
                edge=agg.edge,
            )
            try:
                broker.push_order({
                    "ts": (order.ts_filled or order.ts_created).isoformat(),
                    "order_id": order.order_id,
                    "strategy": order.strategy,
                    "contributors": list(agg.contributors),
                    "condition_id": agg.condition_id,
                    "token_id": order.token_id,
                    # .value strips the "Side." / "OrderStatus." enum prefix.
                    "side": order.side.value,
                    "status": order.status.value,
                    "price": float(order.price) if order.price is not None else None,
                    "size": float(order.size),
                    "filled_size": float(order.filled_size),
                    "avg_fill_price": float(order.avg_fill_price)
                        if order.avg_fill_price is not None else None,
                    "mode": order.mode.value,
                    "edge": agg.edge,
                    "conviction": agg.conviction,
                    "size_pct": agg.size_pct,
                    "market_question": market.question,
                    "category": market.category,
                    "trade_metrics": tm.asdict() if tm is not None else None,
                })
            except Exception as exc:
                log.debug("pipeline.push_order_failed", error=str(exc)[:120])
        return order

    async def on_fill(
        self,
        order: Order,
        filled_qty: Decimal | None = None,
        fill_price: Decimal | None = None,
        fee: Decimal | None = None,
    ) -> None:
        # Fee-aware path (PaperExecutor passes filled qty/price/fee per fill).
        if filled_qty is None or fill_price is None:
            # Backwards-compat: caller didn't pass per-fill detail.
            if order.avg_fill_price is None or order.filled_size <= 0:
                return
            filled_qty = order.filled_size
            fill_price = order.avg_fill_price
            fee = fee or Decimal(0)
        entry = self.ledger.apply_fill(
            ts=order.ts_filled or datetime.now(UTC),
            order=order,
            filled_qty=filled_qty,
            fill_price=fill_price,
            fee=fee or Decimal(0),
        )

        # Persist the ledger entry (Phase L.2). Fire-and-forget — DB outage
        # never blocks the fill path. fill_seq distinguishes partial fills
        # on the same order: count prior entries for this order_id.
        ledger_persist = getattr(self, "on_ledger_entry", None)
        if ledger_persist is not None and entry is not None:
            fill_seq = sum(
                1 for e in self.ledger.entries() if e.order_id == order.order_id
            ) - 1   # 0-indexed; current entry is already in the list
            try:
                # Compute realized PnL on SELL using FIFO-ish proxy: realized
                # per share = fill_price - avg_cost_before. We can read it
                # off the LedgerEntry's notional sign convention.
                realized = None
                pos = self.ledger.get_position(order.token_id)
                if str(order.side).endswith("SELL"):
                    # `notional` for SELL is +cash-in; realized PnL per fill
                    # is (price - avg_cost_at_time_of_fill) × qty. We don't
                    # know the pre-fill avg_cost cleanly here, so approximate
                    # by leaving None — fetch_pnl_per_strategy SUM-skips Nones.
                    realized = None
                ledger_persist({
                    "ts": entry.ts,
                    "order_id": entry.order_id,
                    "fill_seq": max(0, fill_seq),
                    "strategy": entry.strategy,
                    "token_id": entry.token_id,
                    "side": str(entry.side).split(".")[-1],
                    "qty": abs(entry.qty),
                    "price": entry.price,
                    "notional": abs(entry.notional),
                    "fee": entry.fee,
                    "realized_pnl": realized,
                })
            except Exception as exc:
                log.debug("pipeline.ledger_persist_failed", error=str(exc)[:120])

    def context_metrics(self) -> dict[str, Any]:
        return {
            "nav_usd": float(snapshot(self.ledger).nav_usd),
            "cash_usd": float(self.ledger.cash),
            "open_positions": len(self.ledger.positions()),
            "kill_switch_engaged": self.risk.is_kill_switch_engaged(),
        }
