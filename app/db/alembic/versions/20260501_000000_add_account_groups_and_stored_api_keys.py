"""add account groups and stored api keys

Revision ID: 20260501_000000_add_account_groups_and_stored_api_keys
Revises: 20260430_020000_add_account_kyc_flag
Create Date: 2026-05-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260501_000000_add_account_groups_and_stored_api_keys"
down_revision = "20260430_020000_add_account_kyc_flag"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    tables = _table_names()
    if "account_groups" not in tables:
        op.create_table(
            "account_groups",
            sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("group_name", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("account_id", "group_name"),
        )
    if "api_key_allowed_groups" not in tables:
        op.create_table(
            "api_key_allowed_groups",
            sa.Column("api_key_id", sa.String(), sa.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False),
            sa.Column("group_name", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("api_key_id", "group_name"),
        )
    if "api_key_preferred_groups" not in tables:
        op.create_table(
            "api_key_preferred_groups",
            sa.Column("api_key_id", sa.String(), sa.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False),
            sa.Column("group_name", sa.String(), nullable=False),
            sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("api_key_id", "group_name"),
        )

    if "key_encrypted" not in _column_names("api_keys"):
        op.add_column("api_keys", sa.Column("key_encrypted", sa.LargeBinary(), nullable=True))

    for table_name, index_name in (
        ("account_groups", "idx_account_groups_group_name"),
        ("api_key_allowed_groups", "idx_api_key_allowed_groups_group_name"),
        ("api_key_preferred_groups", "idx_api_key_preferred_groups_group_name"),
    ):
        if index_name not in _index_names(table_name):
            op.create_index(index_name, table_name, ["group_name"], unique=False)


def downgrade() -> None:
    if "key_encrypted" in _column_names("api_keys"):
        op.drop_column("api_keys", "key_encrypted")

    for table_name, index_name in (
        ("api_key_preferred_groups", "idx_api_key_preferred_groups_group_name"),
        ("api_key_allowed_groups", "idx_api_key_allowed_groups_group_name"),
        ("account_groups", "idx_account_groups_group_name"),
    ):
        if table_name in _table_names() and index_name in _index_names(table_name):
            op.drop_index(index_name, table_name=table_name)

    tables = _table_names()
    for table_name in ("api_key_preferred_groups", "api_key_allowed_groups", "account_groups"):
        if table_name in tables:
            op.drop_table(table_name)
