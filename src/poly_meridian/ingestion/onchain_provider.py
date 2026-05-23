"""Polygon on-chain provider — smart wallet tracker. See §11.6.

Implementation deferred to Phase 3 (smart-money strategy lands then).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from poly_meridian.ingestion.base import IngestionSource


class PolygonOnchainSource(IngestionSource):
    name = "onchain"

    async def start(self) -> None:
        raise NotImplementedError("On-chain tracker lands in Phase 3. See MASTER_SPEC §11.6.")

    async def stop(self) -> None:
        return

    async def healthcheck(self) -> bool:
        return False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if False:  # pragma: no cover - placeholder generator
            yield {}
        return
