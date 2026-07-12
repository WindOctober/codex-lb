"""add mail inbox tables

Revision ID: 20260619_000000_add_mail_inbox_tables
Revises: 20260618_000000_add_account_subscription_renews_at
Create Date: 2026-06-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260619_000000_add_mail_inbox_tables"
down_revision = "20260618_000000_add_account_subscription_renews_at"
branch_labels = None
depends_on = None


def _table_exists(connection: Connection, table_name: str) -> bool:
    return sa.inspect(connection).has_table(table_name)


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "mail_accounts"):
        op.create_table(
            "mail_accounts",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("address", sa.String(), nullable=False),
            sa.Column("display_name", sa.String(), nullable=True),
            sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("sync_status", sa.String(), server_default="never_synced", nullable=False),
            sa.Column("last_sync_at", sa.DateTime(), nullable=True),
            sa.Column("last_sync_error", sa.Text(), nullable=True),
            sa.Column("imap_host", sa.String(), nullable=True),
            sa.Column("imap_port", sa.Integer(), nullable=True),
            sa.Column("imap_username", sa.String(), nullable=True),
            sa.Column("credential_encrypted", sa.LargeBinary(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "address", name="uq_mail_accounts_provider_address"),
        )
        op.create_index("idx_mail_accounts_provider_address", "mail_accounts", ["provider", "address"])

    if not _table_exists(bind, "mail_messages"):
        op.create_table(
            "mail_messages",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("account_id", sa.String(), nullable=False),
            sa.Column("provider_message_id", sa.String(), nullable=False),
            sa.Column("thread_id", sa.String(), nullable=True),
            sa.Column("sender_email", sa.String(), nullable=False),
            sa.Column("sender_name", sa.String(), nullable=True),
            sa.Column("recipients_json", sa.Text(), server_default="[]", nullable=False),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("snippet", sa.Text(), server_default="", nullable=False),
            sa.Column("received_at", sa.DateTime(), nullable=False),
            sa.Column("unread", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("starred", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("has_attachments", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("focused", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("focus_label", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "provider_message_id", name="uq_mail_messages_account_provider_id"),
        )
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

    if not _table_exists(bind, "mail_focus_rules"):
        op.create_table(
            "mail_focus_rules",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("value", sa.String(), nullable=False),
            sa.Column("label", sa.String(), nullable=True),
            sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_mail_focus_rules_kind_value", "mail_focus_rules", ["kind", "value"])


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "mail_focus_rules"):
        op.drop_index("idx_mail_focus_rules_kind_value", table_name="mail_focus_rules")
        op.drop_table("mail_focus_rules")
    if _table_exists(bind, "mail_messages"):
        op.drop_index("idx_mail_messages_unread_received", table_name="mail_messages")
        op.drop_index("idx_mail_messages_focused_received", table_name="mail_messages")
        op.drop_index("idx_mail_messages_received", table_name="mail_messages")
        op.drop_index("idx_mail_messages_account_received", table_name="mail_messages")
        op.drop_table("mail_messages")
    if _table_exists(bind, "mail_accounts"):
        op.drop_index("idx_mail_accounts_provider_address", table_name="mail_accounts")
        op.drop_table("mail_accounts")
