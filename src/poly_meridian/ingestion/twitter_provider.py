"""Twitter/X v2 filtered stream provider. See §11.5.

Implementation deferred to Phase 3 (sentiment strategy lands then).
Skeleton present so wiring + config can be tested.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from poly_meridian.ingestion.base import IngestionSource


class TwitterStreamSource(IngestionSource):
    name = "twitter"

    async def start(self) -> None:
        raise NotImplementedError("Twitter stream lands in Phase 3. See MASTER_SPEC §11.5.")

    async def stop(self) -> None:
        return

    async def healthcheck(self) -> bool:
        return False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if False:  # pragma: no cover - placeholder generator
            yield {}
        return
