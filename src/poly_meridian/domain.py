"""Domain primitives shared across modules.

Single source of truth for the typed objects that flow between ingestion,
features, strategies, risk, execution, and portfolio. See MASTER_SPEC §14, §15.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Action(StrEnum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


class Mode(StrEnum):
    PAPER = "paper"
    LIVE_CONSERVATIVE = "live-conservative"
    LIVE_NORMAL = "live-normal"
    KILL = "kill"


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    condition_id: str
    question: str
    category: str | None = None
    sub_category: str | None = None
    event_id: str | None = None
    yes_token_id: str
    no_token_id: str
    end_date_iso: datetime | None = None
    active: bool = True
    closed: bool = False
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    ts: datetime
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal(2)


class Features(BaseModel):
    """Feature vector emitted per tick per token. Keys are feature names."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    token_id: str
    values: dict[str, float]


class StrategySignal(BaseModel):
    """Output of a single strategy's `evaluate()` call. See §14."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    strategy: str
    condition_id: str
    token_id: str
    edge: float                   # our_p − market_p
    conviction: float             # 0..1
    suggested_action: Action
    rationale: dict[str, Any] = Field(default_factory=dict)


class AggregatedSignal(BaseModel):
    """Aggregator output that flows into RiskPolicy. See §14.6."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    condition_id: str
    token_id: str
    direction: Action
    edge: float
    conviction: float
    size_pct: float               # fraction of bankroll proposed
    proposed_price: Decimal | None = None  # price the strategy wants to trade at
    category: str | None = None
    market_liquidity_usd: float | None = None
    contributors: list[str] = Field(default_factory=list)
    # Phase N.4: arbitrage partner-leg metadata, lifted by the aggregator from
    # the contributing strategy's rationale and passed through to the router.
    paired_token: str | None = None
    paired_price: Decimal | None = None
    paired_side: Action | None = None


class TradeDecision(BaseModel):
    """Risk-approved order intent that flows into Executor."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    strategy: str
    token_id: str
    side: Side
    order_type: OrderType
    price: Decimal | None
    size: Decimal
    # Phase N.4 — optional second leg for arbitrage. When set, the OrderRouter
    # submits both legs CONCURRENTLY. Single-leg arb is unhedged directional,
    # which is what BUG #5 was: ArbitrageStrategy emitted only the YES side,
    # the router dropped the NO partner, and the "arb" became a long-YES bet.
    paired_token: str | None = None
    paired_price: Decimal | None = None
    paired_side: Side | None = None


class Order(BaseModel):
    order_id: str
    ts_created: datetime
    ts_filled: datetime | None = None
    strategy: str
    token_id: str
    side: Side
    order_type: OrderType
    price: Decimal | None
    size: Decimal
    filled_size: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    status: OrderStatus = OrderStatus.PENDING
    mode: Mode = Mode.PAPER


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    qty: Decimal
    avg_cost: Decimal
    last_mark: Decimal
    last_updated: datetime


class PortfolioSnapshot(BaseModel):
    """Snapshot the RiskPolicy evaluates against."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    nav_usd: Decimal
    cash_usd: Decimal
    positions: list[Position]
    daily_pnl_pct: float
    total_exposure_pct: float
    category_exposure_pct: dict[str, float] = Field(default_factory=dict)
    open_position_count: int = 0
