"""convert kyc-only api keys to kyc group assignments

Revision ID: 20260502_000000_convert_kyc_only_keys_to_group
Revises: 20260501_000000_add_account_groups_and_stored_api_keys
Create Date: 2026-05-02 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260502_000000_convert_kyc_only_keys_to_group"
down_revision = "20260501_000000_add_account_groups_and_stored_api_keys"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    tables = _table_names()
    if "api_keys" not in tables or "api_key_allowed_groups" not in tables:
        return
    if "kyc_only" not in _column_names("api_keys"):
        return

    api_keys = sa.table(
        "api_keys",
        sa.column("id", sa.String()),
        sa.column("kyc_only", sa.Boolean()),
    )
    allowed_groups = sa.table(
        "api_key_allowed_groups",
        sa.column("api_key_id", sa.String()),
        sa.column("group_name", sa.String()),
    )

    bind = op.get_bind()
    kyc_key_ids = [
        row[0]
        for row in bind.execute(sa.select(api_keys.c.id).where(api_keys.c.kyc_only.is_(True))).fetchall()
        if row[0] is not None
    ]
    if not kyc_key_ids:
        return

    bind.execute(allowed_groups.delete().where(allowed_groups.c.api_key_id.in_(kyc_key_ids)))
    bind.execute(
        allowed_groups.insert(),
        [{"api_key_id": key_id, "group_name": "kyc"} for key_id in kyc_key_ids],
    )
    bind.execute(api_keys.update().where(api_keys.c.id.in_(kyc_key_ids)).values(kyc_only=False))


def downgrade() -> None:
    tables = _table_names()
    if "api_keys" not in tables or "api_key_allowed_groups" not in tables:
        return
    if "kyc_only" not in _column_names("api_keys"):
        return

    api_keys = sa.table(
        "api_keys",
        sa.column("id", sa.String()),
        sa.column("kyc_only", sa.Boolean()),
    )
    allowed_groups = sa.table(
        "api_key_allowed_groups",
        sa.column("api_key_id", sa.String()),
        sa.column("group_name", sa.String()),
    )

    bind = op.get_bind()
    group_counts = (
        sa.select(allowed_groups.c.api_key_id)
        .group_by(allowed_groups.c.api_key_id)
        .having(sa.func.count() == 1)
        .having(sa.func.max(allowed_groups.c.group_name) == "kyc")
    )
    key_ids = [row[0] for row in bind.execute(group_counts).fetchall() if row[0] is not None]
    if not key_ids:
        return

    bind.execute(api_keys.update().where(api_keys.c.id.in_(key_ids)).values(kyc_only=True))
    bind.execute(
        allowed_groups.delete()
        .where(allowed_groups.c.api_key_id.in_(key_ids))
        .where(allowed_groups.c.group_name == "kyc")
    )
