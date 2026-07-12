from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.resilience.overload import local_overload_error
from app.db.models import Account
from app.modules.proxy._service.budget import _raise_proxy_budget_exhausted
from app.modules.proxy._service.support import _HTTPBridgeSession, _WebSocketRequestState
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLease, AccountModelConcurrencyLimiter

logger = logging.getLogger("app.modules.proxy.service")


class _ConcurrencyRuntimeService(Protocol):
    _account_model_concurrency: AccountModelConcurrencyLimiter
    _http_bridge_account_model_sessions: AccountModelConcurrencyLimiter

    def _proxy_runtime_settings(self) -> Settings: ...

    @staticmethod
    def _remaining_budget_seconds_compatible(deadline: float) -> float: ...

    def _account_model_concurrency_limit(self) -> int: ...

    def _http_bridge_account_model_connect_limit(self) -> int: ...

    def _http_bridge_account_model_session_limit(self) -> int: ...

    def _try_acquire_http_bridge_connect_account_model_concurrency(
        self,
        *,
        account: Account,
        model: str | None,
        request_id: str,
    ) -> AccountModelConcurrencyLease | None: ...


class _ConcurrencyRuntimeMixin:
    def _account_model_concurrency_limit(self: _ConcurrencyRuntimeService) -> int:
        return max(0, int(getattr(self._proxy_runtime_settings(), "proxy_account_model_concurrency_limit", 32)))

    def _http_bridge_account_model_connect_limit(self: _ConcurrencyRuntimeService) -> int:
        return max(0, int(self._proxy_runtime_settings().proxy_http_bridge_account_model_connect_limit))

    def _http_bridge_account_model_session_limit(self: _ConcurrencyRuntimeService) -> int:
        return max(0, int(self._proxy_runtime_settings().proxy_http_bridge_account_model_session_limit))

    def _full_account_model_concurrency_account_ids(
        self: _ConcurrencyRuntimeService,
        model: str | None,
    ) -> set[str]:
        return self._account_model_concurrency.full_account_ids(
            model=model,
            limit=self._account_model_concurrency_limit(),
        )

    def _full_http_bridge_session_account_ids(
        self: _ConcurrencyRuntimeService,
        model: str | None,
    ) -> set[str]:
        return self._http_bridge_account_model_sessions.full_account_ids(
            model=model,
            limit=self._http_bridge_account_model_session_limit(),
        )

    def _full_http_bridge_connect_account_ids(
        self: _ConcurrencyRuntimeService,
        model: str | None,
    ) -> set[str]:
        return self._account_model_concurrency.full_account_ids(
            model=model,
            limit=self._http_bridge_account_model_connect_limit(),
        )

    def _http_bridge_session_request_budget_full(
        self: _ConcurrencyRuntimeService,
        session: _HTTPBridgeSession,
        *,
        request_model: str | None,
    ) -> bool:
        limit = self._account_model_concurrency_limit()
        if limit <= 0:
            return False
        model = request_model if request_model is not None else session.request_model
        return self._account_model_concurrency.active_count(account_id=session.account.id, model=model) >= limit

    def _try_acquire_account_model_concurrency(
        self: _ConcurrencyRuntimeService,
        *,
        account: Account,
        model: str | None,
        request_id: str,
        transport: str,
    ) -> AccountModelConcurrencyLease | None:
        limit = self._account_model_concurrency_limit()
        lease = self._account_model_concurrency.try_acquire(account_id=account.id, model=model, limit=limit)
        if lease is None:
            active = self._account_model_concurrency.active_count(account_id=account.id, model=model)
            logger.info(
                "proxy_account_model_concurrency_full request_id=%s account_id=%s model=%s "
                "transport=%s active=%s limit=%s",
                request_id,
                account.id,
                model,
                transport,
                active,
                limit,
            )
        return lease

    def _try_acquire_http_bridge_connect_account_model_concurrency(
        self: _ConcurrencyRuntimeService,
        *,
        account: Account,
        model: str | None,
        request_id: str,
    ) -> AccountModelConcurrencyLease | None:
        limit = self._http_bridge_account_model_connect_limit()
        lease = self._account_model_concurrency.try_acquire(account_id=account.id, model=model, limit=limit)
        if lease is None:
            active = self._account_model_concurrency.active_count(account_id=account.id, model=model)
            logger.info(
                "proxy_http_bridge_connect_account_model_concurrency_full "
                "request_id=%s account_id=%s model=%s active=%s limit=%s",
                request_id,
                account.id,
                model,
                active,
                limit,
            )
        return lease

    async def _acquire_http_bridge_connect_account_model_concurrency(
        self: _ConcurrencyRuntimeService,
        *,
        account: Account,
        model: str | None,
        request_id: str,
        deadline: float,
    ) -> AccountModelConcurrencyLease:
        while True:
            lease = self._try_acquire_http_bridge_connect_account_model_concurrency(
                account=account,
                model=model,
                request_id=request_id,
            )
            if lease is not None:
                return lease
            remaining = self._remaining_budget_seconds_compatible(deadline)
            if remaining <= 0:
                _raise_proxy_budget_exhausted()
            await asyncio.sleep(min(0.05, remaining))

    def _try_acquire_http_bridge_session_account_model_concurrency(
        self: _ConcurrencyRuntimeService,
        *,
        account: Account,
        model: str | None,
        request_id: str,
    ) -> AccountModelConcurrencyLease | None:
        limit = self._http_bridge_account_model_session_limit()
        lease = self._http_bridge_account_model_sessions.try_acquire(
            account_id=account.id,
            model=model,
            limit=limit,
        )
        if lease is None:
            active = self._http_bridge_account_model_sessions.active_count(account_id=account.id, model=model)
            logger.info(
                "proxy_http_bridge_session_account_model_concurrency_full "
                "request_id=%s account_id=%s model=%s active=%s limit=%s",
                request_id,
                account.id,
                model,
                active,
                limit,
            )
        return lease

    @staticmethod
    def _release_request_account_model_concurrency(request_state: _WebSocketRequestState) -> None:
        lease = request_state.account_model_concurrency
        if lease is None:
            return
        request_state.account_model_concurrency = None
        lease.release()

    @staticmethod
    def _account_model_concurrency_overload(model: str | None) -> ProxyResponseError:
        model_label = model or "<unknown>"
        return ProxyResponseError(
            429,
            local_overload_error(
                f"All eligible accounts are at the local account/model concurrency budget for {model_label}"
            ),
        )
