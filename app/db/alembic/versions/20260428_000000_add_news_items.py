"""add news items history table

Revision ID: 20260428_000000_add_news_items
Revises: 20260423_000000_merge_upstream_provider_accounts
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260428_000000_add_news_items"
down_revision = "20260423_000000_merge_upstream_provider_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "news_items" not in inspector.get_table_names():
        op.create_table(
            "news_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("section", sa.String(), nullable=False),
            sa.Column("item_identity", sa.String(), nullable=False),
            sa.Column("semantic_signature", sa.Text(), nullable=True),
            sa.Column("full_json", sa.Text(), nullable=False),
            sa.Column("compact_json", sa.Text(), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("source_published_at", sa.String(), nullable=True),
            sa.Column("generated_at", sa.DateTime(), nullable=True),
            sa.Column("recorded_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("section", "item_identity", name="uq_news_items_section_identity"),
        )

    existing_indexes = {index["name"] for index in inspector.get_indexes("news_items")}
    if "idx_news_items_section_recorded_at" not in existing_indexes:
        op.create_index(
            "idx_news_items_section_recorded_at",
            "news_items",
            ["section", sa.text("recorded_at DESC")],
            unique=False,
        )
    if "idx_news_items_section_generated_at" not in existing_indexes:
        op.create_index(
            "idx_news_items_section_generated_at",
            "news_items",
            ["section", sa.text("generated_at DESC")],
            unique=False,
        )
    if "idx_news_items_recorded_at" not in existing_indexes:
        op.create_index(
            "idx_news_items_recorded_at",
            "news_items",
            [sa.text("recorded_at DESC")],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("idx_news_items_recorded_at", table_name="news_items", if_exists=True)
    op.drop_index("idx_news_items_section_generated_at", table_name="news_items", if_exists=True)
    op.drop_index("idx_news_items_section_recorded_at", table_name="news_items", if_exists=True)
    op.drop_table("news_items")
