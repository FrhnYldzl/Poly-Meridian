"""Gamma REST client — mocked transport tests."""
from __future__ import annotations

import httpx
import pytest

from poly_meridian.ingestion.gamma_client import GammaClient


@pytest.mark.asyncio
async def test_list_active_markets_returns_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets"
        return httpx.Response(200, json=[{"conditionId": "x", "question": "q"}])

    transport = httpx.MockTransport(handler)
    c = GammaClient(base_url="https://test")
    await c.start()
    assert c._client is not None  # type: ignore[reportPrivateUsage]
    c._client._transport = transport  # type: ignore[reportPrivateUsage]
    rows = await c.list_active_markets()
    assert rows == [{"conditionId": "x", "question": "q"}]
    await c.stop()


@pytest.mark.asyncio
async def test_iter_paginates_until_short_page() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params).get("offset", 0))
        calls.append(offset)
        if offset == 0:
            return httpx.Response(200, json=[{"conditionId": str(i), "question": "q"} for i in range(500)])
        return httpx.Response(200, json=[{"conditionId": "last", "question": "q"}])

    transport = httpx.MockTransport(handler)
    c = GammaClient(base_url="https://test")
    await c.start()
    c._client._transport = transport  # type: ignore[reportPrivateUsage]
    rows = await c.iter_active_markets()
    assert len(rows) == 501
    assert calls == [0, 500]
    await c.stop()


@pytest.mark.asyncio
async def test_data_wrapper_envelope_handled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"conditionId": "y", "question": "q"}]})

    transport = httpx.MockTransport(handler)
    c = GammaClient(base_url="https://test")
    await c.start()
    c._client._transport = transport  # type: ignore[reportPrivateUsage]
    rows = await c.list_active_markets()
    assert rows == [{"conditionId": "y", "question": "q"}]
    await c.stop()
