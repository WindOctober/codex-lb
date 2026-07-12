"""fix mail message descending indexes

Revision ID: 20260619_010000_fix_mail_message_desc_indexes
Revises: 20260619_000000_add_mail_inbox_tables
Create Date: 2026-06-19 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260619_010000_fix_mail_message_desc_indexes"
down_revision = "20260619_000000_add_mail_inbox_tables"
branch_labels = None
depends_on = None


def _table_exists(connection: Connection, table_name: str) -> bool:
    return sa.inspect(connection).has_table(table_name)


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "mail_messages"):
        return

    op.drop_index("idx_mail_messages_unread_received", table_name="mail_messages")
    op.drop_index("idx_mail_messages_focused_received", table_name="mail_messages")
    op.drop_index("idx_mail_messages_received", table_name="mail_messages")
    op.drop_index("idx_mail_messages_account_received", table_name="mail_messages")
    op.create_index(
        "idx_mail_messages_account_received",
        "mail_messages",
        ["account_id", sa.text("received_at DESC")],
    )
    op.create_index("idx_mail_messages_received", "mail_messages", [sa.text("received_at DESC")])
    op.create_index(
        "idx_mail_messages_focused_received",
        "mail_messages",
        ["focused", sa.text("received_at DESC")],
    )
    op.create_index(
        "idx_mail_messages_unread_received",
        "mail_messages",
        ["unread", sa.text("received_at DESC")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "mail_messages"):
        return

    op.drop_index("idx_mail_messages_unread_received", table_name="mail_messages")
    op.drop_index("idx_mail_messages_focused_received", table_name="mail_messages")
    op.drop_index("idx_mail_messages_received", table_name="mail_messages")
    op.drop_index("idx_mail_messages_account_received", table_name="mail_messages")
    op.create_index("idx_mail_messages_account_received", "mail_messages", ["account_id", "received_at"])
    op.create_index("idx_mail_messages_received", "mail_messages", ["received_at"])
    op.create_index("idx_mail_messages_focused_received", "mail_messages", ["focused", "received_at"])
    op.create_index("idx_mail_messages_unread_received", "mail_messages", ["unread", "received_at"])
