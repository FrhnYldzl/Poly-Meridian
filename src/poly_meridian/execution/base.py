"""Executor ABC. See MASTER_SPEC §16.

Two concrete implementations follow:
- `PaperExecutor` — simulates fills using local book replica, writes to DB
  with mode='paper'. Phase 2.
- `LiveExecutor` — submits to Polymarket CLOB via py-clob-client. Phase 6.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from poly_meridian.domain import Order, TradeDecision


class Executor(ABC):
    """Contract every executor implements."""

    mode: str

    @abstractmethod
    async def submit(self, decision: TradeDecision) -> Order:
        """Submit a risk-approved trade decision; return the resulting Order."""

    @abstractmethod
    async def cancel(self, order_id: str) -> bool:
        """Cancel a live order. Returns True if the cancel was accepted."""

    @abstractmethod
    async def reconcile(self) -> None:
        """Reconcile local order state with the venue (called periodically)."""
