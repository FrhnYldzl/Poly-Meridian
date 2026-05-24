"""In-memory cosine similarity matcher for OpenAI / Voyage embeddings.

Used when pgvector is not available (Railway stock Postgres) but OpenAI
key is set. Computes market question embeddings once, caches in memory,
then per article computes cosine similarity in Python.

Lighter than pgvector for our scale (~few thousand active markets, ~1.5K
dimensions). Recomputes when market set changes.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import structlog

from poly_meridian.sentiment.embeddings import EmbeddingsBackend

log = structlog.get_logger("poly_meridian.sentiment.inmem_matcher")


class InMemoryMatcher:
    """Caches market embeddings in memory, matches articles via cosine."""

    def __init__(self, embeddings: EmbeddingsBackend) -> None:
        self._embeddings = embeddings
        # condition_id -> (text_hash, vector, market_meta dict)
        self._cache: dict[str, tuple[str, list[float], dict[str, Any]]] = {}

    async def refresh_markets(self, markets: list[dict[str, Any]]) -> int:
        """Embed any market whose question changed since last refresh.

        Returns the number of new/updated embeddings.
        """
        to_embed: list[tuple[str, str, dict[str, Any]]] = []  # (cid, question, meta)
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            q = (m.get("question") or m.get("title") or "").strip()
            if not cid or not q:
                continue
            h = self._embeddings.text_hash(q)
            cached = self._cache.get(cid)
            if cached is not None and cached[0] == h:
                continue
            to_embed.append((cid, q, m))

        if not to_embed:
            return 0

        for chunk_start in range(0, len(to_embed), 100):
            chunk = to_embed[chunk_start : chunk_start + 100]
            texts = [q for _, q, _ in chunk]
            try:
                vectors = await self._embeddings.embed(texts)
            except Exception as exc:
                log.warning("inmem_matcher.embed_failed", error=str(exc), n=len(texts))
                return chunk_start
            for (cid, q, meta), vec in zip(chunk, vectors, strict=True):
                h = self._embeddings.text_hash(q)
                self._cache[cid] = (h, vec, meta)

        log.info("inmem_matcher.markets_embedded", n=len(to_embed), total_cached=len(self._cache))
        return len(to_embed)

    async def match_article(
        self,
        article_title: str,
        *,
        k: int = 5,
        min_similarity: float = 0.4,
    ) -> list[dict[str, Any]]:
        """Embed article, return top-K markets by cosine similarity."""
        if not article_title or not self._cache:
            return []
        try:
            vectors = await self._embeddings.embed([article_title])
        except Exception as exc:
            log.warning("inmem_matcher.article_embed_failed", error=str(exc))
            return []
        if not vectors:
            return []
        q_vec = vectors[0]

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for cid, (_h, m_vec, meta) in self._cache.items():
            sim = _cosine(q_vec, m_vec)
            if sim < min_similarity:
                continue
            scored.append((sim, cid, meta))

        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[dict[str, Any]] = []
        for sim, cid, meta in scored[:k]:
            out.append(
                {
                    "condition_id": cid,
                    "question": meta.get("question") or meta.get("title"),
                    "category": meta.get("category"),
                    "yes_token_id": meta.get("clobTokenIds")
                    or meta.get("yes_token_id"),
                    "no_token_id": meta.get("no_token_id"),
                    "similarity": sim,
                }
            )
        return out


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0:
        return 0.0
    return dot / denom
