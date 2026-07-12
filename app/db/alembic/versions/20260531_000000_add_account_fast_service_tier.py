"""add account fast service tier toggle

Revision ID: 20260531_000000_add_account_fast_service_tier
Revises: 20260502_000000_convert_kyc_only_keys_to_group
Create Date: 2026-05-31 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260531_000000_add_account_fast_service_tier"
down_revision = "20260502_000000_convert_kyc_only_keys_to_group"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "fast_service_tier_enabled" not in _column_names("accounts"):
        op.add_column(
            "accounts",
            sa.Column("fast_service_tier_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    if "fast_service_tier_enabled" in _column_names("accounts"):
        op.drop_column("accounts", "fast_service_tier_enabled")
