"""StubEmbeddings deterministic behavior — used to test news_processor without
real network."""
from __future__ import annotations

import pytest

from poly_meridian.sentiment.embeddings import EmbeddingsBackend, StubEmbeddings


@pytest.mark.asyncio
async def test_embed_returns_correct_dim() -> None:
    e = StubEmbeddings(dimensions=128)
    out = await e.embed(["hello", "world"])
    assert len(out) == 2
    assert all(len(v) == 128 for v in out)


@pytest.mark.asyncio
async def test_same_input_same_vector() -> None:
    e = StubEmbeddings(dimensions=64)
    a = (await e.embed(["foo"]))[0]
    b = (await e.embed(["foo"]))[0]
    assert a == b


@pytest.mark.asyncio
async def test_different_inputs_different_vectors() -> None:
    e = StubEmbeddings(dimensions=64)
    a = (await e.embed(["foo"]))[0]
    b = (await e.embed(["bar"]))[0]
    assert a != b


def test_text_hash_deterministic() -> None:
    h1 = EmbeddingsBackend.text_hash("hello world")
    h2 = EmbeddingsBackend.text_hash("hello world")
    assert h1 == h2
    assert h1 != EmbeddingsBackend.text_hash("different")
