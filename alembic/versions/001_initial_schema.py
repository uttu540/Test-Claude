"""Initial schema — all tables as of Phase 4

Revision ID: 001
Revises: None
Create Date: 2026-04-08

NOTE FOR EXISTING DATABASES:
  If you ran `create_all` before Alembic was set up, mark this migration
  as already applied WITHOUT running it:

    alembic stamp 001

  Then run newer migrations normally:

    alembic upgrade head

  For fresh databases, just run:

    alembic upgrade head
"""
from __future__ import annotations
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── instruments ──────────────────────────────────────────────────────────
    op.create_table(
        "instruments",
        sa.Column("id",              sa.BigInteger(),  primary_key=True, autoincrement=True),
        sa.Column("trading_symbol",  sa.String(50),    nullable=False),
        sa.Column("exchange",        sa.String(10),    nullable=False),
        sa.Column("instrument_type", sa.String(20),    nullable=False),
        sa.Column("company_name",    sa.String(200)),
        sa.Column("isin",            sa.String(20)),
        sa.Column("lot_size",        sa.Integer(),     nullable=False, server_default="1"),
        sa.Column("tick_size",       sa.Numeric(10, 2)),
        sa.Column("sector",          sa.String(100)),
        sa.Column("industry",        sa.String(100)),
        sa.Column("kite_token",      sa.BigInteger()),
        sa.Column("is_nifty50",      sa.Boolean(),     nullable=False, server_default="false"),
        sa.Column("is_nifty500",     sa.Boolean(),     nullable=False, server_default="false"),
        sa.Column("is_active",       sa.Boolean(),     nullable=False, server_default="true"),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at",      sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("trading_symbol", "exchange", name="uq_symbol_exchange"),
    )

    # ── trades (must be before orders due to FK) ──────────────────────────────
    op.create_table(
        "trades",
        sa.Column("id",                  postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trading_symbol",      sa.String(50),  nullable=False),
        sa.Column("exchange",            sa.String(10),  nullable=False),
        sa.Column("instrument_id",       sa.BigInteger(), sa.ForeignKey("instruments.id")),
        sa.Column("instrument_type",     sa.String(20),  nullable=False),
        sa.Column("direction",           sa.String(5),   nullable=False),
        sa.Column("strategy_name",       sa.String(100)),
        sa.Column("strategy_mode",       sa.String(30)),
        sa.Column("broker",              sa.String(20),  nullable=False),
        sa.Column("entry_order_id",      postgresql.UUID(as_uuid=True)),
        sa.Column("entry_price",         sa.Numeric(12, 4), nullable=False),
        sa.Column("entry_quantity",      sa.Integer(),   nullable=False),
        sa.Column("entry_time",          sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_order_id",       postgresql.UUID(as_uuid=True)),
        sa.Column("exit_price",          sa.Numeric(12, 4)),
        sa.Column("exit_quantity",       sa.Integer()),
        sa.Column("exit_time",           sa.DateTime(timezone=True)),
        sa.Column("exit_reason",         sa.String(50)),
        sa.Column("gross_pnl",           sa.Numeric(14, 4)),
        sa.Column("brokerage",           sa.Numeric(10, 4)),
        sa.Column("stt",                 sa.Numeric(10, 4)),
        sa.Column("exchange_charges",    sa.Numeric(10, 4)),
        sa.Column("gst",                 sa.Numeric(10, 4)),
        sa.Column("sebi_charges",        sa.Numeric(10, 4)),
        sa.Column("stamp_duty",          sa.Numeric(10, 4)),
        sa.Column("net_pnl",             sa.Numeric(14, 4)),
        sa.Column("planned_stop_loss",   sa.Numeric(12, 4)),
        sa.Column("planned_target_1",    sa.Numeric(12, 4)),
        sa.Column("planned_target_2",    sa.Numeric(12, 4)),
        sa.Column("initial_risk_amount", sa.Numeric(12, 4)),
        sa.Column("risk_reward_planned", sa.Numeric(6, 2)),
        sa.Column("risk_reward_actual",  sa.Numeric(6, 2)),
        sa.Column("r_multiple",          sa.Numeric(6, 2)),
        sa.Column("mae",                 sa.Numeric(12, 4)),
        sa.Column("mfe",                 sa.Numeric(12, 4)),
        sa.Column("ai_confidence",       sa.Numeric(4, 2)),
        sa.Column("ai_reasoning",        sa.Text()),
        sa.Column("signals_at_entry",    postgresql.JSONB()),
        sa.Column("market_regime",       sa.String(30)),
        sa.Column("news_sentiment",      sa.Numeric(4, 2)),
        sa.Column("fundamental_score",   sa.Numeric(5, 2)),
        sa.Column("status",              sa.String(20),  nullable=False, server_default="OPEN"),
        sa.Column("mode",                sa.String(20),  nullable=False, server_default="live"),
        sa.Column("created_at",          sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at",          sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_trades_entry_time",     "trades", ["entry_time"])
    op.create_index("ix_trades_status",         "trades", ["status"])
    op.create_index("ix_trades_trading_symbol", "trades", ["trading_symbol"])

    # ── orders ────────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id",               postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("broker",           sa.String(20),  nullable=False),
        sa.Column("broker_order_id",  sa.String(100)),
        sa.Column("instrument_id",    sa.BigInteger(), sa.ForeignKey("instruments.id")),
        sa.Column("trading_symbol",   sa.String(50),  nullable=False),
        sa.Column("exchange",         sa.String(10),  nullable=False),
        sa.Column("transaction_type", sa.String(5),   nullable=False),
        sa.Column("order_type",       sa.String(10),  nullable=False),
        sa.Column("product",          sa.String(10),  nullable=False),
        sa.Column("quantity",         sa.Integer(),   nullable=False),
        sa.Column("price",            sa.Numeric(12, 4)),
        sa.Column("trigger_price",    sa.Numeric(12, 4)),
        sa.Column("status",           sa.String(20),  nullable=False),
        sa.Column("filled_quantity",  sa.Integer(),   nullable=False, server_default="0"),
        sa.Column("average_price",    sa.Numeric(12, 4)),
        sa.Column("validity",         sa.String(10),  nullable=False, server_default="DAY"),
        sa.Column("variety",          sa.String(20),  nullable=False, server_default="regular"),
        sa.Column("tag",              sa.String(30)),
        sa.Column("parent_trade_id",  postgresql.UUID(as_uuid=True), sa.ForeignKey("trades.id")),
        sa.Column("placed_at",        sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at",       sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("exchange_timestamp", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("raw_response",     postgresql.JSONB()),
    )
    op.create_index("ix_orders_placed_at",      "orders", ["placed_at"])
    op.create_index("ix_orders_status",         "orders", ["status"])
    op.create_index("ix_orders_trading_symbol", "orders", ["trading_symbol"])

    # ── daily_pnl ─────────────────────────────────────────────────────────────
    op.create_table(
        "daily_pnl",
        sa.Column("id",                   sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("trading_date",         sa.Date(),  nullable=False, unique=True),
        sa.Column("total_trades",         sa.Integer(), nullable=False, server_default="0"),
        sa.Column("winning_trades",       sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losing_trades",        sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gross_pnl",            sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("total_charges",        sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("net_pnl",              sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("win_rate",             sa.Numeric(5, 2)),
        sa.Column("avg_r_multiple",       sa.Numeric(6, 2)),
        sa.Column("max_drawdown_intraday", sa.Numeric(12, 4)),
        sa.Column("market_regime",        sa.String(30)),
        sa.Column("notes",                sa.Text()),
        sa.Column("created_at",           sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

    # ── ai_decision_log ───────────────────────────────────────────────────────
    op.create_table(
        "ai_decision_log",
        sa.Column("id",             postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("decision_type",  sa.String(50),  nullable=False),
        sa.Column("input_context",  postgresql.JSONB()),
        sa.Column("raw_response",   sa.Text()),
        sa.Column("parsed_output",  postgresql.JSONB()),
        sa.Column("model_used",     sa.String(50)),
        sa.Column("input_tokens",   sa.Integer()),
        sa.Column("output_tokens",  sa.Integer()),
        sa.Column("latency_ms",     sa.Integer()),
        sa.Column("trade_id",       postgresql.UUID(as_uuid=True)),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_ai_log_created_at", "ai_decision_log", ["created_at"])

    # ── news_items ────────────────────────────────────────────────────────────
    op.create_table(
        "news_items",
        sa.Column("id",              postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trading_symbol",  sa.String(50)),
        sa.Column("headline",        sa.String(500), nullable=False),
        sa.Column("summary",         sa.Text()),
        sa.Column("url",             sa.String(1000)),
        sa.Column("source",          sa.String(100)),
        sa.Column("published_at",    sa.DateTime(timezone=True)),
        sa.Column("sentiment_score", sa.Numeric(4, 2)),
        sa.Column("sentiment_label", sa.String(20)),
        sa.Column("key_events",      postgresql.JSONB()),
        sa.Column("processed",       sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("url", name="uq_news_url"),
    )
    op.create_index("ix_news_published_at",    "news_items", ["published_at"])
    op.create_index("ix_news_trading_symbol",  "news_items", ["trading_symbol"])


def downgrade() -> None:
    op.drop_table("news_items")
    op.drop_table("ai_decision_log")
    op.drop_table("daily_pnl")
    op.drop_table("orders")
    op.drop_table("trades")
    op.drop_table("instruments")
