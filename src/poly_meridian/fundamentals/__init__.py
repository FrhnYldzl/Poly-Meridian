"""Fundamentals — category-specific probability models. See MASTER_SPEC §14.5.

Each category (Politics, Sports, Crypto, Macro) has a `CategoryResolver` that
turns market context into a probability estimate. The `FundamentalsStrategy`
dispatches markets to the right resolver and emits signals when our
probability diverges from the market price.

The framework is data-source-agnostic — resolvers accept a typed `Context`
object (polls, Elo ratings, funding rates, calendar events) that the main
loop or backtest harness populates from any source.
"""
from poly_meridian.fundamentals.base import (
    CategoryResolver,
    FundamentalsContext,
    ProbabilityEstimate,
)
from poly_meridian.fundamentals.crypto import CryptoResolver
from poly_meridian.fundamentals.llm_resolver import LLMResolver
from poly_meridian.fundamentals.macro import MacroResolver
from poly_meridian.fundamentals.politics import PoliticsResolver
from poly_meridian.fundamentals.sports import EloEngine, SportsResolver

__all__ = [
    "CategoryResolver",
    "CryptoResolver",
    "EloEngine",
    "FundamentalsContext",
    "LLMResolver",
    "MacroResolver",
    "PoliticsResolver",
    "ProbabilityEstimate",
    "SportsResolver",
]
