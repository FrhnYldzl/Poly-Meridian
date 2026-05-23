"""Sentiment subsystem — embeddings, scoring, news processor. See §13 + §14.2."""
from poly_meridian.sentiment.embeddings import EmbeddingsBackend, OpenAIEmbeddings
from poly_meridian.sentiment.scorer import (
    ClaudeSentimentScorer,
    HeuristicSentimentScorer,
    SentimentResult,
    SentimentScorer,
)

__all__ = [
    "ClaudeSentimentScorer",
    "EmbeddingsBackend",
    "HeuristicSentimentScorer",
    "OpenAIEmbeddings",
    "SentimentResult",
    "SentimentScorer",
]
