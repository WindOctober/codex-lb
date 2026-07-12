"""add mail account auth type

Revision ID: 20260619_020000_add_mail_account_auth_type
Revises: 20260619_010000_fix_mail_message_desc_indexes
Create Date: 2026-06-19 02:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260619_020000_add_mail_account_auth_type"
down_revision = "20260619_010000_fix_mail_message_desc_indexes"
branch_labels = None
depends_on = None


def _table_exists(connection: Connection, table_name: str) -> bool:
    return sa.inspect(connection).has_table(table_name)


def _column_exists(connection: Connection, table_name: str, column_name: str) -> bool:
    columns = sa.inspect(connection).get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "mail_accounts") or _column_exists(bind, "mail_accounts", "auth_type"):
        return
    op.add_column(
        "mail_accounts",
        sa.Column("auth_type", sa.String(), server_default="imap_password", nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "mail_accounts") and _column_exists(bind, "mail_accounts", "auth_type"):
        op.drop_column("mail_accounts", "auth_type")
