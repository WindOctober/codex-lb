"""add ignore five-hour limit dashboard setting

Revision ID: 20260713_000000_add_ignore_five_hour_limit
Revises: 20260624_000000_add_http_bridge_durable_input_prefix
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260713_000000_add_ignore_five_hour_limit"
down_revision = "20260624_000000_add_http_bridge_durable_input_prefix"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = _columns(bind, "dashboard_settings")
    if not existing_columns or "ignore_five_hour_limit" in existing_columns:
        return
    with op.batch_alter_table("dashboard_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ignore_five_hour_limit",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing_columns = _columns(bind, "dashboard_settings")
    if "ignore_five_hour_limit" not in existing_columns:
        return
    with op.batch_alter_table("dashboard_settings") as batch_op:
        batch_op.drop_column("ignore_five_hour_limit")
