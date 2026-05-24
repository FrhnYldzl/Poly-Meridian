"""Sentiment subsystem — embeddings, scoring, news processor. See §13 + §14.2."""
from poly_meridian.sentiment.embeddings import EmbeddingsBackend, OpenAIEmbeddings
from poly_meridian.sentiment.scorer import (
    ClaudeSentimentScorer,
    GeminiSentimentScorer,
    HeuristicSentimentScorer,
    SentimentResult,
    SentimentScorer,
)

__all__ = [
    "ClaudeSentimentScorer",
    "EmbeddingsBackend",
    "GeminiSentimentScorer",
    "HeuristicSentimentScorer",
    "OpenAIEmbeddings",
    "SentimentResult",
    "SentimentScorer",
]
