"""add kyc routing controls

Revision ID: 20260430_000000_add_kyc_routing_controls
Revises: 20260428_000000_add_news_items
Create Date: 2026-04-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260430_000000_add_kyc_routing_controls"
down_revision = "20260428_000000_add_news_items"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    api_key_columns = _column_names("api_keys")
    if "kyc_only" not in api_key_columns:
        op.add_column(
            "api_keys",
            sa.Column("kyc_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    if "kyc_only" in _column_names("api_keys"):
        op.drop_column("api_keys", "kyc_only")
