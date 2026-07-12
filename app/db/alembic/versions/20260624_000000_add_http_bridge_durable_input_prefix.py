"""add HTTP bridge durable input prefix metadata

Revision ID: 20260624_000000_add_http_bridge_durable_input_prefix
Revises: 20260619_030000_remove_mail_account_auth_type
Create Date: 2026-06-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260624_000000_add_http_bridge_durable_input_prefix"
down_revision = "20260619_030000_remove_mail_account_auth_type"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = _columns(bind, "http_bridge_sessions")
    if not existing_columns:
        return
    with op.batch_alter_table("http_bridge_sessions") as batch_op:
        if "latest_input_item_count" not in existing_columns:
            batch_op.add_column(sa.Column("latest_input_item_count", sa.Integer(), nullable=True))
        if "latest_input_full_fingerprint" not in existing_columns:
            batch_op.add_column(sa.Column("latest_input_full_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing_columns = _columns(bind, "http_bridge_sessions")
    if not existing_columns:
        return
    with op.batch_alter_table("http_bridge_sessions") as batch_op:
        if "latest_input_full_fingerprint" in existing_columns:
            batch_op.drop_column("latest_input_full_fingerprint")
        if "latest_input_item_count" in existing_columns:
            batch_op.drop_column("latest_input_item_count")
