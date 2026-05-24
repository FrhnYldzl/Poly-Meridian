"""Synthetic backtest — deterministic-seed random-walk on simulated markets.

The point: give the operator a *working* backtest button TODAY, before we
have meaningful Timescale history. The math models realistic Polymarket
behavior:
  - Per-market geometric random walk on YES price (mean-reverting to 0.5)
  - Trades fire on |zscore| > threshold, sized by Kelly fraction
  - Returns equity_curve + per-trade PnLs + summary metrics

Switching to real DB replay (load_dataset_from_db + Replayer) is the
follow-up sprint — same shape of result, same UI, no client changes.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


@dataclass
class SyntheticBacktestConfig:
    n_markets: int = 12
    n_steps: int = 200                  # ~3 hours at 60s/step
    step_sec: int = 60
    starting_nav: float = 100_000.0
    bet_size_pct: float = 0.02          # 2% of NAV per trade
    zscore_threshold: float = 1.5
    seed: int = 42
    fee_bps: float = 200.0              # 2% round-trip fee proxy


@dataclass
class SyntheticTrade:
    ts: str
    market: str
    side: str            # "BUY_YES" / "BUY_NO"
    entry_price: float
    exit_price: float
    pnl_usd: float


@dataclass
class SyntheticBacktestResult:
    equity_curve: list[dict]     # [{ts, nav}]
    trades: list[SyntheticTrade]
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    trade_count: int
    final_nav: float
    starting_nav: float
    duration_sec: int
    seed: int

    def asdict(self) -> dict:
        return {
            "equity_curve": self.equity_curve,
            "trades": [t.__dict__ for t in self.trades],
            "total_return_pct": self.total_return_pct,
            "sharpe": self.sharpe,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "final_nav": self.final_nav,
            "starting_nav": self.starting_nav,
            "duration_sec": self.duration_sec,
            "seed": self.seed,
        }


def run_synthetic_backtest(
    config: SyntheticBacktestConfig | None = None,
) -> SyntheticBacktestResult:
    """Run a deterministic synthetic backtest. Same seed → same result."""
    cfg = config or SyntheticBacktestConfig()
    rng = random.Random(cfg.seed)

    # Initialize markets with prices drawn from [0.3, 0.7] uniformly.
    prices = [rng.uniform(0.30, 0.70) for _ in range(cfg.n_markets)]
    histories: list[list[float]] = [[p] for p in prices]

    nav = cfg.starting_nav
    cash = nav
    open_positions: dict[int, tuple[str, float, float]] = {}  # market_id → (side, entry, size_units)
    trades: list[SyntheticTrade] = []
    equity_curve: list[dict] = []

    base_ts = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    base_ts = base_ts - timedelta(seconds=cfg.step_sec * cfg.n_steps)

    fee_factor = cfg.fee_bps / 10_000.0   # round-trip fee fraction

    for step in range(cfg.n_steps):
        ts = base_ts + timedelta(seconds=cfg.step_sec * step)

        # Update prices: mean-reverting random walk toward 0.5.
        for mid in range(cfg.n_markets):
            cur = prices[mid]
            # Pull-to-mean + noise. ~1% vol per step.
            drift = (0.5 - cur) * 0.01
            shock = rng.gauss(0, 0.015)
            new_p = max(0.02, min(0.98, cur + drift + shock))
            prices[mid] = new_p
            histories[mid].append(new_p)

        # Strategy: mean-reversion. Check zscore over last 20 steps.
        for mid in range(cfg.n_markets):
            h = histories[mid][-20:]
            if len(h) < 20:
                continue
            mean = sum(h) / len(h)
            var = sum((x - mean) ** 2 for x in h) / len(h)
            std = math.sqrt(var) or 1e-9
            cur = prices[mid]
            z = (cur - mean) / std

            # Manage existing position — close if price reverts within 50% of entry move
            if mid in open_positions:
                side, entry, size_units = open_positions[mid]
                target_revert = 0.5  # close when price reverts halfway
                profitable_exit = (
                    (side == "BUY_NO" and cur <= entry - (entry - mean) * target_revert)
                    or (side == "BUY_YES" and cur >= entry + (mean - entry) * target_revert)
                )
                step_cap_reached = step - histories[mid].index(entry) > 30 if entry in histories[mid] else False  # stale guard
                if profitable_exit or step_cap_reached:
                    # Realize PnL with round-trip fee.
                    exit_price = cur if side == "BUY_YES" else (1.0 - cur)
                    entry_paid = entry if side == "BUY_YES" else (1.0 - entry)
                    gross = (exit_price - entry_paid) * size_units
                    fee = abs(entry_paid * size_units + exit_price * size_units) * (fee_factor / 2)
                    pnl = gross - fee
                    cash += entry_paid * size_units + pnl     # release capital + PnL
                    trades.append(SyntheticTrade(
                        ts=ts.isoformat(),
                        market=f"market_{mid:02d}",
                        side=side,
                        entry_price=entry,
                        exit_price=cur,
                        pnl_usd=round(pnl, 2),
                    ))
                    del open_positions[mid]
                    continue

            if mid in open_positions:
                continue

            # Open new position if zscore signals reversion opportunity.
            if abs(z) < cfg.zscore_threshold:
                continue
            # High z → price too high → BUY_NO. Low z → BUY_YES.
            side = "BUY_NO" if z > 0 else "BUY_YES"
            entry_price = cur if side == "BUY_YES" else (1.0 - cur)
            size_usd = nav * cfg.bet_size_pct
            if size_usd > cash * 0.95:
                continue   # cash-bound; skip
            size_units = size_usd / max(entry_price, 0.01)
            cash -= entry_price * size_units
            open_positions[mid] = (side, cur, size_units)

        # Mark-to-market NAV.
        open_value = 0.0
        for mid, (side, entry, size_units) in open_positions.items():
            cur = prices[mid]
            mark = cur if side == "BUY_YES" else (1.0 - cur)
            open_value += mark * size_units
        nav = cash + open_value
        equity_curve.append({"ts": ts.isoformat(), "nav": round(nav, 2)})

    final_nav = nav
    total_return_pct = (final_nav - cfg.starting_nav) / cfg.starting_nav

    # Metrics
    if len(equity_curve) >= 2:
        # Daily returns proxy from step returns
        navs = [p["nav"] for p in equity_curve]
        rets = []
        for i in range(1, len(navs)):
            if navs[i - 1] > 0:
                rets.append((navs[i] - navs[i - 1]) / navs[i - 1])
        if rets:
            mean_r = sum(rets) / len(rets)
            var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
            std_r = math.sqrt(var_r) or 1e-9
            # Annualize from step return — ~step_sec resolution.
            steps_per_year = (365 * 24 * 3600) / cfg.step_sec
            sharpe = (mean_r / std_r) * math.sqrt(steps_per_year)
        else:
            sharpe = 0.0

        # Max drawdown
        peak = navs[0]
        max_dd = 0.0
        for n in navs:
            peak = max(peak, n)
            dd = (peak - n) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
    else:
        sharpe = 0.0
        max_dd = 0.0

    wins = sum(1 for t in trades if t.pnl_usd > 0)
    win_rate = wins / len(trades) if trades else 0.0

    return SyntheticBacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        total_return_pct=total_return_pct,
        sharpe=sharpe,
        max_drawdown_pct=max_dd,
        win_rate=win_rate,
        trade_count=len(trades),
        final_nav=final_nav,
        starting_nav=cfg.starting_nav,
        duration_sec=cfg.step_sec * cfg.n_steps,
        seed=cfg.seed,
    )
