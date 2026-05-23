"""Sentiment scoring — pluggable LLM backend.

A SentimentResult carries:
  - sentiment ∈ [-1, 1]   negative ↔ positive
  - impact    ∈  [0, 1]   how market-moving the article is
  - direction ∈ {YES, NO, NEUTRAL}  with respect to the matched market

The scorer takes (article, market) → SentimentResult. Strategies later
aggregate these into a per-market signal.
"""
from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.sentiment.scorer")


@dataclass(frozen=True)
class SentimentResult:
    sentiment: float            # -1..1
    impact: float               #  0..1
    direction: str              # YES | NO | NEUTRAL
    rationale: str = ""

    def clip(self) -> "SentimentResult":
        return SentimentResult(
            sentiment=max(-1.0, min(1.0, self.sentiment)),
            impact=max(0.0, min(1.0, self.impact)),
            direction=self.direction if self.direction in ("YES", "NO", "NEUTRAL") else "NEUTRAL",
            rationale=self.rationale,
        )


class SentimentScorer(ABC):
    @abstractmethod
    async def score(
        self,
        *,
        article_title: str,
        article_body: str | None,
        market_question: str,
    ) -> SentimentResult: ...


class HeuristicSentimentScorer(SentimentScorer):
    """Lightweight keyword-based scorer. Never calls a network. Useful for
    unit tests and as a fallback when LLM API is unreachable.

    NOTE: This is intentionally crude. Production uses Claude.
    """

    _POS = re.compile(
        r"\b(rises?|wins?|approved|surges?|rallies|gains?|breakthrough|bullish|"
        r"upgrade|beats?|growth|positive)\b",
        re.IGNORECASE,
    )
    _NEG = re.compile(
        r"\b(falls?|loses?|declines?|crash(?:es)?|drops?|bearish|downgrade|"
        r"misses?|slump|recession|negative|warns?)\b",
        re.IGNORECASE,
    )

    async def score(
        self,
        *,
        article_title: str,
        article_body: str | None,
        market_question: str,
    ) -> SentimentResult:
        text = f"{article_title or ''} {article_body or ''}"
        pos = len(self._POS.findall(text))
        neg = len(self._NEG.findall(text))
        if pos == 0 and neg == 0:
            return SentimentResult(0.0, 0.1, "NEUTRAL", "no_keywords")
        sentiment = (pos - neg) / max(pos + neg, 1)
        impact = min(1.0, (pos + neg) * 0.1)
        direction = "YES" if sentiment > 0.2 else "NO" if sentiment < -0.2 else "NEUTRAL"
        return SentimentResult(sentiment, impact, direction, "heuristic")


class ClaudeSentimentScorer(SentimentScorer):
    """Production scorer — Anthropic Claude. Cheap, fast, structured output.

    The prompt asks Claude to return strict JSON:
      {"sentiment": float, "impact": float, "direction": "YES"|"NO"|"NEUTRAL",
       "rationale": "..."}.
    Falls back to HeuristicSentimentScorer if the API call fails after retries.
    """

    _SYSTEM_PROMPT = (
        "You are a financial sentiment analyst evaluating news articles for a "
        "prediction-market trading agent. Score each (article, market) pair on "
        "three axes:\n"
        "  sentiment: float in [-1, 1] — negative vs positive tone toward the YES "
        "outcome of the market\n"
        "  impact: float in [0, 1] — how market-moving the article is (0=trivial, "
        "1=major catalyst)\n"
        "  direction: \"YES\" | \"NO\" | \"NEUTRAL\" — which side the article "
        "supports relative to the market question\n"
        "Return STRICT JSON only, no prose:\n"
        '{"sentiment": 0.0, "impact": 0.0, "direction": "NEUTRAL", "rationale": "..."}'
    )

    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        s = get_settings()
        self._model = model or s.sentiment_model
        self._key = api_key or s.anthropic_api_key.get_secret_value()
        self._client: object | None = None
        self._fallback = HeuristicSentimentScorer()

    def _ensure_client(self) -> object:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Anthropic client not installed. Install via `uv pip install -e \".[llm]\"`."
                ) from exc
            if not self._key:
                raise RuntimeError("ANTHROPIC_API_KEY not set; falling back to heuristic.")
            self._client = AsyncAnthropic(api_key=self._key)
        return self._client

    async def score(
        self,
        *,
        article_title: str,
        article_body: str | None,
        market_question: str,
    ) -> SentimentResult:
        try:
            client = self._ensure_client()
        except RuntimeError as exc:
            log.warning("scorer.fallback_heuristic", reason=str(exc))
            return await self._fallback.score(
                article_title=article_title,
                article_body=article_body,
                market_question=market_question,
            )

        user = (
            f"Market question: {market_question}\n\n"
            f"Article title: {article_title}\n"
            f"Article body: {(article_body or '')[:1500]}"
        )

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(Exception),
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
                reraise=True,
            ):
                with attempt:
                    msg = await client.messages.create(  # type: ignore[attr-defined]
                        model=self._model,
                        max_tokens=200,
                        system=self._SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user}],
                    )
                    text = "".join(
                        block.text for block in msg.content if getattr(block, "type", "") == "text"
                    )
                    parsed = _parse_json_result(text)
                    if parsed is None:
                        raise ValueError(f"non-JSON response: {text[:120]}")
                    return SentimentResult(**parsed).clip()
        except Exception as exc:
            log.warning("scorer.claude_failed", error=str(exc))
            return await self._fallback.score(
                article_title=article_title,
                article_body=article_body,
                market_question=market_question,
            )
        return SentimentResult(0.0, 0.0, "NEUTRAL", "unreachable")


def _parse_json_result(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction — tolerates code fences / preamble."""
    text = text.strip()
    # Strip markdown fences.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first {...} block.
        m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if m is None:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    return {
        "sentiment": float(obj.get("sentiment", 0.0)),
        "impact": float(obj.get("impact", 0.0)),
        "direction": str(obj.get("direction", "NEUTRAL")).upper(),
        "rationale": str(obj.get("rationale", ""))[:300],
    }


async def score_many(
    scorer: SentimentScorer,
    items: list[tuple[str, str | None, str]],
    *,
    concurrency: int = 5,
) -> list[SentimentResult]:
    """Batch scoring with bounded concurrency. items = [(title, body, question), ...]"""
    sem = asyncio.Semaphore(concurrency)

    async def _one(title: str, body: str | None, q: str) -> SentimentResult:
        async with sem:
            return await scorer.score(article_title=title, article_body=body, market_question=q)

    return await asyncio.gather(*(_one(t, b, q) for t, b, q in items))
