"""Add mode column to trades table

Revision ID: 002
Revises: 001
Create Date: 2026-04-08

Adds `mode` column (development / paper / semi-auto / live) to trades.
Existing rows default to 'live' via server_default.
"""
from __future__ import annotations
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use IF NOT EXISTS — migration 001 already includes this column in its
    # CREATE TABLE, so fresh installs don't need it added again.
    op.execute(
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS mode VARCHAR(20) NOT NULL DEFAULT 'live'"
    )


def downgrade() -> None:
    op.drop_column("trades", "mode")
