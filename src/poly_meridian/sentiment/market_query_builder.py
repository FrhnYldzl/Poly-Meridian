"""Build category-aware GDELT queries from active Polymarket markets.

Instead of generic queries (politics OR election), extract the actual
keywords from each active market's question and build OR'd queries per
category. This way news ingestion is biased toward markets we trade.

Example output:
  Politics → "trump OR vance OR shutdown OR republican OR democrat"
  Crypto   → "bitcoin OR ethereum OR solana OR ETF OR halving"
  Sports   → "lakers OR celtics OR chiefs OR psg OR madrid"
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from poly_meridian.sentiment.keyword_match import extract_keywords


# GDELT URL length is bounded ~ 2KB per request; cap OR-terms per query.
MAX_TERMS_PER_QUERY = 12
MAX_QUERIES_PER_CYCLE = 16


def build_queries_from_markets(
    markets: list[dict[str, Any]],
    *,
    max_terms_per_query: int = MAX_TERMS_PER_QUERY,
    max_queries: int = MAX_QUERIES_PER_CYCLE,
) -> list[str]:
    """Group active markets by category, extract top keywords per group,
    return a GDELT query for each category."""
    if not markets:
        return []

    by_category: dict[str, list[str]] = {}
    for m in markets:
        cat = (m.get("category") or m.get("sub_category") or "Other").strip()
        q = (m.get("question") or m.get("title") or "").strip()
        if not q:
            continue
        keywords = extract_keywords(q, min_len=4, max_keywords=6)
        by_category.setdefault(cat, []).extend(keywords)

    out: list[str] = []
    for cat, kws in by_category.items():
        # Rank by frequency across markets in this category — most relevant
        # words appear in multiple markets.
        ranked = [w for w, _ in Counter(kws).most_common(max_terms_per_query)]
        if not ranked:
            continue
        out.append(" OR ".join(ranked))
        if len(out) >= max_queries:
            break
    return out
