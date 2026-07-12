from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

import aiohttp
import anyio

from app.core.auth.refresh import RefreshError
from app.core.balancer import failover_decision
from app.core.balancer.types import ClassifiedFailure
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.db.models import Account, DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import (
    _headers_with_turn_state,
    _sticky_key_from_turn_state_header,
    _upstream_turn_state_from_socket,
)
from app.modules.proxy._service.budget import (
    _raise_proxy_budget_exhausted,
    _raise_proxy_unavailable,
    _remaining_budget_seconds,
    _websocket_connect_deadline,
)
from app.modules.proxy._service.http_bridge.keys import _http_bridge_key_strength
from app.modules.proxy._service.http_bridge.policy import (
    _http_bridge_client_kind,
    _should_reclaim_http_bridge_idle_session_for_selection,
)
from app.modules.proxy._service.observability import (
    _elapsed_ms,
    _hash_identifier,
    _record_bridge_first_turn_timeout,
    _record_same_account_takeover,
)
from app.modules.proxy._service.support import (
    _ACCOUNT_SELECTION_RECOVERABLE_WAIT_REASON,
    _REQUEST_TRANSPORT_HTTP,
    _WEBSOCKET_MAX_ACCOUNT_ATTEMPTS,
    _account_selection_wait_retry_after_seconds,
    _account_selection_wait_sleep_seconds,
    _AffinityPolicy,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _is_recoverable_account_selection_wait,
    _routing_strategy,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLease
from app.modules.proxy.load_balancer import AccountSelection

logger = logging.getLogger("app.modules.proxy.service")


@dataclass(slots=True)
class _HTTPBridgeSessionCreateTrace:
    started_at: float = field(default_factory=time.monotonic)
    select_account_ms: int = 0
    account_capacity_wait_ms: int = 0
    connect_concurrency_ms: int = 0
    ensure_fresh_ms: int = 0
    open_upstream_ws_ms: int = 0

    def add_elapsed(self, field_name: str, started_at: float) -> None:
        elapsed = _elapsed_ms(started_at, time.monotonic()) or 0
        setattr(self, field_name, getattr(self, field_name) + elapsed)


class _HTTPBridgeSessionCreateService(Protocol):
    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    @staticmethod
    async def _http_bridge_dashboard_settings() -> DashboardSettings: ...

    async def _select_account_with_budget_compatible(
        self,
        deadline: float,
        **kwargs: object,
    ) -> AccountSelection: ...

    def _try_acquire_http_bridge_session_account_model_concurrency(
        self,
        *,
        account: Account,
        model: str | None,
        request_id: str,
    ) -> AccountModelConcurrencyLease | None: ...

    async def _evict_http_bridge_idle_session_for_account_model_capacity(
        self,
        *,
        model: str | None,
        protected_key: _HTTPBridgeSessionKey,
        account_ids: set[str] | None = None,
    ) -> bool: ...

    def _try_acquire_http_bridge_connect_account_model_concurrency(
        self,
        *,
        account: Account,
        model: str | None,
        request_id: str,
    ) -> AccountModelConcurrencyLease | None: ...

    async def _acquire_http_bridge_connect_account_model_concurrency(
        self,
        *,
        account: Account,
        model: str | None,
        request_id: str,
        deadline: float,
    ) -> AccountModelConcurrencyLease: ...

    async def _ensure_fresh_with_budget(self, account: Account, *, timeout_seconds: float) -> Account: ...

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

    @staticmethod
    def _is_retryable_http_bridge_connect_forbidden(exc: ProxyResponseError) -> bool: ...

    @staticmethod
    def _http_bridge_connect_rejected_error() -> ProxyResponseError: ...

    async def _mark_http_bridge_account_permanent_failure(
        self,
        account: Account,
        error_code: str,
    ) -> None: ...

    async def _relay_http_bridge_upstream_messages(self, session: _HTTPBridgeSession) -> None: ...


class _HTTPBridgeSessionCreateMixin:
    async def _create_http_bridge_session(
        self: _HTTPBridgeSessionCreateService,
        key: _HTTPBridgeSessionKey,
        *,
        headers: dict[str, str],
        affinity: _AffinityPolicy,
        api_key: ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
        require_preferred_account: bool = False,
    ) -> _HTTPBridgeSession:
        request_state = _WebSocketRequestState(
            request_id=f"http_bridge_connect_{uuid4().hex}",
            model=request_model,
            service_tier=None,
            reasoning_effort=None,
            api_key_reservation=None,
            started_at=time.monotonic(),
            transport=_REQUEST_TRANSPORT_HTTP,
        )
        runtime_settings = self._http_bridge_runtime_settings()
        connect_budget_seconds = (
            runtime_settings.proxy_reconnect_request_budget_seconds
            if request_stage in {"reattach", "context_overflow_recover"}
            else runtime_settings.proxy_request_budget_seconds
        )
        deadline = _websocket_connect_deadline(request_state, connect_budget_seconds)
        settings = await self._http_bridge_dashboard_settings()
        excluded_account_ids: set[str] = set()
        retry_same_account_once = preferred_account_id is not None
        preferred_candidate_id = preferred_account_id
        attempt = 0
        last_connect_exc: ProxyResponseError | None = None
        session_account_model_concurrency: AccountModelConcurrencyLease | None = None
        trace = _HTTPBridgeSessionCreateTrace()
        account: Account
        connect_headers: dict[str, str]
        upstream: UpstreamResponsesWebSocket

        while attempt < _WEBSOCKET_MAX_ACCOUNT_ATTEMPTS:
            select_account_started_at = time.monotonic()
            selection = await self._select_account_with_budget_compatible(
                deadline,
                request_id=request_state.request_log_id or request_state.request_id,
                kind="http_bridge",
                request_stage=request_stage,
                api_key=api_key,
                sticky_key=affinity.key,
                sticky_kind=affinity.kind,
                reallocate_sticky=affinity.reallocate_sticky,
                sticky_max_age_seconds=affinity.max_age_seconds,
                prefer_earlier_reset_accounts=settings.prefer_earlier_reset_accounts,
                routing_strategy=_routing_strategy(settings),
                model=request_model,
                exclude_account_ids=excluded_account_ids,
                preferred_account_id=preferred_candidate_id,
            )
            trace.add_elapsed("select_account_ms", select_account_started_at)
            selected_account = selection.account
            if selected_account is None:
                _record_same_account_takeover(
                    preferred_account_id=preferred_account_id,
                    selected_account_id=None,
                )
                if _is_recoverable_account_selection_wait(selection):
                    remaining = _remaining_budget_seconds(deadline)
                    if remaining <= 0:
                        _raise_proxy_budget_exhausted()
                    logger.info(
                        "http_bridge_account_rate_limit_wait request_id=%s model=%s remaining_seconds=%.3f "
                        "retry_after_seconds=%s",
                        request_state.request_log_id or request_state.request_id,
                        request_model,
                        remaining,
                        selection.retry_after_seconds,
                    )
                    request_state.account_capacity_waiting = True
                    request_state.account_capacity_wait_reason = _ACCOUNT_SELECTION_RECOVERABLE_WAIT_REASON
                    request_state.account_capacity_wait_started_at = (
                        request_state.account_capacity_wait_started_at or time.monotonic()
                    )
                    request_state.account_capacity_wait_retry_after_seconds = (
                        _account_selection_wait_retry_after_seconds(selection, remaining)
                    )
                    wait_started_at = time.monotonic()
                    try:
                        await asyncio.sleep(_account_selection_wait_sleep_seconds(selection, remaining))
                    finally:
                        request_state.account_capacity_waiting = False
                        request_state.account_capacity_wait_reason = None
                        request_state.account_capacity_wait_retry_after_seconds = None
                    trace.add_elapsed("account_capacity_wait_ms", wait_started_at)
                    continue
                if _should_reclaim_http_bridge_idle_session_for_selection(selection):
                    reclaimed = await self._evict_http_bridge_idle_session_for_account_model_capacity(
                        model=request_model,
                        protected_key=key,
                    )
                    if reclaimed:
                        continue
                    remaining = _remaining_budget_seconds(deadline)
                    if remaining <= 0:
                        _raise_proxy_budget_exhausted()
                    logger.info(
                        "http_bridge_account_model_capacity_wait request_id=%s model=%s remaining_seconds=%.3f",
                        request_state.request_log_id or request_state.request_id,
                        request_model,
                        remaining,
                    )
                    wait_started_at = time.monotonic()
                    await asyncio.sleep(min(0.05, remaining))
                    trace.add_elapsed("account_capacity_wait_ms", wait_started_at)
                    continue
                raise ProxyResponseError(
                    503,
                    openai_error(
                        selection.error_code or "no_accounts",
                        selection.error_message or "No active accounts available",
                        error_type="server_error",
                    ),
                )
            account = selected_account
            if require_preferred_account and preferred_account_id is not None and account.id != preferred_account_id:
                _record_same_account_takeover(
                    preferred_account_id=preferred_account_id,
                    selected_account_id=account.id,
                )
                remaining = _remaining_budget_seconds(deadline)
                if remaining <= 0:
                    _raise_proxy_budget_exhausted()
                excluded_account_ids.discard(preferred_account_id)
                preferred_candidate_id = preferred_account_id
                logger.info(
                    "http_bridge_preferred_account_wait request_id=%s preferred_account_id=%s "
                    "selected_account_id=%s model=%s remaining_seconds=%.3f",
                    request_state.request_log_id or request_state.request_id,
                    preferred_account_id,
                    account.id,
                    request_model,
                    remaining,
                )
                wait_started_at = time.monotonic()
                await asyncio.sleep(min(0.05, remaining))
                trace.add_elapsed("account_capacity_wait_ms", wait_started_at)
                continue

            selected_is_preferred = preferred_account_id is not None and account.id == preferred_account_id
            session_account_model_concurrency = self._try_acquire_http_bridge_session_account_model_concurrency(
                account=account,
                model=request_model,
                request_id=request_state.request_log_id or request_state.request_id,
            )
            if session_account_model_concurrency is None:
                reclaimed = await self._evict_http_bridge_idle_session_for_account_model_capacity(
                    model=request_model,
                    protected_key=key,
                    account_ids={account.id},
                )
                if reclaimed:
                    continue
                remaining = _remaining_budget_seconds(deadline)
                if remaining <= 0:
                    _raise_proxy_budget_exhausted()
                if require_preferred_account and selected_is_preferred:
                    logger.info(
                        "http_bridge_preferred_account_model_session_wait "
                        "request_id=%s account_id=%s model=%s remaining_seconds=%.3f",
                        request_state.request_log_id or request_state.request_id,
                        account.id,
                        request_model,
                        remaining,
                    )
                    wait_started_at = time.monotonic()
                    await asyncio.sleep(min(0.05, remaining))
                    trace.add_elapsed("account_capacity_wait_ms", wait_started_at)
                    continue
                excluded_account_ids.add(account.id)
                if selected_is_preferred:
                    preferred_candidate_id = None
                continue

            try:
                connect_concurrency_started_at = time.monotonic()
                connect_account_model_concurrency = self._try_acquire_http_bridge_connect_account_model_concurrency(
                    account=account,
                    model=request_model,
                    request_id=request_state.request_log_id or request_state.request_id,
                )
                if connect_account_model_concurrency is None:
                    if require_preferred_account:
                        connect_account_model_concurrency = (
                            await self._acquire_http_bridge_connect_account_model_concurrency(
                                account=account,
                                model=request_model,
                                request_id=request_state.request_log_id or request_state.request_id,
                                deadline=deadline,
                            )
                        )
                    else:
                        session_account_model_concurrency.release()
                        session_account_model_concurrency = None
                        excluded_account_ids.add(account.id)
                        if selected_is_preferred:
                            preferred_candidate_id = None
                        logger.info(
                            "http_bridge_connect_account_model_capacity_rebalance request_id=%s account_id=%s model=%s",
                            request_state.request_log_id or request_state.request_id,
                            account.id,
                            request_model,
                        )
                        remaining = _remaining_budget_seconds(deadline)
                        if remaining <= 0:
                            _raise_proxy_budget_exhausted()
                        continue
                trace.add_elapsed("connect_concurrency_ms", connect_concurrency_started_at)
                attempt += 1
            except BaseException:
                if session_account_model_concurrency is not None:
                    session_account_model_concurrency.release()
                session_account_model_concurrency = None
                raise

            try:
                ensure_fresh_started_at = time.monotonic()
                account = await self._ensure_fresh_with_budget(
                    account,
                    timeout_seconds=_remaining_budget_seconds(deadline),
                )
                trace.add_elapsed("ensure_fresh_ms", ensure_fresh_started_at)
                connect_headers = _headers_with_turn_state(headers, _sticky_key_from_turn_state_header(headers))
                open_upstream_ws_started_at = time.monotonic()
                upstream = await self._open_upstream_websocket_with_budget(
                    account,
                    connect_headers,
                    timeout_seconds=_remaining_budget_seconds(deadline),
                )
                trace.add_elapsed("open_upstream_ws_ms", open_upstream_ws_started_at)
                _record_same_account_takeover(
                    preferred_account_id=preferred_account_id,
                    selected_account_id=account.id,
                )
                break
            except asyncio.CancelledError:
                session_account_model_concurrency.release()
                session_account_model_concurrency = None
                raise
            except ProxyResponseError as exc:
                session_account_model_concurrency.release()
                session_account_model_concurrency = None
                last_connect_exc = exc
                classified = await self._handle_websocket_connect_error(account, exc)
                failure_class = classified["failure_class"]
                action = (
                    "surface"
                    if require_preferred_account
                    else failover_decision(
                        failure_class=failure_class,
                        downstream_visible=False,
                        candidates_remaining=_WEBSOCKET_MAX_ACCOUNT_ATTEMPTS - attempt,
                    )
                )
                logger.info(
                    "Failover decision request_id=%s transport=http_bridge_connect account_id=%s "
                    "attempt=%d failure_class=%s action=%s",
                    request_state.request_log_id or request_state.request_id,
                    account.id,
                    attempt,
                    failure_class,
                    action,
                )
                remaining = _remaining_budget_seconds(deadline)
                if (
                    require_preferred_account
                    and self._is_retryable_http_bridge_connect_forbidden(exc)
                    and remaining > 0
                ):
                    logger.info(
                        "http_bridge_preferred_connect_rejected_retry request_id=%s account_id=%s "
                        "model=%s remaining_seconds=%.3f",
                        request_state.request_log_id or request_state.request_id,
                        account.id,
                        request_model,
                        remaining,
                    )
                    await asyncio.sleep(min(0.05, remaining))
                    continue
                if action == "failover_next" and remaining > 0:
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                if self._is_retryable_http_bridge_connect_forbidden(exc):
                    raise self._http_bridge_connect_rejected_error() from exc
                raise
            except RefreshError as exc:
                session_account_model_concurrency.release()
                session_account_model_concurrency = None
                if exc.is_permanent:
                    await self._mark_http_bridge_account_permanent_failure(account, exc.code)
                if selected_is_preferred and _remaining_budget_seconds(deadline) > 0:
                    if retry_same_account_once and not exc.is_permanent:
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                if exc.is_permanent:
                    raise ProxyResponseError(
                        401,
                        openai_error(
                            "invalid_api_key",
                            exc.message,
                            error_type="authentication_error",
                        ),
                    ) from exc
                if request_stage == "first_turn":
                    _record_bridge_first_turn_timeout()
                _raise_proxy_unavailable(exc.message or "Temporary upstream refresh failure")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                session_account_model_concurrency.release()
                session_account_model_concurrency = None
                if selected_is_preferred and _remaining_budget_seconds(deadline) > 0:
                    if retry_same_account_once:
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                if request_stage == "first_turn":
                    _record_bridge_first_turn_timeout()
                _raise_proxy_unavailable(str(exc) or "Request to upstream timed out")
            finally:
                connect_account_model_concurrency.release()
        else:
            if last_connect_exc is not None:
                if self._is_retryable_http_bridge_connect_forbidden(last_connect_exc):
                    raise self._http_bridge_connect_rejected_error() from last_connect_exc
                raise last_connect_exc
            _raise_proxy_unavailable("No active accounts available")

        assert session_account_model_concurrency is not None
        session = _HTTPBridgeSession(
            key=key,
            headers=connect_headers,
            affinity=affinity,
            api_key=api_key,
            request_model=request_model,
            account=account,
            upstream=upstream,
            upstream_control=_WebSocketUpstreamControl(),
            pending_requests=deque(),
            pending_lock=anyio.Lock(),
            response_create_gate=None,
            queued_request_count=0,
            last_used_at=time.monotonic(),
            idle_ttl_seconds=idle_ttl_seconds,
            codex_session=affinity.kind == StickySessionKind.CODEX_SESSION,
            prewarm_lock=anyio.Lock(),
            upstream_turn_state=_upstream_turn_state_from_socket(upstream),
            downstream_turn_state=None,
            account_model_session_lease=session_account_model_concurrency,
            client_kind=_http_bridge_client_kind(connect_headers, key=key),
        )
        session.upstream_reader = asyncio.create_task(self._relay_http_bridge_upstream_messages(session))
        logger.warning(
            "http_bridge_session_create_breakdown request_id=%s account_id=%s model=%s"
            " request_stage=%s total_ms=%s attempts=%s select_account_ms=%s account_capacity_wait_ms=%s"
            " connect_concurrency_ms=%s ensure_fresh_ms=%s open_upstream_ws_ms=%s"
            " bridge_kind=%s bridge_key=%s key_strength=%s preferred_account_id=%s require_preferred_account=%s",
            request_state.request_log_id or request_state.request_id,
            account.id,
            request_model,
            request_stage,
            _elapsed_ms(trace.started_at, time.monotonic()),
            attempt,
            trace.select_account_ms,
            trace.account_capacity_wait_ms,
            trace.connect_concurrency_ms,
            trace.ensure_fresh_ms,
            trace.open_upstream_ws_ms,
            key.affinity_kind,
            _hash_identifier(key.affinity_key),
            _http_bridge_key_strength(key),
            preferred_account_id,
            require_preferred_account,
        )
        return session
