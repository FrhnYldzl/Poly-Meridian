"""Embeddings — pluggable backend (OpenAI default).

Used by `news_processor` to embed article titles + market questions for
top-K cosine-similarity matching in pgvector.
"""
from __future__ import annotations

import asyncio
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Sequence

import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.embeddings")


class EmbeddingsBackend(ABC):
    """Contract for embedding providers. Always returns float lists."""

    dimensions: int

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


class OpenAIEmbeddings(EmbeddingsBackend):
    """OpenAI text-embedding-3-small by default (1536-d)."""

    dimensions = 1536

    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        s = get_settings()
        self._model = model or s.embedding_model
        self._key = api_key or s.openai_api_key.get_secret_value()
        self._client: object | None = None
        if self._model == "text-embedding-3-large":
            self.dimensions = 3072
        elif self._model == "text-embedding-3-small":
            self.dimensions = 1536

    def _ensure_client(self) -> object:
        if self._client is None:
            try:
                from openai import AsyncOpenAI  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "OpenAI client not installed. Install via `uv pip install -e \".[llm]\"`."
                ) from exc
            if not self._key:
                raise RuntimeError("OPENAI_API_KEY not set; cannot embed.")
            self._client = AsyncOpenAI(api_key=self._key)
        return self._client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._ensure_client()
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(Exception),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
            reraise=True,
        ):
            with attempt:
                resp = await client.embeddings.create(  # type: ignore[attr-defined]
                    model=self._model,
                    input=list(texts),
                )
                vectors = [d.embedding for d in resp.data]
                log.debug("embeddings.batch", n=len(vectors), model=self._model)
                return [list(v) for v in vectors]
        return []


class StubEmbeddings(EmbeddingsBackend):
    """Deterministic fake embeddings for unit tests. Hash → fixed vector."""

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        await asyncio.sleep(0)
        out: list[list[float]] = []
        for t in texts:
            h = self.text_hash(t)
            seed = int(h[:16], 16)
            vec = [((seed >> (i % 60)) & 0xFF) / 255.0 for i in range(self.dimensions)]
            mag = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / mag for v in vec])
        return out
