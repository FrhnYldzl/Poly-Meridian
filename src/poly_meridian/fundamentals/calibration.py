"""LLM calibration tracker — Brier score + bucketed accuracy. Phase R.8.

The trading thesis is "the LLM finds mispricings the market doesn't see."
That claim is testable: every time we trade on an LLM probability and
the market resolves, we now have a ground truth. The Brier score
measures how close those probabilities were to reality over many trades.

Brier score for binary outcomes:
    BS = (1/N) Σ (p_yes_i - outcome_i)²

  outcome_i = 1 if YES won, 0 if NO won
  Lower is better; a constant 0.5 forecaster scores 0.25
  A perfectly calibrated, high-confidence forecaster approaches 0

Bucketed accuracy splits predictions by stated confidence band — if
the LLM says "0.7 confidence" 100 times, we expect ~70 wins for it
to be well calibrated. Wide deviations indicate over/underconfidence
and should pull entry conviction down via Phase R.9 weighting.

The store is in-memory + DB-backed (calibration_entries table). The
in-memory window is the most recent N=500 entries for fast rolling
metrics; historical entries live in DB for backtest replay.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from poly_meridian.portfolio.ledger import PositionState

log = structlog.get_logger("poly_meridian.fundamentals.calibration")


@dataclass
class CalibrationEntry:
    """One resolved LLM prediction (binary outcome, claimed probability)."""
    ts_resolved: datetime
    token_id: str
    entry_strategy: str | None
    # Claimed probability that the SIDE WE BOUGHT would win — stored as
    # p_long, not p_yes. For BUY YES this equals p_yes; for BUY NO it
    # equals 1 - p_yes. Brier math is then `(p_long - won)²` directly.
    claimed_p_long: float
    confidence: float
    settle_price: float           # 0.0 or 1.0 (the SIDE WE BOUGHT)
    won: bool                     # settle_price == 1.0
    pnl_usd: float                # realized P&L of this single position
    base_rate: float | None = None  # the LLM's base-rate estimate, if any


@dataclass
class CalibrationMetrics:
    n_entries: int = 0
    brier_score: float | None = None
    accuracy: float | None = None
    # Per-confidence-bucket accuracy. Keys: "0.5-0.6", "0.6-0.7", etc.
    bucket_accuracy: dict[str, float] = field(default_factory=dict)
    bucket_counts: dict[str, int] = field(default_factory=dict)
    # Mean confidence claimed across all entries. If brier < 0.25 and
    # mean_confidence ≈ accuracy → well calibrated.
    mean_confidence: float | None = None
    mean_p_long: float | None = None
    total_pnl_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_entries": self.n_entries,
            "brier_score": self.brier_score,
            "accuracy": self.accuracy,
            "mean_confidence": self.mean_confidence,
            "mean_p_long": self.mean_p_long,
            "total_pnl_usd": round(self.total_pnl_usd, 4),
            "bucket_accuracy": self.bucket_accuracy,
            "bucket_counts": self.bucket_counts,
        }


_BUCKETS = [
    ("0.50-0.60", 0.50, 0.60),
    ("0.60-0.70", 0.60, 0.70),
    ("0.70-0.80", 0.70, 0.80),
    ("0.80-0.90", 0.80, 0.90),
    ("0.90-1.00", 0.90, 1.01),  # inclusive of 1.0
]


class CalibrationStore:
    """In-memory rolling window + optional DB persistence hook.

    Wired into ExitMonitor via `_calibration_recorder`. On every
    settlement (Phase R.7) the store appends a new entry. A consumer
    (broker_refresh_loop) reads `metrics()` to surface on /api/state.
    """

    def __init__(self, *, max_entries: int = 500, db_writer: Any = None) -> None:
        self._entries: deque[CalibrationEntry] = deque(maxlen=max_entries)
        self._db_writer = db_writer  # callable(entry) -> None / coroutine

    # ----- write path -----

    def record(
        self,
        *,
        token_id: str,
        settle_price: float,
        pos: PositionState,
        ts: datetime,
    ) -> None:
        """Called by ExitMonitor on settlement. Pulls the entry rationale
        off the position metadata if available — we stash it in pos
        comments via Phase R.9 — and computes claimed_p_long from the
        signal that opened the trade.

        Falls back to a 0.5 prior if we don't have a stored probability
        (e.g. position was opened by a non-LLM strategy)."""
        # PositionState doesn't carry rationale, but the BROKER snapshot
        # does — the entry's strategy + entry_price tell us what side
        # we bought. For the LLM calibration we need claimed_p_long.
        # If the strategy stamped a `_llm_claimed_p_long` on the
        # position via a sidecar dict (Phase R.9), use that. Otherwise
        # we can't score this one — log and skip.
        claimed_p_long = getattr(pos, "claimed_p_long", None)
        confidence = getattr(pos, "claimed_confidence", None)
        base_rate = getattr(pos, "claimed_base_rate", None)
        if claimed_p_long is None or confidence is None:
            log.debug(
                "calibration.skip_no_prediction",
                token_id=token_id[:14],
                entry_strategy=pos.entry_strategy,
            )
            return

        pnl_usd = float(
            (pos.last_mark - pos.avg_cost) * pos.qty - pos.fees_paid
        ) if pos.avg_cost > 0 and pos.qty != 0 else 0.0
        # settle_price for the SIDE WE BOUGHT (caller already mapped
        # yes_token / no_token to the correct settle column)
        won = float(settle_price) >= 0.999

        entry = CalibrationEntry(
            ts_resolved=ts,
            token_id=token_id,
            entry_strategy=pos.entry_strategy,
            claimed_p_long=float(claimed_p_long),
            confidence=float(confidence),
            settle_price=float(settle_price),
            won=won,
            pnl_usd=pnl_usd,
            base_rate=float(base_rate) if base_rate is not None else None,
        )
        self._entries.append(entry)

        # Best-effort DB persistence (calibration_entries table)
        if self._db_writer is not None:
            try:
                res = self._db_writer(entry)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as exc:
                log.warning("calibration.db_write_failed", error=str(exc)[:120])

        log.info(
            "calibration.recorded",
            token_id=token_id[:14],
            claimed=round(entry.claimed_p_long, 3),
            confidence=round(entry.confidence, 3),
            won=entry.won,
            pnl=round(entry.pnl_usd, 4),
            n_entries=len(self._entries),
        )

    # ----- read path -----

    def metrics(self) -> CalibrationMetrics:
        """Compute rolling metrics from the in-memory window. Cheap —
        single O(N) pass over up to 500 entries."""
        m = CalibrationMetrics()
        entries = list(self._entries)
        n = len(entries)
        m.n_entries = n
        if n == 0:
            return m

        squared_err = 0.0
        wins = 0
        sum_conf = 0.0
        sum_p = 0.0
        sum_pnl = 0.0
        # buckets[name] = (n, n_wins)
        bucket_n: dict[str, int] = {b[0]: 0 for b in _BUCKETS}
        bucket_wins: dict[str, int] = {b[0]: 0 for b in _BUCKETS}

        for e in entries:
            outcome = 1.0 if e.won else 0.0
            squared_err += (e.claimed_p_long - outcome) ** 2
            wins += 1 if e.won else 0
            sum_conf += e.confidence
            sum_p += e.claimed_p_long
            sum_pnl += e.pnl_usd
            for name, lo, hi in _BUCKETS:
                if lo <= e.claimed_p_long < hi:
                    bucket_n[name] += 1
                    bucket_wins[name] += 1 if e.won else 0
                    break

        m.brier_score = round(squared_err / n, 4)
        m.accuracy = round(wins / n, 4)
        m.mean_confidence = round(sum_conf / n, 4)
        m.mean_p_long = round(sum_p / n, 4)
        m.total_pnl_usd = sum_pnl
        for name in bucket_n:
            cnt = bucket_n[name]
            m.bucket_counts[name] = cnt
            m.bucket_accuracy[name] = (
                round(bucket_wins[name] / cnt, 4) if cnt > 0 else None  # type: ignore[assignment]
            )
        return m

    def __len__(self) -> int:
        return len(self._entries)
