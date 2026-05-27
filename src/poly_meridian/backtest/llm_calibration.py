"""LLMCalibrationRunner — validate Claude's forecast skill on past markets.

Why this exists:

Phase R wired LLMResolver into live trading. The thesis — "AI finds
mispricings the market can't see" — is testable but only AFTER we've
spent real (paper) capital and waited for resolutions. Brier score on
live trades takes weeks to accumulate enough samples for statistical
significance.

This module does the same thing OFFLINE against historical resolved
markets:

  1. Fetch ~100 closed Polymarket markets with known outcomes (Gamma
     /markets?closed=true&limit=N) — voided markets are filtered out.
  2. For each, build a minimal Market + FundamentalsContext mirroring
     what the live pipeline would have seen at the *as-of* time the
     question was asked (NO leakage of post-resolution news).
  3. Call LLMResolver with cache DISABLED so every call is a fresh
     evaluation. Use Haiku only — the cost should be <$0.05 for a
     full validation run, and we want a baseline that mirrors what
     production triage does.
  4. Compare each (claimed_p_yes, settled_yes) into a fresh
     CalibrationStore. Compute Brier + bucketed accuracy.
  5. Persist the run summary into llm_backtest_runs (Phase O.1 schema
     pattern) so we can compare across model versions / prompt
     iterations over time.

If Brier > 0.25 (worse than coin-flip) → LLMResolver is NOT producing
useful forecasts on this question distribution. Adjust prompt, lower
min_confidence floor, or swap models BEFORE letting live capital flow
through this path.

If Brier < 0.20 → meaningful skill. We've earned the right to trust
live trades, and can scale capital up.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import FundamentalsContext, ProbabilityEstimate
from poly_meridian.fundamentals.calibration import CalibrationEntry, CalibrationStore
from poly_meridian.fundamentals.llm_resolver import LLMResolver
from poly_meridian.ingestion.normalize import (
    derive_category_from_tags,
    gamma_market_to_domain,
)
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.backtest.llm_calibration")


@dataclass
class _Outcome:
    """Resolved outcome extracted from Gamma's outcomePrices."""
    yes_won: bool
    no_won: bool
    voided: bool


@dataclass
class LLMBacktestRunSummary:
    """Snapshot of a completed run. Persisted + surfaced on UI."""
    started_at: datetime
    finished_at: datetime
    n_markets_attempted: int
    n_markets_scored: int
    n_skipped_voided: int
    n_skipped_no_estimate: int
    brier_score: float | None
    accuracy: float | None
    mean_confidence: float | None
    mean_p_long: float | None
    bucket_accuracy: dict[str, float | None] = field(default_factory=dict)
    bucket_counts: dict[str, int] = field(default_factory=dict)
    triage_model: str = ""
    cost_usd_estimate: float = 0.0
    # Compact per-market breakdown for explainability
    sample_predictions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "n_markets_attempted": self.n_markets_attempted,
            "n_markets_scored": self.n_markets_scored,
            "n_skipped_voided": self.n_skipped_voided,
            "n_skipped_no_estimate": self.n_skipped_no_estimate,
            "brier_score": self.brier_score,
            "accuracy": self.accuracy,
            "mean_confidence": self.mean_confidence,
            "mean_p_long": self.mean_p_long,
            "bucket_accuracy": self.bucket_accuracy,
            "bucket_counts": self.bucket_counts,
            "triage_model": self.triage_model,
            "cost_usd_estimate": round(self.cost_usd_estimate, 4),
            "sample_predictions": self.sample_predictions,
        }


class LLMCalibrationRunner:
    """One-shot LLM backtest. Cheap (<$0.05 per 100-market run) and
    safe (no live capital touched, no cache pollution since we use a
    dedicated isolated resolver instance)."""

    GAMMA_BASE = "https://gamma-api.polymarket.com"

    def __init__(
        self,
        *,
        resolver: LLMResolver | None = None,
        max_concurrency: int = 4,
    ) -> None:
        s = get_settings()
        # Dedicated resolver with a near-zero cache TTL so every call
        # produces a fresh probability. Daily budget left at settings
        # default — operator can lower for backtest runs.
        self._resolver = resolver or LLMResolver(
            cache_ttl_sec=1,
            # Use the same Haiku triage model as production so the
            # backtest measures what live trading actually does.
            triage_model=s.llm_resolver_triage_model,
            deep_model=s.llm_resolver_deep_model,
        )
        self._sem = asyncio.Semaphore(max_concurrency)

    # ---------- public API ----------

    async def run(
        self,
        *,
        n_markets: int = 100,
        category_filter: str | None = None,
    ) -> tuple[LLMBacktestRunSummary, CalibrationStore]:
        """Fetch resolved markets, score each, return summary + store."""
        started = datetime.now(UTC)
        rows = await self._fetch_resolved_markets(limit=n_markets * 2)
        # Filter voided + dedup by condition_id
        candidates: list[tuple[Market, _Outcome]] = []
        seen: set[str] = set()
        for raw in rows:
            outcome = self._extract_outcome(raw)
            if outcome.voided:
                continue
            m = gamma_market_to_domain(raw)
            if m is None or m.condition_id in seen:
                continue
            # Backfill category from `events[*].tags` since closed markets
            # often have null `category` field but populated `events` array.
            if not m.category:
                events = raw.get("events") or []
                if isinstance(events, list) and events:
                    tags = events[0].get("tags") if isinstance(events[0], dict) else None
                    cat = derive_category_from_tags(tags)
                    if cat:
                        m = m.model_copy(update={"category": cat})
            seen.add(m.condition_id)
            candidates.append((m, outcome))
            if len(candidates) >= n_markets:
                break

        log.info(
            "llm_backtest.start",
            n_candidates=len(candidates),
            n_fetched=len(rows),
        )

        # Fresh CalibrationStore — isolated from live state
        store = CalibrationStore(max_entries=n_markets + 10)
        n_voided = sum(1 for r in rows if self._extract_outcome(r).voided)
        n_no_est = 0
        samples: list[dict[str, Any]] = []

        # Process with bounded concurrency
        async def _process_one(m: Market, oc: _Outcome) -> dict[str, Any] | None:
            nonlocal n_no_est
            async with self._sem:
                ctx = FundamentalsContext()
                est = await self._resolver.resolve_async(m, ctx)
                if est is None:
                    n_no_est += 1
                    return None
                # Map to "our_p_long" for the WINNING side — for a
                # backtest we want to score the model's calibration on
                # the side it FAVORED, not just on YES side. Take the
                # side with p_yes > 0.5 as "the bet we'd place"; if
                # both sides are near 0.5 just score against YES.
                if est.p_yes >= 0.5:
                    claimed_p_long = est.p_yes
                    won = oc.yes_won
                else:
                    claimed_p_long = 1.0 - est.p_yes
                    won = oc.no_won
                # Build a synthetic CalibrationEntry directly (we don't
                # want to fake a PositionState).
                entry = CalibrationEntry(
                    ts_resolved=datetime.now(UTC),
                    token_id=m.condition_id,  # synthetic
                    entry_strategy="backtest.fundamentals",
                    claimed_p_long=float(claimed_p_long),
                    confidence=float(est.confidence),
                    settle_price=1.0 if won else 0.0,
                    won=won,
                    pnl_usd=0.0,
                    base_rate=float(est.rationale.get("base_rate"))
                        if est.rationale.get("base_rate") is not None else None,
                )
                store._entries.append(entry)  # type: ignore[attr-defined]
                return {
                    "question": m.question[:120],
                    "category": m.category,
                    "claimed_p_long": round(claimed_p_long, 3),
                    "confidence": round(est.confidence, 3),
                    "won": won,
                    "rationale": str(est.rationale.get("rationale", ""))[:120],
                }

        results = await asyncio.gather(
            *[_process_one(m, oc) for m, oc in candidates],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, dict):
                samples.append(r)

        finished = datetime.now(UTC)
        metrics = store.metrics()
        # Approximate cost from resolver usage (triage path only since
        # cache TTL=1 means every call is fresh; deep tier may have
        # also fired when edge > 0.10).
        u = self._resolver.get_usage()
        summary = LLMBacktestRunSummary(
            started_at=started,
            finished_at=finished,
            n_markets_attempted=len(candidates),
            n_markets_scored=metrics.n_entries,
            n_skipped_voided=n_voided,
            n_skipped_no_estimate=n_no_est,
            brier_score=metrics.brier_score,
            accuracy=metrics.accuracy,
            mean_confidence=metrics.mean_confidence,
            mean_p_long=metrics.mean_p_long,
            bucket_accuracy=metrics.bucket_accuracy,
            bucket_counts=metrics.bucket_counts,
            triage_model=u.get("last_reset_date", ""),
            cost_usd_estimate=float(u.get("spend_usd_today", 0.0)),
            sample_predictions=samples[:20],  # cap UI payload
        )
        log.info(
            "llm_backtest.done",
            n_scored=summary.n_markets_scored,
            brier=summary.brier_score,
            accuracy=summary.accuracy,
            cost=summary.cost_usd_estimate,
        )
        return summary, store

    # ---------- internals ----------

    async def _fetch_resolved_markets(self, *, limit: int) -> list[dict[str, Any]]:
        """Pull a page of closed markets from Gamma. Newest first via
        order=endDate+ascending=false."""
        params = {
            "closed": "true",
            "limit": min(limit, 500),
            "order": "endDate",
            "ascending": "false",
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(f"{self.GAMMA_BASE}/markets", params=params)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            return []
        return data

    def _extract_outcome(self, raw: dict[str, Any]) -> _Outcome:
        """Parse Gamma's outcomePrices into a typed Outcome.
        ["1","0"] = YES won, ["0","1"] = NO won, ["0","0"] = voided."""
        op = raw.get("outcomePrices") or "[]"
        try:
            if isinstance(op, str):
                op = json.loads(op)
            if isinstance(op, list) and len(op) >= 2:
                yes_p = float(op[0])
                no_p = float(op[1])
                if yes_p == 0 and no_p == 0:
                    return _Outcome(False, False, voided=True)
                return _Outcome(yes_p >= 0.999, no_p >= 0.999, voided=False)
        except Exception:
            pass
        return _Outcome(False, False, voided=True)
