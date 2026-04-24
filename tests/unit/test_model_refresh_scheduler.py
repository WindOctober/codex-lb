from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.openai.model_refresh_scheduler import _group_by_plan
from app.db.models import ACCOUNT_PROVIDER_API_KEY, ACCOUNT_PROVIDER_OPENAI_OAUTH, Account, AccountStatus

pytestmark = pytest.mark.unit


def _account(
    account_id: str,
    *,
    provider_kind: str = ACCOUNT_PROVIDER_OPENAI_OAUTH,
    plan_type: str = "plus",
    status: AccountStatus = AccountStatus.ACTIVE,
) -> Account:
    return Account(
        id=account_id,
        chatgpt_account_id=None,
        email=f"{account_id}@example.test",
        plan_type=plan_type,
        provider_kind=provider_kind,
        upstream_base_url=None,
        upstream_wire_api=None,
        upstream_priority=100,
        supported_models_json=None,
        access_token_encrypted=b"access",
        refresh_token_encrypted=b"refresh",
        id_token_encrypted=b"id",
        last_refresh=datetime.now(timezone.utc),
        status=status,
        deactivation_reason=None,
    )


def test_group_by_plan_skips_api_key_providers() -> None:
    grouped = _group_by_plan(
        [
            _account("oauth_plus"),
            _account("provider", provider_kind=ACCOUNT_PROVIDER_API_KEY, plan_type="api_key_provider"),
            _account("paused", status=AccountStatus.PAUSED),
        ]
    )

    assert grouped == {"plus": [grouped["plus"][0]]}
    assert grouped["plus"][0].id == "oauth_plus"
