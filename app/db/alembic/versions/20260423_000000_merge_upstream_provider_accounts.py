"""merge upstream provider account branch

Revision ID: 20260423_000000_merge_upstream_provider_accounts
Revises: 20260421_120000_merge_request_log_lookup_and_plan_type_heads, 20260328_000000_add_upstream_provider_accounts
Create Date: 2026-04-23
"""

from __future__ import annotations

revision = "20260423_000000_merge_upstream_provider_accounts"
down_revision = (
    "20260421_120000_merge_request_log_lookup_and_plan_type_heads",
    "20260328_000000_add_upstream_provider_accounts",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    return


def downgrade() -> None:
    return
