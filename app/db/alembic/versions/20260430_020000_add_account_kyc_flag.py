"""add account kyc flag

Revision ID: 20260430_020000_add_account_kyc_flag
Revises: 20260430_010000_add_kyc_routing_enforcement_toggle
Create Date: 2026-04-30 02:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260430_020000_add_account_kyc_flag"
down_revision = "20260430_010000_add_kyc_routing_enforcement_toggle"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    account_columns = _column_names("accounts")
    if "kyc_enabled" not in account_columns:
        op.add_column(
            "accounts",
            sa.Column("kyc_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        )

    settings_columns = _column_names("dashboard_settings")
    if "kyc_models_json" in settings_columns:
        # Remove the superseded model-list based KYC setting during upgrade.
        op.drop_column("dashboard_settings", "kyc_models_json")


def downgrade() -> None:
    settings_columns = _column_names("dashboard_settings")
    if "kyc_models_json" not in settings_columns:
        # Restore the superseded setting only for downgrade compatibility.
        op.add_column(
            "dashboard_settings",
            sa.Column("kyc_models_json", sa.Text(), server_default=sa.text("'[]'"), nullable=False),
        )

    account_columns = _column_names("accounts")
    if "kyc_enabled" in account_columns:
        op.drop_column("accounts", "kyc_enabled")
