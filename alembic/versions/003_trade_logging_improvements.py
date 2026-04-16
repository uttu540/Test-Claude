"""Trade logging improvements — MAE/MFE, exit context, slippage, signal rejection log

Revision ID: 003
Revises: 002
Create Date: 2026-04-16

Changes:
  trades table:
    - exit_slippage  NUMERIC(10,4)  — abs(actual_exit - planned_exit)
    - exit_context   JSONB          — regime at exit, tick at exit, planned exit price

  New table:
    - signal_rejection_log — audit trail for every signal the bot evaluated but did NOT trade
      (stage = RISK | AI | APPROVAL, with full signal params and rejection reason)

Note: mae and mfe columns already exist from migration 001.
      They are now populated by trade_lifecycle.py on every trade close.
"""
from __future__ import annotations
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── trades: new logging columns ───────────────────────────────────────────
    op.add_column("trades", sa.Column("exit_slippage", sa.Numeric(10, 4), nullable=True))
    op.add_column("trades", sa.Column("exit_context",  postgresql.JSONB(),  nullable=True))

    # ── signal_rejection_log ──────────────────────────────────────────────────
    op.create_table(
        "signal_rejection_log",
        sa.Column("id",               postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trading_symbol",   sa.String(50),  nullable=False),
        sa.Column("signal_type",      sa.String(50),  nullable=False),
        sa.Column("direction",        sa.String(10),  nullable=False),
        sa.Column("confidence",       sa.Numeric(5, 2)),
        sa.Column("price_at_signal",  sa.Numeric(12, 4)),
        sa.Column("indicators",       postgresql.JSONB()),
        sa.Column("timeframe",        sa.String(20)),
        sa.Column("rejection_stage",  sa.String(20),  nullable=False),   # RISK | AI | APPROVAL
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("market_regime",    sa.String(30)),
        sa.Column("created_at",       sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("ix_rejection_log_created_at", "signal_rejection_log", ["created_at"])
    op.create_index("ix_rejection_log_symbol",     "signal_rejection_log", ["trading_symbol"])


def downgrade() -> None:
    op.drop_index("ix_rejection_log_symbol",     table_name="signal_rejection_log")
    op.drop_index("ix_rejection_log_created_at", table_name="signal_rejection_log")
    op.drop_table("signal_rejection_log")
    op.drop_column("trades", "exit_context")
    op.drop_column("trades", "exit_slippage")
