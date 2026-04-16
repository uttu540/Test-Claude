"""
database/models.py
──────────────────
SQLAlchemy ORM models for every core table.
TimescaleDB hypertables (OHLCV) are created via migration, not ORM.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.connection import Base


# ─── Instruments ──────────────────────────────────────────────────────────────

class Instrument(Base):
    """Master list of all tradeable instruments (NSE/BSE equities)."""
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("trading_symbol", "exchange", name="uq_symbol_exchange"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trading_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)          # NSE, BSE
    instrument_type: Mapped[str] = mapped_column(String(20), nullable=False)   # EQ, INDEX
    company_name: Mapped[str | None] = mapped_column(String(200))
    isin: Mapped[str | None] = mapped_column(String(20))
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    sector: Mapped[str | None] = mapped_column(String(100))
    industry: Mapped[str | None] = mapped_column(String(100))
    kite_token: Mapped[int | None] = mapped_column(BigInteger)                 # Zerodha instrument token
    is_nifty50: Mapped[bool] = mapped_column(Boolean, default=False)
    is_nifty500: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="instrument")


# ─── Orders ───────────────────────────────────────────────────────────────────

class Order(Base):
    """Every order attempt, one row per order (including rejected ones)."""
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_placed_at", "placed_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_trading_symbol", "trading_symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker: Mapped[str] = mapped_column(String(20), nullable=False)            # ZERODHA, GROWW, PAPER
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    instrument_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("instruments.id"))
    trading_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(5), nullable=False)   # BUY, SELL
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)        # MARKET, LIMIT, SL, SL-M
    product: Mapped[str] = mapped_column(String(10), nullable=False)           # CNC, MIS, NRML
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[str] = mapped_column(String(20), nullable=False)            # PENDING, OPEN, COMPLETE, CANCELLED, REJECTED
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    validity: Mapped[str] = mapped_column(String(10), default="DAY")
    variety: Mapped[str] = mapped_column(String(20), default="regular")
    tag: Mapped[str | None] = mapped_column(String(30))                        # Strategy identifier (SEBI requirement)
    parent_trade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("trades.id"))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)


# ─── Trades ───────────────────────────────────────────────────────────────────

class Trade(Base):
    """
    A completed trade = entry order + exit order pair.
    Created when entry order fills; updated when exit occurs.
    """
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_entry_time", "entry_time"),
        Index("ix_trades_status", "status"),
        Index("ix_trades_trading_symbol", "trading_symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trading_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("instruments.id"))
    instrument_type: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(5), nullable=False)          # LONG, SHORT
    strategy_name: Mapped[str | None] = mapped_column(String(100))
    strategy_mode: Mapped[str | None] = mapped_column(String(30))              # INTRADAY, SWING, POSITIONAL
    broker: Mapped[str] = mapped_column(String(20), nullable=False)

    # Entry
    entry_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    entry_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Exit
    exit_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    exit_quantity: Mapped[int | None] = mapped_column(Integer)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(String(50))               # TARGET, STOP_LOSS, TRAILING_STOP, MANUAL, TIME_EXIT

    # P&L
    gross_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    brokerage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    stt: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    exchange_charges: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    gst: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sebi_charges: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    stamp_duty: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    net_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))

    # Risk metrics
    planned_stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    planned_target_1: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    planned_target_2: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    initial_risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    risk_reward_planned: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    risk_reward_actual: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    mae: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))               # Max Adverse Excursion (points against position)
    mfe: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))               # Max Favorable Excursion (points in favor)
    exit_slippage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))     # abs(actual_exit - planned_exit)
    exit_context: Mapped[dict | None] = mapped_column(JSONB)                  # regime at exit, tick at exit, planned exit

    # AI context (stored for every trade — regulatory audit trail)
    ai_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    ai_reasoning: Mapped[str | None] = mapped_column(Text)
    signals_at_entry: Mapped[dict | None] = mapped_column(JSONB)
    market_regime: Mapped[str | None] = mapped_column(String(30))
    news_sentiment: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    fundamental_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    status: Mapped[str] = mapped_column(String(20), default="OPEN")           # OPEN, CLOSED
    mode: Mapped[str] = mapped_column(String(20), default="live", server_default="live")  # development, paper, live
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    instrument: Mapped["Instrument"] = relationship("Instrument", back_populates="trades")


# ─── Daily P&L ────────────────────────────────────────────────────────────────

class DailyPnL(Base):
    """Aggregated P&L per trading day. Updated EOD."""
    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trading_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0)
    total_charges: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0)
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    avg_r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    max_drawdown_intraday: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    market_regime: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ─── AI Decision Log ──────────────────────────────────────────────────────────

class AIDecisionLog(Base):
    """
    Full audit trail of every Claude AI decision.
    SEBI compliance: every automated order must be traceable to an AI decision.
    """
    __tablename__ = "ai_decision_log"
    __table_args__ = (
        Index("ix_ai_log_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_type: Mapped[str] = mapped_column(String(50), nullable=False)    # STRATEGY_EVAL, SENTIMENT, MARKET_BRIEFING
    input_context: Mapped[dict | None] = mapped_column(JSONB)                 # What was sent to Claude
    raw_response: Mapped[str | None] = mapped_column(Text)                    # Claude's exact response
    parsed_output: Mapped[dict | None] = mapped_column(JSONB)                 # Structured output after parsing
    model_used: Mapped[str | None] = mapped_column(String(50))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))   # Links to trade if one was generated
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ─── News ─────────────────────────────────────────────────────────────────────

class SignalRejectionLog(Base):
    """
    Audit trail for every signal that was evaluated but NOT traded.
    Captures rejected signals from all three gates: Risk, AI, Approval.
    Essential for strategy tuning — tells you what the bot is filtering out.
    """
    __tablename__ = "signal_rejection_log"
    __table_args__ = (
        Index("ix_rejection_log_created_at", "created_at"),
        Index("ix_rejection_log_symbol",     "trading_symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trading_symbol:   Mapped[str]          = mapped_column(String(50), nullable=False)
    signal_type:      Mapped[str]          = mapped_column(String(50), nullable=False)
    direction:        Mapped[str]          = mapped_column(String(10), nullable=False)
    confidence:       Mapped[Decimal|None] = mapped_column(Numeric(5, 2))
    price_at_signal:  Mapped[Decimal|None] = mapped_column(Numeric(12, 4))
    indicators:       Mapped[dict|None]    = mapped_column(JSONB)
    timeframe:        Mapped[str|None]     = mapped_column(String(20))
    rejection_stage:  Mapped[str]          = mapped_column(String(20), nullable=False)  # RISK | AI | APPROVAL
    rejection_reason: Mapped[str|None]     = mapped_column(Text)
    market_regime:    Mapped[str|None]     = mapped_column(String(30))
    created_at:       Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now())


# ─── News ─────────────────────────────────────────────────────────────────────

class NewsItem(Base):
    """Ingested news articles with sentiment scores."""
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("url", name="uq_news_url"),
        Index("ix_news_published_at", "published_at"),
        Index("ix_news_trading_symbol", "trading_symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trading_symbol: Mapped[str | None] = mapped_column(String(50))            # NULL = market-wide news
    headline: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    source: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))    # -1.0 to +1.0
    sentiment_label: Mapped[str | None] = mapped_column(String(20))           # BULLISH, BEARISH, NEUTRAL
    key_events: Mapped[list | None] = mapped_column(JSONB)                    # Extracted events from Claude
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
