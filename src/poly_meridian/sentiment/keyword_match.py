"""Keyword-based fallback for news → market matching.

Used when no embeddings backend is configured (e.g. Anthropic-only setups).
Less precise than vector cosine similarity but works without OpenAI / Voyage.

Algorithm:
  1. Tokenize article title (alphanumeric, lowercase, strip stopwords)
  2. Keep tokens with length ≥ 4 (filters out common short words)
  3. Query DB for active markets whose question contains ≥1 of these tokens
  4. Rank by overlap count (more matched tokens → higher score)
  5. Return top-K
"""
from __future__ import annotations

import re
from typing import Any

STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "had", "her", "was", "one", "our", "out", "day", "get", "has", "him",
    "his", "how", "man", "new", "now", "old", "see", "two", "way", "who",
    "boy", "did", "its", "let", "put", "say", "she", "too", "use", "will",
    "from", "with", "this", "that", "have", "they", "what", "when", "your",
    "more", "than", "into", "about", "after", "their", "would", "could",
    "should", "there", "these", "those", "which", "while", "where", "many",
    "some", "year", "years", "month", "today", "weeks", "weekly", "daily",
    "says", "said", "say", "told", "tells", "amid", "over", "than", "just",
    "back", "report", "reports", "news", "update", "latest",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def extract_keywords(text: str, *, min_len: int = 4, max_keywords: int = 12) -> list[str]:
    """Pull useful tokens out of an article title. Lowercased + dedupd."""
    tokens = [t.lower() for t in _TOKEN_RE.findall(text or "")]
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if len(t) < min_len:
            continue
        if t in STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_keywords:
            break
    return out


async def find_markets_by_keyword(
    db: Any,
    *,
    keywords: list[str],
    k: int = 5,
) -> list[dict[str, Any]]:
    """Postgres-side OR'd ILIKE search across markets.question.

    Returns matches with a `similarity` score derived from how many
    keywords each market's question contains (range 0..1).
    """
    if not keywords:
        return []

    # Build the ILIKE OR clauses with positional params.
    # WHERE question ILIKE $1 OR question ILIKE $2 OR ...
    clauses = " OR ".join(f"question ILIKE ${i + 1}" for i in range(len(keywords)))
    params = [f"%{kw}%" for kw in keywords]

    # The CASE expression counts how many keywords matched, normalized to [0, 1].
    score_terms = " + ".join(
        f"CASE WHEN question ILIKE ${i + 1} THEN 1 ELSE 0 END"
        for i in range(len(keywords))
    )
    score_sql = f"({score_terms})::float / {len(keywords)}::float"

    sql = f"""
        SELECT
            condition_id, question, category,
            yes_token_id, no_token_id,
            {score_sql} AS similarity
        FROM markets
        WHERE active = TRUE AND closed = FALSE
          AND ({clauses})
        ORDER BY similarity DESC, volume_num DESC NULLS LAST
        LIMIT ${len(keywords) + 1}
    """

    async with db.acquire() as conn:
        rows = await conn.fetch(sql, *params, k)
        return [dict(r) for r in rows]
