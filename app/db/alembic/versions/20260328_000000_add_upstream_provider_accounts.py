"""add upstream provider account fields

Revision ID: 20260328_000000_add_upstream_provider_accounts
Revises: 20260321_210000_merge_request_log_tiers_and_dashboard_index_heads
Create Date: 2026-03-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260328_000000_add_upstream_provider_accounts"
down_revision = "20260321_210000_merge_request_log_tiers_and_dashboard_index_heads"
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

    existing = _columns(bind, "accounts")
    with op.batch_alter_table("accounts") as batch_op:
        if "provider_kind" not in existing:
            batch_op.add_column(
                sa.Column(
                    "provider_kind",
                    sa.String(),
                    nullable=False,
                    server_default=sa.text("'openai_oauth'"),
                )
            )
        if "upstream_base_url" not in existing:
            batch_op.add_column(sa.Column("upstream_base_url", sa.Text(), nullable=True))
        if "upstream_wire_api" not in existing:
            batch_op.add_column(sa.Column("upstream_wire_api", sa.String(), nullable=True))
        if "upstream_priority" not in existing:
            batch_op.add_column(
                sa.Column(
                    "upstream_priority",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("100"),
                )
            )
        if "supported_models_json" not in existing:
            batch_op.add_column(sa.Column("supported_models_json", sa.Text(), nullable=True))

    bind.execute(
        sa.text(
            """
            UPDATE accounts
            SET provider_kind = 'openai_oauth'
            WHERE provider_kind IS NULL OR provider_kind = ''
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE accounts
            SET upstream_priority = 100
            WHERE upstream_priority IS NULL
            """
        )
    )


def downgrade() -> None:
    return
