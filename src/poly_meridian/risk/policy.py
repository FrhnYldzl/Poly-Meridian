"""RiskPolicy ABC. See MASTER_SPEC §15.4.

Every aggregated signal MUST pass through `RiskPolicy.evaluate()` before
reaching the executor. There is no bypass — this is enforced by the agent
main loop wiring, not by convention.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from poly_meridian.domain import AggregatedSignal, PortfolioSnapshot, TradeDecision


class RiskDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REDUCE = "reduce"


class RiskPolicy(ABC):
    """Contract for the risk gate that sits between aggregator and executor."""

    @abstractmethod
    def evaluate(
        self,
        signal: AggregatedSignal,
        portfolio: PortfolioSnapshot,
    ) -> RiskDecision:
        """Decide whether to approve, reject, or reduce the signal."""

    @abstractmethod
    def size(
        self,
        signal: AggregatedSignal,
        portfolio: PortfolioSnapshot,
    ) -> TradeDecision | None:
        """Translate an APPROVE/REDUCE decision into a sized TradeDecision."""

    @abstractmethod
    def is_kill_switch_engaged(self) -> bool:
        """Return True if the kill-switch blocks all new orders."""
