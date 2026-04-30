"""add kyc routing enforcement toggle

Revision ID: 20260430_010000_add_kyc_routing_enforcement_toggle
Revises: 20260430_000000_add_kyc_routing_controls
Create Date: 2026-04-30 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260430_010000_add_kyc_routing_enforcement_toggle"
down_revision = "20260430_000000_add_kyc_routing_controls"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "kyc_routing_enforcement_enabled" not in _column_names("dashboard_settings"):
        op.add_column(
            "dashboard_settings",
            sa.Column(
                "kyc_routing_enforcement_enabled",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if "kyc_routing_enforcement_enabled" in _column_names("dashboard_settings"):
        op.drop_column("dashboard_settings", "kyc_routing_enforcement_enabled")
