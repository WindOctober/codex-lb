from __future__ import annotations

from datetime import datetime

import pytest

from app.core.crypto import TokenEncryptor
from app.core.usage.models import UsagePayload
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus
from app.modules.accounts.service import AccountsService

pytestmark = pytest.mark.unit


class _Repo:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.tokens_payload: dict[str, object] | None = None
        self.status_payload: dict[str, object] | None = None

    async def get_by_id(self, account_id: str) -> Account | None:
        return self.account if account_id == self.account.id else None

    async def update_status(
        self,
        account_id: str,
        status: AccountStatus,
        deactivation_reason: str | None = None,
        reset_at: int | None = None,
        blocked_at: int | None = None,
    ) -> bool:
        self.status_payload = {
            "account_id": account_id,
            "status": status,
            "deactivation_reason": deactivation_reason,
            "reset_at": reset_at,
            "blocked_at": blocked_at,
        }
        return True

    async def update_tokens(
        self,
        account_id: str,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes,
        id_token_encrypted: bytes,
        last_refresh: datetime,
        plan_type: str | None = None,
        email: str | None = None,
        chatgpt_account_id: str | None = None,
    ) -> bool:
        self.tokens_payload = {
            "account_id": account_id,
            "access_token_encrypted": access_token_encrypted,
            "refresh_token_encrypted": refresh_token_encrypted,
            "id_token_encrypted": id_token_encrypted,
            "last_refresh": last_refresh,
            "plan_type": plan_type,
            "email": email,
            "chatgpt_account_id": chatgpt_account_id,
        }
        self.account.plan_type = plan_type or self.account.plan_type
        return True


@pytest.mark.asyncio
async def test_availability_probe_syncs_plan_type_from_usage_payload(monkeypatch) -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="acc_usage_plan",
        chatgpt_account_id="workspace_usage_plan",
        email="user@example.com",
        plan_type="free",
        access_token_encrypted=encryptor.encrypt("old-access"),
        refresh_token_encrypted=encryptor.encrypt("old-refresh"),
        id_token_encrypted=encryptor.encrypt("old-id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    repo = _Repo(account)
    service = AccountsService(repo)  # type: ignore[arg-type]

    async def _fake_ensure_fresh(account_arg: Account, *, force: bool = False) -> Account:
        assert force is True
        account_arg.access_token_encrypted = encryptor.encrypt("new-access")
        account_arg.refresh_token_encrypted = encryptor.encrypt("new-refresh")
        account_arg.id_token_encrypted = encryptor.encrypt("new-id-with-free-claim")
        return account_arg

    async def _fake_fetch_usage(**kwargs: object) -> UsagePayload:
        assert kwargs["access_token"] == "new-access"
        assert kwargs["account_id"] == "workspace_usage_plan"
        return UsagePayload.model_validate({"plan_type": "plus"})

    service._auth_manager.ensure_fresh = _fake_ensure_fresh  # type: ignore[method-assign]
    monkeypatch.setattr("app.modules.accounts.service.fetch_usage", _fake_fetch_usage)

    result = await service.test_availability(account.id)

    assert result is not None
    assert result.passed_count == 1
    assert account.plan_type == "plus"
    assert repo.tokens_payload is not None
    assert repo.tokens_payload["plan_type"] == "plus"
    assert repo.status_payload is None
