"""StatQuantStrategy — four micro-strategies in one container. §14.4.

1. Mean reversion: |z-score| > threshold → bet against the move
2. Momentum: short-window return > threshold AND volume confirm → bet with the trend
3. Volatility breakout: low vol + sudden range expansion → bet in breakout direction
4. Time decay arb: < N hours to resolution AND market mispricing → small bet on resolution

Each sub-signal is configurable independently. The strategy returns at most
one StrategySignal per evaluate() — the strongest among the four.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import Action, Features, Market, StrategySignal
from poly_meridian.features.ta_features import (
    RollingPriceWindow,
    momentum,
    rolling_volatility,
    rolling_zscore,
)
from poly_meridian.features.time_features import time_to_resolution_hours
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.strategies.base import BaseStrategy

log = structlog.get_logger("poly_meridian.strategies.stat_quant")


class StatQuantStrategy(BaseStrategy):
    """Configuration (see config/strategies/stat_quant.yaml):

      enabled
      mean_reversion:   {zscore_threshold, min_window, max_size_pct}
      momentum:         {lookback_window, return_threshold, min_volume,
                         max_size_pct}
      vol_breakout:     {low_vol_threshold, breakout_multiplier, max_size_pct}
      time_decay:       {horizon_hours_max, price_deviation_threshold,
                         max_size_pct}
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(name="stat_quant", config=config, enabled=config.get("enabled", False))

        mr = config.get("mean_reversion", {}) or {}
        mo = config.get("momentum", {}) or {}
        vb = config.get("vol_breakout", {}) or {}
        td = config.get("time_decay", {}) or {}

        self.mr_zscore_threshold = float(mr.get("zscore_threshold", 2.0))
        self.mr_min_window = int(mr.get("min_window", 10))
        self.mr_max_size_pct = float(mr.get("max_size_pct", 0.02))

        self.mo_lookback = int(mo.get("lookback_window", 20))
        self.mo_return_threshold = float(mo.get("return_threshold", 0.05))
        self.mo_min_volume = float(mo.get("min_volume", 0.0))
        self.mo_max_size_pct = float(mo.get("max_size_pct", 0.02))

        self.vb_low_vol_threshold = float(vb.get("low_vol_threshold", 0.005))
        self.vb_breakout_multiplier = float(vb.get("breakout_multiplier", 2.0))
        self.vb_max_size_pct = float(vb.get("max_size_pct", 0.02))

        self.td_horizon_hours_max = float(td.get("horizon_hours_max", 24.0))
        self.td_price_deviation_threshold = float(td.get("price_deviation_threshold", 0.10))
        self.td_max_size_pct = float(td.get("max_size_pct", 0.015))

        # Per-token rolling price history. Sized for the largest window we use.
        self._capacity = max(self.mr_min_window, self.mo_lookback, 60)
        self._history: dict[str, RollingPriceWindow] = {}
        self._volume_1h: dict[str, float] = {}
        self._books: dict[str, LocalBook] = {}

    def attach_book(self, token_id: str, book: LocalBook) -> None:
        self._books[token_id] = book

    def push_price(self, token_id: str, price: float) -> None:
        """Pipeline pushes the latest mid price each tick."""
        win = self._history.setdefault(token_id, RollingPriceWindow(capacity=self._capacity))
        win.push(price)

    def set_volume(self, token_id: str, volume_1h: float) -> None:
        self._volume_1h[token_id] = volume_1h

    async def evaluate(
        self, market: Market, features: Features
    ) -> StrategySignal | None:
        if not self.enabled:
            return None

        # Pick the strongest sub-signal across the four legs.
        candidates: list[StrategySignal] = []

        sig_mr = self._mean_reversion(market, features)
        if sig_mr is not None:
            candidates.append(sig_mr)
        sig_mo = self._momentum(market, features)
        if sig_mo is not None:
            candidates.append(sig_mo)
        sig_vb = self._vol_breakout(market, features)
        if sig_vb is not None:
            candidates.append(sig_vb)
        sig_td = self._time_decay(market, features)
        if sig_td is not None:
            candidates.append(sig_td)

        if not candidates:
            return None
        return max(candidates, key=lambda s: s.conviction)

    def capacity_estimate(self) -> float:
        return 4_000.0

    # ---------------- sub-signals ----------------

    def _mean_reversion(self, market: Market, features: Features) -> StrategySignal | None:
        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        win = self._history.get(market.yes_token_id)
        if win is None or len(win.prices) < self.mr_min_window:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.mean_reversion", reason="no_history").inc()
            return None
        z = rolling_zscore(win.list())
        if z is None or abs(z) < self.mr_zscore_threshold:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.mean_reversion", reason="z_below_thr").inc()
            return None
        # Bet against the move: high z → SELL YES (i.e. BUY NO).
        book_token = market.no_token_id if z > 0 else market.yes_token_id
        book = self._books.get(book_token)
        if book is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.mean_reversion", reason="no_book").inc()
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.mean_reversion", reason="no_best_ask").inc()
            return None
        price, _ = best_ask
        action = Action.BUY_NO if z > 0 else Action.BUY_YES
        conviction = min(1.0, abs(z) / (self.mr_zscore_threshold * 2))
        # Edge estimate: half the z-score normalized.
        edge = min(0.20, abs(z) * 0.05)
        # Kelly inputs on the long side (Phase N.1). our_p_long ≈ market_p ±
        # edge depending on direction.
        market_p_long = float(price)
        our_p_long = max(0.01, min(0.99, market_p_long + edge))

        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=f"{self.name}.mean_reversion",
            condition_id=market.condition_id,
            token_id=book_token,
            edge=edge,
            conviction=conviction,
            suggested_action=action,
            rationale={"sub": "mean_reversion", "zscore": z, "best_ask": float(price),
                       "max_size_pct": self.mr_max_size_pct,
                       "our_p_long": our_p_long, "market_p_long": market_p_long},
        )

    def _momentum(self, market: Market, features: Features) -> StrategySignal | None:
        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        win = self._history.get(market.yes_token_id)
        if win is None or len(win.prices) < self.mo_lookback:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.momentum", reason="no_history").inc()
            return None
        recent = list(win.prices)[-self.mo_lookback:]
        ret = momentum(recent)
        if ret is None or abs(ret) < self.mo_return_threshold:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.momentum", reason="ret_below_thr").inc()
            return None
        if self._volume_1h.get(market.yes_token_id, 0.0) < self.mo_min_volume:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.momentum", reason="low_volume").inc()
            return None
        # Bet WITH the trend.
        action = Action.BUY_YES if ret > 0 else Action.BUY_NO
        book_token = market.yes_token_id if ret > 0 else market.no_token_id
        book = self._books.get(book_token)
        if book is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.momentum", reason="no_book").inc()
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.momentum", reason="no_best_ask").inc()
            return None
        price, _ = best_ask

        conviction = min(1.0, abs(ret) / (self.mo_return_threshold * 4))
        edge = min(0.15, abs(ret) * 1.0)
        market_p_long = float(price)
        our_p_long = max(0.01, min(0.99, market_p_long + edge))
        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=f"{self.name}.momentum",
            condition_id=market.condition_id,
            token_id=book_token,
            edge=edge,
            conviction=conviction,
            suggested_action=action,
            rationale={"sub": "momentum", "return": ret, "best_ask": float(price),
                       "max_size_pct": self.mo_max_size_pct,
                       "our_p_long": our_p_long, "market_p_long": market_p_long},
        )

    def _vol_breakout(self, market: Market, features: Features) -> StrategySignal | None:
        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        win = self._history.get(market.yes_token_id)
        if win is None or len(win.prices) < self._capacity:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.vol_breakout", reason="no_history").inc()
            return None
        prices = win.list()
        # Compare recent N to prior N — Phase 4 minimal logic.
        half = len(prices) // 2
        prior = prices[:half]
        recent = prices[half:]
        prior_vol = rolling_volatility(prior) or 0.0
        recent_vol = rolling_volatility(recent) or 0.0
        if prior_vol == 0 or prior_vol > self.vb_low_vol_threshold:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.vol_breakout", reason="prior_vol_too_high").inc()
            return None
        if recent_vol < prior_vol * self.vb_breakout_multiplier:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.vol_breakout", reason="no_breakout").inc()
            return None

        ret = momentum(recent) or 0.0
        if abs(ret) < 0.01:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.vol_breakout", reason="ret_below_thr").inc()
            return None

        action = Action.BUY_YES if ret > 0 else Action.BUY_NO
        book_token = market.yes_token_id if ret > 0 else market.no_token_id
        book = self._books.get(book_token)
        if book is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.vol_breakout", reason="no_book").inc()
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.vol_breakout", reason="no_best_ask").inc()
            return None
        price, _ = best_ask
        conviction = min(1.0, recent_vol / (prior_vol * self.vb_breakout_multiplier))
        edge = min(0.10, abs(ret))
        market_p_long = float(price)
        our_p_long = max(0.01, min(0.99, market_p_long + edge))

        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=f"{self.name}.vol_breakout",
            condition_id=market.condition_id,
            token_id=book_token,
            edge=edge,
            conviction=conviction,
            suggested_action=action,
            rationale={"sub": "vol_breakout", "prior_vol": prior_vol, "recent_vol": recent_vol,
                       "best_ask": float(price), "max_size_pct": self.vb_max_size_pct,
                       "our_p_long": our_p_long, "market_p_long": market_p_long},
        )

    def _time_decay(self, market: Market, features: Features) -> StrategySignal | None:
        from poly_meridian.pipeline import PM_STRATEGY_REJECT
        if market.end_date_iso is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="no_end_date").inc()
            return None
        now = datetime.now(UTC)
        hours = time_to_resolution_hours(now, market.end_date_iso)
        if hours is None or hours <= 0 or hours > self.td_horizon_hours_max:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="outside_horizon").inc()
            return None

        yes_book = self._books.get(market.yes_token_id)
        no_book = self._books.get(market.no_token_id)
        if yes_book is None or no_book is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="no_book").inc()
            return None
        yes_mid = yes_book.mid()
        no_mid = no_book.mid()
        if yes_mid is None or no_mid is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="no_mid").inc()
            return None

        # If the market thinks YES is "very likely" (>0.5+threshold) but the price
        # is far from 1.0, lean YES (assumes implied prob underprices certainty
        # as resolution nears).
        yes_p = float(yes_mid)
        if yes_p > 0.5 + self.td_price_deviation_threshold:
            action, token_id, edge_dir = Action.BUY_YES, market.yes_token_id, 1.0
        elif yes_p < 0.5 - self.td_price_deviation_threshold:
            action, token_id, edge_dir = Action.BUY_NO, market.no_token_id, 1.0
        else:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="price_within_band").inc()
            return None

        book = self._books.get(token_id)
        if book is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="no_book").inc()
            return None
        best_ask = book.best_ask()
        if best_ask is None:
            PM_STRATEGY_REJECT.labels(strategy="stat_quant.time_decay", reason="no_best_ask").inc()
            return None
        price, _ = best_ask

        # Time-decay conviction grows as resolution approaches.
        closeness = 1.0 - (hours / self.td_horizon_hours_max)
        conviction = min(1.0, 0.4 + 0.6 * closeness)
        deviation = abs(yes_p - 0.5)
        edge = min(0.20, deviation * edge_dir)
        # Long-side Kelly inputs: the side we're BUYING. For BUY_YES,
        # market_p_long = best_ask on yes_token. our_p_long shifts toward 1.0
        # (we expect resolution to confirm the favorite).
        market_p_long = float(price)
        our_p_long = max(0.01, min(0.99, market_p_long + edge))

        return StrategySignal(
            ts=datetime.now(UTC),
            strategy=f"{self.name}.time_decay",
            condition_id=market.condition_id,
            token_id=token_id,
            edge=edge,
            conviction=conviction,
            suggested_action=action,
            rationale={"sub": "time_decay", "hours_to_resolution": hours, "yes_p": yes_p,
                       "best_ask": float(price), "max_size_pct": self.td_max_size_pct,
                       "our_p_long": our_p_long, "market_p_long": market_p_long},
        )

    # ---------------- aggregator helpers ----------------

    @staticmethod
    def proposed_price_from_signal(rationale: dict[str, Any]) -> Decimal:
        return Decimal(str(rationale.get("best_ask", 0.5)))

    @staticmethod
    def proposed_size_pct(
        rationale: dict[str, Any],
        bankroll_usd: Decimal,
        max_size_pct: float,
    ) -> float:
        # Each sub-signal carries its own cap via `max_size_pct` in rationale.
        sub_cap = float(rationale.get("max_size_pct", max_size_pct))
        # Phase N.1: Kelly sizing on long-side prob (set by each sub-signal).
        our_p = rationale.get("our_p_long")
        mkt_p = rationale.get("market_p_long")
        if our_p is not None and mkt_p is not None:
            from poly_meridian.risk.kelly import sized_kelly
            effective_cap = min(max_size_pct, sub_cap)
            kr = sized_kelly(
                p=float(our_p), market_price=float(mkt_p),
                bankroll_usd=bankroll_usd,
                kelly_fraction_multiplier=0.25, hard_cap_pct=effective_cap,
            )
            return kr.f_used
        return float(min(max_size_pct, sub_cap))
