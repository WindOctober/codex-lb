from __future__ import annotations

import asyncio
from typing import Protocol, cast

from app.core.auth.refresh import (
    pop_token_refresh_timeout_override,
    push_token_refresh_timeout_override,
)
from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account
from app.modules.accounts.auth_manager import AuthManager
from app.modules.proxy._service.support import (
    _await_operation_before_hard_timeout,
    _call_with_supported_optional_kwargs,
)
from app.modules.proxy.repo_bundle import ProxyRepoFactory
from app.modules.proxy.work_admission import WorkAdmissionController


class _AccountFreshnessService(Protocol):
    _repo_factory: ProxyRepoFactory
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

    def _get_work_admission(self) -> WorkAdmissionController: ...

    async def _ensure_fresh(
        self,
        account: Account,
        *,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> Account: ...


class _AccountFreshnessMixin:
    async def _ensure_fresh(
        self: _AccountFreshnessService,
        account: Account,
        *,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> Account:
        if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
            return account
        token = push_token_refresh_timeout_override(timeout_seconds)
        try:
            async with self._repo_factory() as repos:
                auth_manager = AuthManager(
                    repos.accounts,
                    acquire_refresh_admission=self._get_work_admission().acquire_token_refresh,
                )
                # Proxy request paths classify and persist refresh failures in
                # tracked cleanup so account failover never waits on a status
                # write inside AuthManager.
                return cast(
                    Account,
                    await _call_with_supported_optional_kwargs(
                        auth_manager.ensure_fresh,
                        account,
                        optional_kwargs={"deactivate_on_permanent_error": False},
                        force=force,
                    ),
                )
        finally:
            pop_token_refresh_timeout_override(token)

    async def _ensure_fresh_with_budget(
        self: _AccountFreshnessService,
        account: Account,
        *,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> Account:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise asyncio.TimeoutError
        operation = _call_with_supported_optional_kwargs(
            self._ensure_fresh,
            account,
            optional_kwargs={"timeout_seconds": timeout_seconds},
            force=force,
        )
        if timeout_seconds is None:
            return cast(Account, await operation)
        return cast(
            Account,
            await _await_operation_before_hard_timeout(
                operation,
                timeout_seconds=timeout_seconds,
                tasks=self._proxy_cleanup_tasks,
                label=f"account freshness account_id={account.id}",
            ),
        )
