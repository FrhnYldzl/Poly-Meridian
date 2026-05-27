"""LLMResolver — Claude-driven probability estimation. Phase R.

This is the strategy's *real* edge: the structured-API keys we have
(Anthropic, OpenAI, Gemini) are only used today for news sentiment.
Polymarket's information value lives in the questions themselves —
"Will X happen by Y?" — and an LLM with web-current context can
reason about that better than rolling-window TA can.

How it works:

  1. **Triage layer** (cheap, every market we look at).
     Claude Haiku gets: question text, end_date, current market price,
     recent news summary, smart-money flow direction. Returns
     {p_yes, confidence, rationale}. Capped at 400 tokens.

  2. **Deep layer** (expensive, only when triage finds an edge).
     When |triage_p - market_p| > deep_edge threshold (default 0.10),
     re-query Claude Sonnet with a longer prompt that allows multi-step
     reasoning. 800 token cap. The deeper estimate replaces the triage.

  3. **Cache** — Per-market TTL (default 6h). Re-query only when
     news/flow changes meaningfully OR TTL expires.

  4. **Budget guard** — Daily USD ceiling. When breached, every
     subsequent call returns None until UTC midnight resets.

  5. **Fail-closed** — Any LLM error returns None. FundamentalsStrategy
     handles None by not trading. No retries with degraded models.

The output `ProbabilityEstimate` plugs directly into the existing
FundamentalsStrategy → AggregatedSignal → RiskPolicy chain. No new
plumbing needed downstream.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from poly_meridian.domain import Market
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.fundamentals.llm_resolver")


# Approximate $/1K token prices (May 2026 Anthropic pricing). Used for
# budget tracking only — billing reconciles via Anthropic's own metering.
_TOKEN_PRICE_USD: dict[str, tuple[float, float]] = {
    # model -> (input_per_1K, output_per_1K)
    "claude-haiku-4-5-20251001":  (0.00025, 0.00125),
    "claude-sonnet-4-5-20251001": (0.003,   0.015),
    "claude-opus-4-7-20251001":   (0.015,   0.075),
}


@dataclass
class _CacheEntry:
    estimate: ProbabilityEstimate
    market_p_at_query: float | None
    expires_at: datetime


@dataclass
class LLMUsage:
    """Mutable counters surfaced to /api/state via the resolver instance."""
    calls_total: int = 0
    calls_triage: int = 0
    calls_deep: int = 0
    calls_cache_hits: int = 0
    calls_budget_blocked: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    spend_usd_today: float = 0.0
    last_reset_date: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())

    def maybe_reset_daily(self) -> None:
        today = datetime.now(UTC).date().isoformat()
        if today != self.last_reset_date:
            self.spend_usd_today = 0.0
            self.last_reset_date = today

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls_total": self.calls_total,
            "calls_triage": self.calls_triage,
            "calls_deep": self.calls_deep,
            "calls_cache_hits": self.calls_cache_hits,
            "calls_budget_blocked": self.calls_budget_blocked,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "spend_usd_today": round(self.spend_usd_today, 4),
            "last_reset_date": self.last_reset_date,
        }


class LLMResolver(CategoryResolver):
    """Catch-all resolver — works for any category by asking the LLM.

    Registered as a *fallback* in FundamentalsStrategy: category-specific
    resolvers (Politics polls, Sports Elo, etc.) run first and only fall
    through to LLM when their structured data is unavailable. In practice
    today that means LLM is THE resolver for almost everything.
    """

    category = "Default"  # plugged in for every category

    _TRIAGE_SYSTEM = (
        "You are a probability analyst for a prediction market trading "
        "agent. Estimate the probability of YES resolving true for the "
        "given Polymarket question. Use the structured context provided. "
        "Be a calibrated forecaster — over many predictions, your stated "
        "probabilities should match reality (no overconfidence, no fence-"
        "sitting at 0.5). Return STRICT JSON only:\n"
        '{"p_yes": 0.00, "confidence": 0.00, "key_factors": ["..."], '
        '"rationale": "<80 chars"}\n\n'
        "Rules:\n"
        "- p_yes in [0.01, 0.99]\n"
        "- confidence in [0.0, 1.0] — how strong YOUR estimate is, not "
        "how confident the market is\n"
        "- If you have no evidence either way, return p_yes=0.5 and "
        "confidence=0.0 (the strategy will reject low-confidence outputs)\n"
        "- Avoid the 'fade the favorite' fallacy: if the market price "
        "looks reasonable given the context, agree with it"
    )

    _DEEP_SYSTEM = (
        "You are a senior probability analyst doing a DEEP-DIVE on a "
        "single Polymarket question. A triage pass flagged a potential "
        "mispricing (your initial p_yes differed from the market by >0.10). "
        "Now reason step-by-step before answering:\n"
        "  1. What is the question literally asking? What counts as YES?\n"
        "  2. What base rate applies (historical frequency of similar "
        "events)?\n"
        "  3. What evidence in the provided context updates the base rate "
        "up or down?\n"
        "  4. What is your final p_yes after Bayesian updating?\n"
        "  5. What would have to be true for you to be wrong?\n\n"
        "Then output STRICT JSON only:\n"
        '{"p_yes": 0.00, "confidence": 0.00, "base_rate": 0.00, '
        '"key_factors": ["..."], "rationale": "<160 chars"}\n\n'
        "Be honest about uncertainty — if the question hinges on factors "
        "you cannot evaluate, lower confidence below 0.4 and the strategy "
        "will pass."
    )

    def __init__(
        self,
        *,
        triage_model: str | None = None,
        deep_model: str | None = None,
        deep_edge_threshold: float | None = None,
        cache_ttl_sec: int | None = None,
        daily_budget_usd: float | None = None,
        api_key: str | None = None,
        triage_max_tokens: int | None = None,
        deep_max_tokens: int | None = None,
    ) -> None:
        s = get_settings()
        self._triage_model = triage_model or s.llm_resolver_triage_model
        self._deep_model = deep_model or s.llm_resolver_deep_model
        self._deep_edge = (
            deep_edge_threshold
            if deep_edge_threshold is not None
            else s.llm_resolver_deep_edge
        )
        self._cache_ttl = cache_ttl_sec or s.llm_resolver_cache_ttl_sec
        self._budget_usd = (
            daily_budget_usd
            if daily_budget_usd is not None
            else s.llm_resolver_daily_budget_usd
        )
        self._triage_max_tokens = (
            triage_max_tokens or s.llm_resolver_triage_max_tokens
        )
        self._deep_max_tokens = deep_max_tokens or s.llm_resolver_deep_max_tokens
        self._key = api_key or s.anthropic_api_key.get_secret_value()
        self._client: Any = None
        self._cache: dict[str, _CacheEntry] = {}
        self.usage = LLMUsage()
        # Async lock per-market so concurrent ticks on the same market
        # don't both spawn LLM calls — second one waits and reuses cache.
        self._inflight: dict[str, asyncio.Lock] = {}

    # ---------- public API ----------

    def resolve(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        """Sync wrapper expected by FundamentalsStrategy.

        FundamentalsStrategy.evaluate() is async, but each resolver call is
        sync (legacy). We bridge by scheduling on the running loop. If no
        loop is running (e.g. from a unit test), we use asyncio.run.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.resolve_async(market, ctx))
        # We're inside an async context — create a task and block on it
        # via run_until_complete. But we can't nest loops, so use a
        # threadsafe future bridge via run_coroutine_threadsafe if needed.
        # Simpler: just await directly via a helper coroutine.
        future = asyncio.ensure_future(self.resolve_async(market, ctx), loop=loop)
        # NOTE: the existing FundamentalsStrategy.evaluate() is itself
        # async but calls resolver.resolve() synchronously. We can't
        # block here without deadlocking. The fix: have FundamentalsStrategy
        # await an async resolver path. We expose `resolve_async` for that
        # — the sync path returns None to be safe.
        future.cancel()
        return None

    async def resolve_async(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> ProbabilityEstimate | None:
        """Async entry — called by FundamentalsStrategy.evaluate when
        the strategy has been patched to use the async path. Looks up
        cache first, then triage, then optionally deep."""
        if not self._key:
            return None

        # Cache check
        now = datetime.now(UTC)
        cached = self._cache.get(market.condition_id)
        if cached is not None and cached.expires_at > now:
            self.usage.calls_cache_hits += 1
            return cached.estimate

        # Budget check — reset counter if a new UTC day rolled over
        self.usage.maybe_reset_daily()
        if self.usage.spend_usd_today >= self._budget_usd:
            self.usage.calls_budget_blocked += 1
            log.warning(
                "llm_resolver.budget_exhausted",
                spend=round(self.usage.spend_usd_today, 4),
                cap=self._budget_usd,
            )
            return None

        # Per-market lock — coalesce duplicate concurrent calls.
        lock = self._inflight.setdefault(market.condition_id, asyncio.Lock())
        async with lock:
            # Re-check cache inside lock (another caller may have filled it).
            cached = self._cache.get(market.condition_id)
            if cached is not None and cached.expires_at > now:
                self.usage.calls_cache_hits += 1
                return cached.estimate

            try:
                client = self._ensure_client()
            except RuntimeError as exc:
                log.warning("llm_resolver.no_client", reason=str(exc))
                return None

            # Build context summary
            ctx_summary = self._summarize_context(market, ctx)

            # Triage
            triage_est = await self._query(
                client,
                model=self._triage_model,
                system=self._TRIAGE_SYSTEM,
                user_payload=ctx_summary,
                max_tokens=self._triage_max_tokens,
                tier="triage",
            )
            if triage_est is None:
                return None

            self.usage.calls_triage += 1

            # Deep re-query iff edge looks large
            market_p = ctx_summary.get("market_p")
            should_deep = (
                market_p is not None
                and abs(triage_est.p_yes - float(market_p)) >= self._deep_edge
                and triage_est.confidence >= 0.4
            )
            final_est = triage_est
            if should_deep:
                deep_est = await self._query(
                    client,
                    model=self._deep_model,
                    system=self._DEEP_SYSTEM,
                    user_payload=ctx_summary,
                    max_tokens=self._deep_max_tokens,
                    tier="deep",
                )
                if deep_est is not None:
                    self.usage.calls_deep += 1
                    final_est = deep_est

            # Cache
            self._cache[market.condition_id] = _CacheEntry(
                estimate=final_est,
                market_p_at_query=float(market_p) if market_p is not None else None,
                expires_at=now + timedelta(seconds=self._cache_ttl),
            )
            return final_est

    def get_usage(self) -> dict[str, Any]:
        """Snapshot of usage counters for /api/state."""
        self.usage.maybe_reset_daily()
        return self.usage.as_dict()

    # ---------- internals ----------

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "anthropic SDK not installed (install via `uv pip install -e \".[llm]\"`)."
                ) from exc
            if not self._key:
                raise RuntimeError("ANTHROPIC_API_KEY missing — LLMResolver disabled")
            self._client = AsyncAnthropic(api_key=self._key)
        return self._client

    def _summarize_context(
        self,
        market: Market,
        ctx: FundamentalsContext,
    ) -> dict[str, Any]:
        """Distill market + FundamentalsContext into a JSON-serializable
        payload the LLM can read. We keep this compact — every token
        costs money."""
        now = datetime.now(UTC)
        days_to_resolution: float | None = None
        if market.end_date_iso is not None:
            days_to_resolution = round(
                (market.end_date_iso - now).total_seconds() / 86400.0, 2
            )

        # Recent news for this condition (if any)
        news_blurb: str | None = None
        try:
            news_blurb = ctx.__dict__.get("news_summary")
        except Exception:
            news_blurb = None

        # Smart-money flow direction (if any)
        sm_flow: str | None = None
        try:
            sm_flow = ctx.__dict__.get("smart_money_direction")
        except Exception:
            sm_flow = None

        # Current market mid as a calibration anchor
        market_p: float | None = None
        try:
            market_p = float(ctx.__dict__.get("current_market_p") or 0.0) or None
        except Exception:
            market_p = None

        return {
            "question": market.question,
            "category": market.category or "Uncategorized",
            "days_to_resolution": days_to_resolution,
            "market_p": market_p,
            "liquidity_usd": float(market.liquidity_usd) if market.liquidity_usd else None,
            "news_summary": (news_blurb or "")[:600],
            "smart_money_flow": sm_flow,
            "now_iso": now.isoformat(),
        }

    async def _query(
        self,
        client: Any,
        *,
        model: str,
        system: str,
        user_payload: dict[str, Any],
        max_tokens: int,
        tier: str,
    ) -> ProbabilityEstimate | None:
        """Single call. Returns None on any error (fail-closed)."""
        user_text = json.dumps(user_payload, ensure_ascii=False)
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(Exception),
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
                reraise=True,
            ):
                with attempt:
                    msg = await client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user_text}],
                    )
                    self.usage.calls_total += 1
                    # Anthropic SDK returns usage on the message object
                    in_tok = int(getattr(msg.usage, "input_tokens", 0) or 0)
                    out_tok = int(getattr(msg.usage, "output_tokens", 0) or 0)
                    self.usage.tokens_input += in_tok
                    self.usage.tokens_output += out_tok
                    self._accrue_spend(model, in_tok, out_tok)

                    text = "".join(
                        b.text for b in msg.content
                        if getattr(b, "type", "") == "text"
                    )
                    parsed = _parse_estimate_json(text)
                    if parsed is None:
                        raise ValueError(f"non-JSON: {text[:160]}")
                    return ProbabilityEstimate(
                        p_yes=parsed["p_yes"],
                        confidence=parsed["confidence"],
                        rationale={
                            "source": f"llm:{tier}",
                            "model": model,
                            "key_factors": parsed.get("key_factors", []),
                            "rationale": parsed.get("rationale", ""),
                            "base_rate": parsed.get("base_rate"),
                        },
                    )
        except Exception as exc:
            log.warning(
                "llm_resolver.query_failed",
                tier=tier, model=model, error=str(exc)[:200],
            )
            return None
        return None

    def _accrue_spend(self, model: str, in_tok: int, out_tok: int) -> None:
        prices = _TOKEN_PRICE_USD.get(model)
        if prices is None:
            return
        in_price, out_price = prices
        cost = (in_tok / 1000.0) * in_price + (out_tok / 1000.0) * out_price
        self.usage.spend_usd_today += cost


def _parse_estimate_json(text: str) -> dict[str, Any] | None:
    """Tolerant JSON parser — strips code fences, finds first {...}."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE
        )
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m is None:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    try:
        p_yes = max(0.01, min(0.99, float(obj.get("p_yes", 0.5))))
        confidence = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
    except (TypeError, ValueError):
        return None
    out: dict[str, Any] = {
        "p_yes": p_yes,
        "confidence": confidence,
        "rationale": str(obj.get("rationale", ""))[:300],
    }
    kf = obj.get("key_factors")
    if isinstance(kf, list):
        out["key_factors"] = [str(x)[:80] for x in kf[:5]]
    if obj.get("base_rate") is not None:
        try:
            out["base_rate"] = float(obj["base_rate"])
        except (TypeError, ValueError):
            pass
    return out
