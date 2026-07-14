"""add request-log cache write tokens

Revision ID: 20260714_000000_add_request_log_cache_write_tokens
Revises: 20260713_000000_add_ignore_five_hour_limit
Create Date: 2026-07-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260714_000000_add_request_log_cache_write_tokens"
down_revision = "20260713_000000_add_ignore_five_hour_limit"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = _columns(bind, "request_logs")
    if not existing_columns or "cache_write_tokens" in existing_columns:
        return
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing_columns = _columns(bind, "request_logs")
    if "cache_write_tokens" not in existing_columns:
        return
    with op.batch_alter_table("request_logs") as batch_op:
        batch_op.drop_column("cache_write_tokens")
