"""SQLAlchemy 2.0 async ORM models — mirror MASTER_SPEC §12 schema."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    condition_id: Mapped[str] = mapped_column(String, primary_key=True)
    question: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String)
    sub_category: Mapped[str | None] = mapped_column(String)
    event_id: Mapped[str | None] = mapped_column(String)
    yes_token_id: Mapped[str] = mapped_column(String, nullable=False)
    no_token_id: Mapped[str] = mapped_column(String, nullable=False)
    end_date_iso: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    liquidity_num: Mapped[Decimal | None] = mapped_column(Numeric)
    volume_num: Mapped[Decimal | None] = mapped_column(Numeric)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_markets_active", "active", "closed", "end_date_iso"),
        Index("idx_markets_event", "event_id"),
    )


class OrderbookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    token_id: Mapped[str] = mapped_column(String, primary_key=True)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric)
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric)
    mid: Mapped[Decimal | None] = mapped_column(Numeric)
    microprice: Mapped[Decimal | None] = mapped_column(Numeric)
    bid_depth_5pct: Mapped[Decimal | None] = mapped_column(Numeric)
    ask_depth_5pct: Mapped[Decimal | None] = mapped_column(Numeric)
    raw_levels: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Trade(Base):
    __tablename__ = "trades"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    token_id: Mapped[str] = mapped_column(String, primary_key=True)
    side: Mapped[str] = mapped_column(String, primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    size: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    maker_address: Mapped[str | None] = mapped_column(String)
    taker_address: Mapped[str | None] = mapped_column(String)
    tx_hash: Mapped[str | None] = mapped_column(String, primary_key=True)
    is_ours: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint("side IN ('BUY','SELL')", name="trades_side_check"),
    )


class NewsArticle(Base):
    __tablename__ = "news_articles"

    article_id: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    # embedding handled via raw SQL (pgvector column type not in core SA).
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class NewsSignal(Base):
    __tablename__ = "news_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    article_id: Mapped[str] = mapped_column(ForeignKey("news_articles.article_id"), nullable=False)
    condition_id: Mapped[str] = mapped_column(ForeignKey("markets.condition_id"), nullable=False)
    sentiment: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    impact: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint("direction IN ('YES','NO','NEUTRAL')", name="news_signals_dir_check"),
    )


class SmartWallet(Base):
    __tablename__ = "smart_wallets"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str | None] = mapped_column(String)
    lifetime_pnl: Mapped[Decimal | None] = mapped_column(Numeric)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeatureSnapshot(Base):
    __tablename__ = "feature_snapshots"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    token_id: Mapped[str] = mapped_column(String, primary_key=True)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class StrategySignalRow(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    condition_id: Mapped[str] = mapped_column(String, nullable=False)
    token_id: Mapped[str] = mapped_column(String, nullable=False)
    edge: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    conviction: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    suggested_action: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class OurOrder(Base):
    __tablename__ = "our_orders"

    order_id: Mapped[str] = mapped_column(String, primary_key=True)
    ts_created: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ts_filled: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    token_id: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    order_type: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric)
    size: Mapped[Decimal | None] = mapped_column(Numeric)
    filled_size: Mapped[Decimal] = mapped_column(Numeric, default=Decimal(0), nullable=False)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric)
    status: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)


class PositionRow(Base):
    __tablename__ = "positions"

    token_id: Mapped[str] = mapped_column(String, primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    last_mark: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PnLDaily(Base):
    __tablename__ = "pnl_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    starting_nav: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    ending_nav: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    realized: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unrealized: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False)
