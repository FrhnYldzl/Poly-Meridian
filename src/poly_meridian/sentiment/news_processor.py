"""News processor — orchestrates the article → signals pipeline.

Per cycle (e.g. every 5 min):
  1. Pull N unprocessed news_articles.
  2. Embed titles in one batch.
  3. Update each article's embedding column.
  4. For each article, find top-K markets by cosine similarity.
  5. Score each (article, market) pair via sentiment scorer.
  6. Write news_signals + mark article processed.

Market embeddings are populated lazily by `embed_markets_if_stale()` — call
once after Gamma sync to keep the vector store in sync.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from prometheus_client import Counter

from poly_meridian.sentiment.embeddings import EmbeddingsBackend
from poly_meridian.sentiment.scorer import SentimentScorer
from poly_meridian.storage.db import Database
from poly_meridian.storage.writers import (
    fetch_unprocessed_articles,
    find_top_k_markets_for_article,
    insert_news_signal,
    mark_article_processed,
    market_needs_embedding,
    set_news_embedding,
    upsert_market_embedding,
)

log = structlog.get_logger("poly_meridian.sentiment.news_processor")

PM_NEWS_PROCESSED = Counter(
    "pm_news_processed_total", "news articles fully processed into signals"
)
PM_NEWS_SIGNAL_EMITTED = Counter(
    "pm_news_signal_emitted_total", "news_signals rows written", ["direction"]
)


class NewsProcessor:
    """Orchestrates the article → signals pipeline.

    Two operating modes:
    - **Vector mode** (embeddings != None): OpenAI/Voyage embeddings + pgvector
      cosine search. High precision news→market matching.
    - **Keyword mode** (embeddings is None): tokenize + Postgres ILIKE search
      against markets.question. Lower precision but works without an
      embeddings provider (Anthropic-only setups).
    """

    def __init__(
        self,
        *,
        embeddings: EmbeddingsBackend | None,
        scorer: SentimentScorer,
        top_k: int = 5,
        min_similarity: float = 0.4,
        min_impact_for_signal: float = 0.1,
    ) -> None:
        self._embeddings = embeddings
        self._scorer = scorer
        self._top_k = top_k
        self._min_similarity = min_similarity
        self._min_impact = min_impact_for_signal

    @property
    def mode(self) -> str:
        return "vector" if self._embeddings is not None else "keyword"

    async def embed_markets_if_stale(
        self,
        db: Database,
        markets: list[dict[str, Any]],
    ) -> int:
        """Compute + cache embeddings for markets whose question changed.

        No-op in keyword mode — markets are matched via ILIKE on question text.
        """
        if self._embeddings is None:
            return 0
        to_embed: list[tuple[str, str, str]] = []  # (condition_id, question, hash)
        for row in markets:
            cid = row.get("conditionId") or row.get("condition_id")
            q = (row.get("question") or row.get("title") or "").strip()
            if not cid or not q:
                continue
            text_hash = self._embeddings.text_hash(q)
            try:
                needs = await market_needs_embedding(db, condition_id=cid, text_hash=text_hash)
            except Exception as exc:
                log.warning("news.market_emb.check_failed", error=str(exc))
                return 0
            if needs:
                to_embed.append((cid, q, text_hash))

        if not to_embed:
            return 0

        # Batch in groups of 100 — OpenAI accepts ~2048 max per request, but we
        # stay conservative.
        for chunk_start in range(0, len(to_embed), 100):
            chunk = to_embed[chunk_start : chunk_start + 100]
            texts = [q for _, q, _ in chunk]
            try:
                vectors = await self._embeddings.embed(texts)
            except Exception as exc:
                log.warning("news.market_emb.embed_failed", error=str(exc), n=len(texts))
                return chunk_start
            for (cid, _, h), vec in zip(chunk, vectors, strict=True):
                try:
                    await upsert_market_embedding(
                        db, condition_id=cid, embedding=vec, text_hash=h
                    )
                except Exception as exc:
                    log.warning("news.market_emb.write_failed", error=str(exc), cid=cid)

        log.info("news.market_emb.done", n=len(to_embed))
        return len(to_embed)

    async def process_unprocessed(self, db: Database, *, batch: int = 25) -> int:
        articles = await fetch_unprocessed_articles(db, limit=batch)
        if not articles:
            return 0

        if self._embeddings is not None:
            # ---- VECTOR MODE: embed + pgvector cosine match ----
            titles = [a.get("title") or "" for a in articles]
            try:
                vectors = await self._embeddings.embed(titles)
            except Exception as exc:
                log.warning("news.embed_failed", error=str(exc), n=len(titles))
                return 0

            for art, vec in zip(articles, vectors, strict=True):
                try:
                    await set_news_embedding(db, article_id=art["article_id"], embedding=vec)
                except Exception as exc:
                    log.warning("news.emb_write_failed", error=str(exc), aid=art["article_id"])

        # ---- For each article, match to markets + score sentiment ----
        processed = 0
        for art in articles:
            try:
                matches = await self._match_markets(db, art)
            except Exception as exc:
                log.warning("news.match_failed", error=str(exc), aid=art["article_id"])
                continue

            for m in matches:
                try:
                    result = await self._scorer.score(
                        article_title=art.get("title") or "",
                        article_body=art.get("body"),
                        market_question=m["question"],
                    )
                except Exception as exc:
                    log.warning("news.score_failed", error=str(exc), aid=art["article_id"])
                    continue

                if result.impact < self._min_impact or result.direction == "NEUTRAL":
                    continue

                try:
                    await insert_news_signal(
                        db,
                        ts=datetime.now(UTC),
                        article_id=art["article_id"],
                        condition_id=m["condition_id"],
                        sentiment=result.sentiment,
                        impact=result.impact,
                        direction=result.direction,
                    )
                    PM_NEWS_SIGNAL_EMITTED.labels(direction=result.direction).inc()
                except Exception as exc:
                    log.warning(
                        "news.signal_write_failed",
                        error=str(exc),
                        aid=art["article_id"],
                        cid=m["condition_id"],
                    )

            try:
                await mark_article_processed(db, art["article_id"])
                processed += 1
                PM_NEWS_PROCESSED.inc()
            except Exception as exc:
                log.warning("news.mark_failed", error=str(exc), aid=art["article_id"])

        log.info("news.processed", n=processed, mode=self.mode)
        return processed

    async def _match_markets(
        self, db: Database, article: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Dispatch matching by mode. Vector when embeddings present, else keyword."""
        if self._embeddings is not None:
            return await find_top_k_markets_for_article(
                db,
                article_id=article["article_id"],
                k=self._top_k,
                min_similarity=self._min_similarity,
            )
        # Keyword fallback path — extract tokens, ILIKE search.
        from poly_meridian.sentiment.keyword_match import (
            extract_keywords,
            find_markets_by_keyword,
        )

        title = article.get("title") or ""
        keywords = extract_keywords(title)
        if not keywords:
            return []
        return await find_markets_by_keyword(db, keywords=keywords, k=self._top_k)
