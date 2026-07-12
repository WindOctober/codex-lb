from __future__ import annotations

import asyncio
from types import TracebackType
from typing import AsyncContextManager, Literal, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import refresh as refresh_module
from app.core.auth.refresh import RefreshError
from app.db.models import ACCOUNT_PROVIDER_API_KEY, ACCOUNT_PROVIDER_OPENAI_OAUTH, Account, AccountStatus
from app.modules.accounts.auth_manager import AuthManager
from app.modules.accounts.repository import AccountsRepository
from app.modules.api_keys.repository import ApiKeysRepository
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.account_freshness import (
    _AccountFreshnessMixin,
    _AccountFreshnessService,
)
from app.modules.proxy.repo_bundle import ProxyRepoFactory, ProxyRepositories
from app.modules.proxy.sticky_repository import StickySessionsRepository
from app.modules.proxy.work_admission import AdmissionLease, WorkAdmissionController
from app.modules.request_logs.repository import RequestLogsRepository
from app.modules.usage.repository import AdditionalUsageRepository, UsageRepository

pytestmark = pytest.mark.unit


class _RepositoryContext:
    def __init__(self, repositories: ProxyRepositories) -> None:
        self.repositories = repositories
        self.enter_calls = 0
        self.exit_calls = 0

    async def __aenter__(self) -> ProxyRepositories:
        self.enter_calls += 1
        return self.repositories

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.exit_calls += 1


class _RecordingRepoFactory:
    def __init__(self, context: _RepositoryContext) -> None:
        self.context = context
        self.calls = 0

    def __call__(self) -> AsyncContextManager[ProxyRepositories]:
        self.calls += 1
        return self.context


class _RecordingWorkAdmission(WorkAdmissionController):
    def __init__(self) -> None:
        self.acquire_token_refresh_calls = 0

    async def acquire_token_refresh(self) -> AdmissionLease:
        self.acquire_token_refresh_calls += 1
        return AdmissionLease(None)


class _FakeService(_AccountFreshnessMixin):
    def __init__(
        self,
        repo_factory: ProxyRepoFactory,
        admission: WorkAdmissionController,
    ) -> None:
        self._repo_factory = repo_factory
        self._admission = admission
        self.admission_lookup_calls = 0

    def _get_work_admission(self) -> WorkAdmissionController:
        self.admission_lookup_calls += 1
        return self._admission


def _repositories() -> ProxyRepositories:
    session = cast(AsyncSession, object())
    return ProxyRepositories(
        accounts=AccountsRepository(session),
        usage=UsageRepository(session),
        request_logs=RequestLogsRepository(session),
        sticky_sessions=StickySessionsRepository(session),
        api_keys=ApiKeysRepository(session),
        additional_usage=AdditionalUsageRepository(session),
    )


def _account(provider_kind: str) -> Account:
    return Account(
        id=f"account-{provider_kind}",
        chatgpt_account_id=None if provider_kind == ACCOUNT_PROVIDER_API_KEY else "workspace-account",
        email=f"{provider_kind}@example.com",
        plan_type="api_key_provider" if provider_kind == ACCOUNT_PROVIDER_API_KEY else "plus",
        provider_kind=provider_kind,
        access_token_encrypted=b"encrypted-access-token",
        refresh_token_encrypted=b"encrypted-refresh-token",
        id_token_encrypted=b"encrypted-id-token",
        last_refresh=refresh_module.utcnow(),
        status=AccountStatus.ACTIVE,
    )


def _service() -> tuple[
    _FakeService,
    _RecordingRepoFactory,
    _RepositoryContext,
    _RecordingWorkAdmission,
]:
    context = _RepositoryContext(_repositories())
    repo_factory = _RecordingRepoFactory(context)
    repo_factory_contract: ProxyRepoFactory = repo_factory
    admission = _RecordingWorkAdmission()
    service = _FakeService(repo_factory_contract, admission)
    service_contract: _AccountFreshnessService = service
    assert service_contract is service
    return service, repo_factory, context, admission


@pytest.mark.asyncio
async def test_api_key_provider_bypasses_repository_and_refresh_admission() -> None:
    service, repo_factory, context, admission = _service()
    account = _account(ACCOUNT_PROVIDER_API_KEY)

    result = await service._ensure_fresh(
        account,
        force=True,
        timeout_seconds=0.25,
    )

    assert result is account
    assert repo_factory.calls == 0
    assert context.enter_calls == 0
    assert context.exit_calls == 0
    assert service.admission_lookup_calls == 0
    assert admission.acquire_token_refresh_calls == 0


@pytest.mark.asyncio
async def test_oauth_freshness_uses_canonical_auth_manager_and_refresh_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo_factory, context, admission = _service()
    account = _account(ACCOUNT_PROVIDER_OPENAI_OAUTH)
    managers: list[AuthManager] = []
    force_values: list[bool] = []

    async def fake_ensure_fresh(
        manager: AuthManager,
        target: Account,
        *,
        force: bool = False,
        deactivate_on_permanent_error: bool = True,
    ) -> Account:
        assert deactivate_on_permanent_error is True
        managers.append(manager)
        force_values.append(force)
        return target

    monkeypatch.setattr(AuthManager, "ensure_fresh", fake_ensure_fresh)

    result = await service._ensure_fresh(
        account,
        force=True,
        timeout_seconds=2.0,
    )

    assert result is account
    assert repo_factory.calls == 1
    assert context.enter_calls == 1
    assert context.exit_calls == 1
    assert service.admission_lookup_calls == 1
    assert len(managers) == 1
    manager = managers[0]
    assert type(manager) is AuthManager
    assert manager._repo is context.repositories.accounts
    acquire_refresh_admission = manager._acquire_refresh_admission
    assert acquire_refresh_admission is not None
    lease = await acquire_refresh_admission()
    lease.release()
    assert admission.acquire_token_refresh_calls == 1
    assert force_values == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "runtime_error", "cancelled"])
async def test_outer_refresh_timeout_override_is_restored_after_every_exit(
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["success", "runtime_error", "cancelled"],
) -> None:
    service, _, _, _ = _service()
    account = _account(ACCOUNT_PROVIDER_OPENAI_OAUTH)
    observed_inner_timeouts: list[float] = []

    async def fake_ensure_fresh(
        manager: AuthManager,
        target: Account,
        *,
        force: bool = False,
        deactivate_on_permanent_error: bool = True,
    ) -> Account:
        del manager, force, deactivate_on_permanent_error
        observed_inner_timeouts.append(refresh_module._effective_token_refresh_timeout(30.0))
        if outcome == "runtime_error":
            raise RuntimeError("refresh failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return target

    monkeypatch.setattr(AuthManager, "ensure_fresh", fake_ensure_fresh)
    outer_token = refresh_module.push_token_refresh_timeout_override(7.0)
    try:
        if outcome == "success":
            assert await service._ensure_fresh(account, timeout_seconds=1.5) is account
        elif outcome == "runtime_error":
            with pytest.raises(RuntimeError, match="refresh failed"):
                await service._ensure_fresh(account, timeout_seconds=1.5)
        else:
            with pytest.raises(asyncio.CancelledError):
                await service._ensure_fresh(account, timeout_seconds=1.5)

        assert refresh_module._effective_token_refresh_timeout(30.0) == 7.0
    finally:
        refresh_module.pop_token_refresh_timeout_override(outer_token)

    assert observed_inner_timeouts == [1.5]


@pytest.mark.asyncio
async def test_budget_adapter_filters_timeout_for_legacy_signature_but_preserves_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = _service()
    account = _account(ACCOUNT_PROVIDER_OPENAI_OAUTH)
    force_values: list[bool] = []

    async def legacy_ensure_fresh(target: Account, *, force: bool = False) -> Account:
        force_values.append(force)
        return target

    monkeypatch.setattr(service, "_ensure_fresh", legacy_ensure_fresh)

    result = await service._ensure_fresh_with_budget(
        account,
        force=True,
        timeout_seconds=3.0,
    )

    assert result is account
    assert force_values == [True]


def test_proxy_service_preserves_account_freshness_facade_aliases() -> None:
    assert proxy_service.AuthManager is AuthManager
    assert proxy_service.RefreshError is RefreshError
    assert proxy_service.ACCOUNT_PROVIDER_API_KEY == ACCOUNT_PROVIDER_API_KEY
