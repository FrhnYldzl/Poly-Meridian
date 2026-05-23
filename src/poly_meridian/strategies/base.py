"""BaseStrategy ABC. See MASTER_SPEC §14."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from poly_meridian.domain import Features, Market, StrategySignal


class BaseStrategy(ABC):
    """Contract every sub-strategy implements.

    Sub-strategies live under `poly_meridian.strategies.*` and are wired
    into the aggregator at startup. Each must declare its name, a config
    dict (loaded from `config/strategies/<name>.yaml`), and an enabled
    flag.
    """

    name: str
    enabled: bool
    config: dict[str, Any]

    def __init__(self, *, name: str, config: dict[str, Any], enabled: bool = True) -> None:
        self.name = name
        self.config = config
        self.enabled = enabled

    @abstractmethod
    async def evaluate(self, market: Market, features: Features) -> StrategySignal | None:
        """Return a signal for this (market, features) tuple, or None to pass."""

    @abstractmethod
    def capacity_estimate(self) -> float:
        """Estimated daily USD capacity for this strategy at current AUM."""
