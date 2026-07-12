"""add account primary drain priority flag

Revision ID: 20260615_000000_add_account_primary_drain_priority
Revises: 20260613_000000_make_high_waterline_default_routing
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260615_000000_add_account_primary_drain_priority"
down_revision = "20260613_000000_make_high_waterline_default_routing"
branch_labels = None
depends_on = None


def _table_exists(connection: Connection, table_name: str) -> bool:
    inspector = sa.inspect(connection)
    return inspector.has_table(table_name)


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "accounts"):
        return
    columns = _columns(bind, "accounts")
    if "primary_drain_priority_enabled" in columns:
        return
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "primary_drain_priority_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "accounts"):
        return
    columns = _columns(bind, "accounts")
    if "primary_drain_priority_enabled" not in columns:
        return
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("primary_drain_priority_enabled")
