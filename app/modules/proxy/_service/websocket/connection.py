from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import aiohttp
import anyio
from fastapi import WebSocket

from app.core.auth.refresh import RefreshError
from app.core.balancer import RoutingStrategy, failover_decision
from app.core.balancer.types import ClassifiedFailure
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
from app.core.config.settings import Settings
from app.core.errors import OpenAIErrorEnvelope, openai_error
from app.db.models import Account, StickySessionKind
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.budget import (
    _is_proxy_budget_exhausted_error,
    _websocket_connect_deadline,
)
from app.modules.proxy._service.support import (
    _WEBSOCKET_MAX_ACCOUNT_ATTEMPTS,
    _WebSocketRequestState,
)
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error
from app.modules.proxy.load_balancer import AccountSelection, LoadBalancer

logger = logging.getLogger("app.modules.proxy.service")


class _WebSocketConnectionService(Protocol):
    _load_balancer: LoadBalancer

    @staticmethod
    def _proxy_runtime_settings() -> Settings: ...

    @staticmethod
    def _remaining_budget_seconds_compatible(deadline: float) -> float: ...

    @staticmethod
    def _record_continuity_fail_closed_compatible(
        *,
        surface: str,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None = None,
        upstream_error_code: str | None = None,
    ) -> None: ...

    async def _select_account_with_budget_compatible(
        self,
        deadline: float,
        **kwargs: object,
    ) -> AccountSelection: ...

    async def _ensure_fresh_with_budget(
        self,
        account: Account,
        *,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> Account: ...

    async def _open_upstream_websocket_with_budget(
        self,
        account: Account,
        headers: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> UpstreamResponsesWebSocket: ...

    async def _handle_websocket_connect_error(
        self,
        account: Account,
        exc: ProxyResponseError,
    ) -> ClassifiedFailure: ...

    async def _emit_websocket_connect_failure(
        self,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        status_code: int,
        payload: OpenAIErrorEnvelope,
        error_code: str,
        error_message: str,
    ) -> None: ...

    async def _emit_websocket_proxy_request_timeout(
        self,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
    ) -> None: ...

    async def _select_websocket_connect_account(
        self,
        deadline: float,
        *,
        sticky_key: str | None,
        sticky_kind: StickySessionKind | None,
        prefer_earlier_reset: bool,
        routing_strategy: RoutingStrategy,
        model: str | None,
        request_state: _WebSocketRequestState,
        api_key: ApiKeyData | None,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
        reallocate_sticky: bool,
        sticky_max_age_seconds: int | None,
        exclude_account_ids: set[str],
        preferred_account_id: str | None,
        require_preferred_account: bool,
    ) -> Account | None: ...

    async def _try_open_websocket_connect_attempt(
        self,
        account: Account,
        headers: dict[str, str],
        *,
        deadline: float,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
    ) -> tuple[Account, UpstreamResponsesWebSocket] | None: ...

    async def _retry_websocket_connect_after_401(
        self,
        account: Account,
        headers: dict[str, str],
        *,
        deadline: float,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
    ) -> tuple[Account, UpstreamResponsesWebSocket] | None: ...

    async def _decide_websocket_failover_action(
        self,
        *,
        account: Account,
        exc: ProxyResponseError,
        request_state: _WebSocketRequestState,
        attempt: int,
        max_attempts: int,
        deterministic_failover_enabled: bool,
    ) -> str: ...

    async def _emit_websocket_connect_timeout(
        self,
        *,
        websocket: WebSocket,
        client_send_lock: anyio.Lock,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
    ) -> None: ...


class _WebSocketConnectionMixin:
    async def _connect_proxy_websocket(
        self: _WebSocketConnectionService,
        headers: dict[str, str],
        *,
        sticky_key: str | None,
        sticky_kind: StickySessionKind | None,
        prefer_earlier_reset: bool,
        routing_strategy: RoutingStrategy,
        model: str | None,
        request_state: _WebSocketRequestState,
        api_key: ApiKeyData | None,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
        reallocate_sticky: bool = False,
        sticky_max_age_seconds: int | None = None,
    ) -> tuple[Account | None, UpstreamResponsesWebSocket | None]:
        base_settings = self._proxy_runtime_settings()
        deadline = _websocket_connect_deadline(request_state, base_settings.proxy_request_budget_seconds)
        max_attempts = _WEBSOCKET_MAX_ACCOUNT_ATTEMPTS
        excluded_account_ids: set[str] = set()
        last_failover_exc: ProxyResponseError | None = None
        last_failover_account: Account | None = None
        for attempt in range(max_attempts):
            is_retry = attempt > 0
            account = await self._select_websocket_connect_account(
                deadline,
                sticky_key=sticky_key,
                sticky_kind=sticky_kind,
                prefer_earlier_reset=prefer_earlier_reset,
                routing_strategy=routing_strategy,
                model=model,
                request_state=request_state,
                api_key=api_key,
                client_send_lock=client_send_lock,
                websocket=websocket,
                reallocate_sticky=True if is_retry else reallocate_sticky,
                sticky_max_age_seconds=sticky_max_age_seconds,
                exclude_account_ids=excluded_account_ids,
                preferred_account_id=request_state.preferred_account_id,
                require_preferred_account=(
                    request_state.previous_response_id is not None and request_state.preferred_account_id is not None
                ),
            )
            if account is None:
                return None, None

            try:
                connect_result = await self._try_open_websocket_connect_attempt(
                    account,
                    headers,
                    deadline=deadline,
                    api_key=api_key,
                    request_state=request_state,
                    client_send_lock=client_send_lock,
                    websocket=websocket,
                )
            except ProxyResponseError as exc:
                action = await self._decide_websocket_failover_action(
                    account=account,
                    exc=exc,
                    request_state=request_state,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    deterministic_failover_enabled=getattr(base_settings, "deterministic_failover_enabled", True),
                )
                if action == "failover_next":
                    last_failover_exc = exc
                    last_failover_account = account
                    excluded_account_ids.add(account.id)
                    continue
                error = _parse_openai_error(exc.payload)
                error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                error_message = error.message if error else None
                await self._emit_websocket_connect_failure(
                    websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                    status_code=exc.status_code,
                    payload=exc.payload,
                    error_code=error_code or "upstream_error",
                    error_message=error_message or "Upstream error",
                )
                return None, None

            if connect_result is None:
                return None, None
            return connect_result

        if last_failover_exc is not None and last_failover_account is not None:
            error = _parse_openai_error(last_failover_exc.payload)
            error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
            error_message = error.message if error else None
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=last_failover_account.id,
                api_key=api_key,
                request_state=request_state,
                status_code=last_failover_exc.status_code,
                payload=last_failover_exc.payload,
                error_code=error_code or "upstream_error",
                error_message=error_message or "Upstream error",
            )
        return None, None

    async def _select_websocket_connect_account(
        self: _WebSocketConnectionService,
        deadline: float,
        *,
        sticky_key: str | None,
        sticky_kind: StickySessionKind | None,
        prefer_earlier_reset: bool,
        routing_strategy: RoutingStrategy,
        model: str | None,
        request_state: _WebSocketRequestState,
        api_key: ApiKeyData | None,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
        reallocate_sticky: bool,
        sticky_max_age_seconds: int | None,
        exclude_account_ids: set[str],
        preferred_account_id: str | None,
        require_preferred_account: bool,
    ) -> Account | None:
        try:
            selection = await self._select_account_with_budget_compatible(
                deadline,
                request_id=request_state.request_log_id or request_state.request_id,
                kind="websocket",
                api_key=api_key,
                sticky_key=sticky_key,
                sticky_kind=sticky_kind,
                reallocate_sticky=reallocate_sticky,
                sticky_max_age_seconds=sticky_max_age_seconds,
                prefer_earlier_reset_accounts=prefer_earlier_reset,
                routing_strategy=routing_strategy,
                model=model,
                exclude_account_ids=exclude_account_ids,
                preferred_account_id=preferred_account_id,
            )
        except ProxyResponseError as exc:
            if _is_proxy_budget_exhausted_error(exc):
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=None,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            raise

        account = selection.account
        if (
            account is not None
            and require_preferred_account
            and preferred_account_id is not None
            and account.id != preferred_account_id
        ):
            message = "Previous response owner account is unavailable; retry later."
            self._record_continuity_fail_closed_compatible(
                surface="websocket_connect",
                reason="owner_account_unavailable",
                previous_response_id=request_state.previous_response_id,
                session_id=request_state.session_id,
                upstream_error_code="upstream_unavailable",
            )
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=preferred_account_id,
                api_key=api_key,
                request_state=request_state,
                status_code=502,
                payload=openai_error(
                    "upstream_unavailable",
                    message,
                    error_type="server_error",
                ),
                error_code="upstream_unavailable",
                error_message=message,
            )
            return None
        if account:
            return account
        error_code = selection.error_code or "no_accounts"
        error_message = selection.error_message or "No active accounts available"
        if require_preferred_account and preferred_account_id is not None:
            message = "Previous response owner account is unavailable; retry later."
            self._record_continuity_fail_closed_compatible(
                surface="websocket_connect",
                reason="owner_account_unavailable",
                previous_response_id=request_state.previous_response_id,
                session_id=request_state.session_id,
                upstream_error_code=error_code,
            )
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=preferred_account_id,
                api_key=api_key,
                request_state=request_state,
                status_code=502,
                payload=openai_error(
                    "upstream_unavailable",
                    message,
                    error_type="server_error",
                ),
                error_code="upstream_unavailable",
                error_message=message,
            )
            return None
        await self._emit_websocket_connect_failure(
            websocket,
            client_send_lock=client_send_lock,
            account_id=None,
            api_key=api_key,
            request_state=request_state,
            status_code=503,
            payload=openai_error(
                error_code,
                error_message,
                error_type="server_error",
            ),
            error_code=error_code,
            error_message=error_message,
        )
        return None

    async def _try_open_websocket_connect_attempt(
        self: _WebSocketConnectionService,
        account: Account,
        headers: dict[str, str],
        *,
        deadline: float,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
    ) -> tuple[Account, UpstreamResponsesWebSocket] | None:
        try:
            remaining_budget = self._remaining_budget_seconds_compatible(deadline)
            if remaining_budget <= 0:
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            account = await self._ensure_fresh_with_budget(account, timeout_seconds=remaining_budget)

            remaining_budget = self._remaining_budget_seconds_compatible(deadline)
            if remaining_budget <= 0:
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            return account, await self._open_upstream_websocket_with_budget(
                account,
                headers,
                timeout_seconds=remaining_budget,
            )
        except ProxyResponseError as exc:
            if _is_proxy_budget_exhausted_error(exc):
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            if exc.status_code != 401:
                raise
            return await self._retry_websocket_connect_after_401(
                account,
                headers,
                deadline=deadline,
                api_key=api_key,
                request_state=request_state,
                client_send_lock=client_send_lock,
                websocket=websocket,
            )
        except RefreshError as exc:
            if exc.is_permanent:
                await self._load_balancer.mark_permanent_failure(account, exc.code)
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=account.id,
                api_key=api_key,
                request_state=request_state,
                status_code=401,
                payload=openai_error(
                    "invalid_api_key",
                    exc.message,
                    error_type="authentication_error",
                ),
                error_code="invalid_api_key",
                error_message=exc.message,
            )
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            message = str(exc) or "Request to upstream timed out"
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=account.id,
                api_key=api_key,
                request_state=request_state,
                status_code=502,
                payload=openai_error(
                    "upstream_unavailable",
                    message,
                    error_type="server_error",
                ),
                error_code="upstream_unavailable",
                error_message=message,
            )
            return None

    async def _retry_websocket_connect_after_401(
        self: _WebSocketConnectionService,
        account: Account,
        headers: dict[str, str],
        *,
        deadline: float,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        client_send_lock: anyio.Lock,
        websocket: WebSocket,
    ) -> tuple[Account, UpstreamResponsesWebSocket] | None:
        try:
            remaining_budget = self._remaining_budget_seconds_compatible(deadline)
            if remaining_budget <= 0:
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            account = await self._ensure_fresh_with_budget(
                account,
                force=True,
                timeout_seconds=remaining_budget,
            )
        except RefreshError as refresh_exc:
            if refresh_exc.is_permanent:
                await self._load_balancer.mark_permanent_failure(account, refresh_exc.code)
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=account.id,
                api_key=api_key,
                request_state=request_state,
                status_code=401,
                payload=openai_error(
                    "invalid_api_key",
                    refresh_exc.message,
                    error_type="authentication_error",
                ),
                error_code="invalid_api_key",
                error_message=refresh_exc.message,
            )
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as refresh_transport_exc:
            message = str(refresh_transport_exc) or "Request to upstream timed out"
            await self._emit_websocket_connect_failure(
                websocket,
                client_send_lock=client_send_lock,
                account_id=account.id,
                api_key=api_key,
                request_state=request_state,
                status_code=502,
                payload=openai_error(
                    "upstream_unavailable",
                    message,
                    error_type="server_error",
                ),
                error_code="upstream_unavailable",
                error_message=message,
            )
            return None

        try:
            remaining_budget = self._remaining_budget_seconds_compatible(deadline)
            if remaining_budget <= 0:
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            return account, await self._open_upstream_websocket_with_budget(
                account,
                headers,
                timeout_seconds=remaining_budget,
            )
        except ProxyResponseError as exc:
            if _is_proxy_budget_exhausted_error(exc):
                await self._emit_websocket_connect_timeout(
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    account_id=account.id,
                    api_key=api_key,
                    request_state=request_state,
                )
                return None
            raise

    async def _decide_websocket_failover_action(
        self: _WebSocketConnectionService,
        *,
        account: Account,
        exc: ProxyResponseError,
        request_state: _WebSocketRequestState,
        attempt: int,
        max_attempts: int,
        deterministic_failover_enabled: bool,
    ) -> str:
        classified = await self._handle_websocket_connect_error(account, exc)
        failure_class = classified["failure_class"] if isinstance(classified, dict) else "non_retryable"
        if deterministic_failover_enabled:
            action = failover_decision(
                failure_class=failure_class,
                downstream_visible=False,
                candidates_remaining=max_attempts - attempt,
            )
        else:
            action = "surface"
        logger.info(
            "Failover decision request_id=%s transport=websocket account_id=%s attempt=%d failure_class=%s action=%s",
            request_state.request_log_id or request_state.request_id,
            account.id,
            attempt,
            failure_class,
            action,
        )
        return action

    async def _emit_websocket_connect_timeout(
        self: _WebSocketConnectionService,
        *,
        websocket: WebSocket,
        client_send_lock: anyio.Lock,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
    ) -> None:
        await self._emit_websocket_proxy_request_timeout(
            websocket,
            client_send_lock=client_send_lock,
            account_id=account_id,
            api_key=api_key,
            request_state=request_state,
        )
