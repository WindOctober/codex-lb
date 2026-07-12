"""add account subscription renewal timestamp

Revision ID: 20260618_000000_add_account_subscription_renews_at
Revises: 20260615_000000_add_account_primary_drain_priority
Create Date: 2026-06-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260618_000000_add_account_subscription_renews_at"
down_revision = "20260615_000000_add_account_primary_drain_priority"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "accounts")
    if not columns or "subscription_renews_at" in columns:
        return
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(sa.Column("subscription_renews_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "accounts")
    if "subscription_renews_at" not in columns:
        return
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("subscription_renews_at")
