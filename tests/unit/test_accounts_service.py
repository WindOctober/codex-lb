from __future__ import annotations

from datetime import datetime

import pytest

from app.core.auth.refresh import RefreshError
from app.core.crypto import TokenEncryptor
from app.core.usage.models import RateLimitResetConsumePayload, RateLimitResetCreditBankPayload, UsagePayload
from app.core.utils.time import utcnow
from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account, AccountStatus
from app.modules.accounts.service import AccountResetCreditError, AccountsService, _quota_timeline_bucket_count

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


class _UsageUpdater:
    def __init__(self) -> None:
        self.refreshed_account_id: str | None = None

    async def refresh_account_now(self, account: Account) -> None:
        self.refreshed_account_id = account.id


def test_quota_timeline_bucket_count_covers_current_partial_bucket() -> None:
    bucket_seconds = 5 * 3600
    aligned_start = 100 * bucket_seconds
    since_epoch = aligned_start + 4 * 3600
    until_epoch = since_epoch + 7 * 24 * 3600

    bucket_count = _quota_timeline_bucket_count(
        since_epoch=since_epoch,
        until_epoch=until_epoch,
        bucket_seconds=bucket_seconds,
    )

    assert aligned_start + bucket_count * bucket_seconds >= until_epoch
    assert aligned_start + (bucket_count - 1) * bucket_seconds < until_epoch


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

    async def _fake_ensure_fresh(
        account_arg: Account,
        *,
        force: bool = False,
        deactivate_on_permanent_error: bool = True,
    ) -> Account:
        assert force is True
        assert deactivate_on_permanent_error is False
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


@pytest.mark.asyncio
async def test_availability_probe_does_not_deactivate_transient_refresh_failure() -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="acc_transient_refresh",
        chatgpt_account_id="workspace_transient_refresh",
        email="user@example.com",
        plan_type="pro",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    repo = _Repo(account)
    service = AccountsService(repo)  # type: ignore[arg-type]

    async def _fake_ensure_fresh(
        account_arg: Account,
        *,
        force: bool = False,
        deactivate_on_permanent_error: bool = True,
    ) -> Account:
        assert force is True
        assert deactivate_on_permanent_error is False
        raise RefreshError("invalid_response", "temporary refresh failure", False)

    service._auth_manager.ensure_fresh = _fake_ensure_fresh  # type: ignore[method-assign]

    result = await service.test_availability(account.id)

    assert result is not None
    assert result.passed_count == 0
    assert result.failed_count == 1
    assert result.active_count == 1
    assert repo.status_payload is None
    assert account.status == AccountStatus.ACTIVE
    assert account.deactivation_reason is None


@pytest.mark.asyncio
async def test_get_rate_limit_reset_credits_returns_available_count(monkeypatch) -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="acc_reset_credits",
        chatgpt_account_id="workspace_reset_credits",
        email="user@example.com",
        plan_type="pro",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    service = AccountsService(_Repo(account))  # type: ignore[arg-type]

    async def _fake_ensure_fresh(account_arg: Account, **_: object) -> Account:
        return account_arg

    async def _fake_fetch_credits(**kwargs: object) -> RateLimitResetCreditBankPayload:
        assert kwargs["access_token"] == "access"
        assert kwargs["account_id"] == "workspace_reset_credits"
        return RateLimitResetCreditBankPayload(available_count=3)

    service._auth_manager.ensure_fresh = _fake_ensure_fresh  # type: ignore[method-assign]
    monkeypatch.setattr("app.modules.accounts.service.fetch_rate_limit_reset_credits", _fake_fetch_credits)

    result = await service.get_rate_limit_reset_credits(account.id)

    assert result is not None
    assert result.account_id == account.id
    assert result.available_count == 3


@pytest.mark.asyncio
async def test_consume_rate_limit_reset_credit_refreshes_usage(monkeypatch) -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="acc_consume_reset",
        chatgpt_account_id="workspace_consume_reset",
        email="user@example.com",
        plan_type="pro",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    service = AccountsService(_Repo(account))  # type: ignore[arg-type]
    usage_updater = _UsageUpdater()
    service._usage_updater = usage_updater  # type: ignore[assignment]

    async def _fake_ensure_fresh(account_arg: Account, **_: object) -> Account:
        return account_arg

    async def _fake_consume(**kwargs: object) -> RateLimitResetConsumePayload:
        assert kwargs["credit_id"] == "RateLimitResetCredit_test"
        assert kwargs["idempotency_key"] == "redeem-1"
        return RateLimitResetConsumePayload(code="reset", windows_reset=1)

    async def _fake_fetch_credits(**_: object) -> RateLimitResetCreditBankPayload:
        return RateLimitResetCreditBankPayload.model_validate(
            {
                "available_count": 2,
                "credits": [
                    {
                        "id": "RateLimitResetCredit_test",
                        "status": "available",
                        "reset_type": "codex_rate_limits",
                    }
                ],
            }
        )

    service._auth_manager.ensure_fresh = _fake_ensure_fresh  # type: ignore[method-assign]
    monkeypatch.setattr("app.modules.accounts.service.consume_upstream_rate_limit_reset_credit", _fake_consume)
    monkeypatch.setattr("app.modules.accounts.service.fetch_rate_limit_reset_credits", _fake_fetch_credits)

    result = await service.consume_rate_limit_reset_credit(account.id, idempotency_key="redeem-1")

    assert result is not None
    assert result.account_id == account.id
    assert result.outcome == "reset"
    assert result.windows_reset == 1
    assert result.available_count == 2
    assert usage_updater.refreshed_account_id == account.id


@pytest.mark.asyncio
async def test_rate_limit_reset_credits_rejects_api_key_provider() -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="provider_reset_credits",
        chatgpt_account_id=None,
        email="Provider",
        plan_type="api_key_provider",
        provider_kind=ACCOUNT_PROVIDER_API_KEY,
        access_token_encrypted=encryptor.encrypt("api-key"),
        refresh_token_encrypted=b"",
        id_token_encrypted=b"",
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    service = AccountsService(_Repo(account))  # type: ignore[arg-type]

    with pytest.raises(AccountResetCreditError):
        await service.get_rate_limit_reset_credits(account.id)


@pytest.mark.asyncio
async def test_availability_probe_does_not_deactivate_permanent_refresh_failure() -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="acc_permanent_refresh",
        chatgpt_account_id="workspace_permanent_refresh",
        email="user@example.com",
        plan_type="pro",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    repo = _Repo(account)
    service = AccountsService(repo)  # type: ignore[arg-type]

    async def _fake_ensure_fresh(
        account_arg: Account,
        *,
        force: bool = False,
        deactivate_on_permanent_error: bool = True,
    ) -> Account:
        assert force is True
        assert deactivate_on_permanent_error is False
        raise RefreshError("refresh_token_expired", "token expired", True)

    service._auth_manager.ensure_fresh = _fake_ensure_fresh  # type: ignore[method-assign]

    result = await service.test_availability(account.id)

    assert result is not None
    assert result.passed_count == 0
    assert result.failed_count == 1
    assert result.active_count == 1
    assert repo.status_payload is None
    assert account.status == AccountStatus.ACTIVE
    assert account.deactivation_reason is None
