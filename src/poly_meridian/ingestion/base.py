"""IngestionSource ABC. See MASTER_SPEC §11.

Each data source (Gamma REST, CLOB WebSocket, GDELT, X API, on-chain RPC)
implements this contract. The agent main loop owns the lifecycle and pipes
events through `normalize.py` into a unified event stream.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class IngestionSource(ABC):
    """Contract for any upstream data source."""

    name: str

    @abstractmethod
    async def start(self) -> None:
        """Open connections, warm caches, subscribe to streams."""

    @abstractmethod
    async def stop(self) -> None:
        """Cleanly shut down. Called on agent termination."""

    @abstractmethod
    def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized events as `{type, ts, payload, source}` dicts."""

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Return True if the source is currently healthy."""
