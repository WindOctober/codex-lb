from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Protocol
from uuid import uuid4

import aiohttp
import anyio
from fastapi import WebSocket

from app.core.auth.refresh import RefreshError
from app.core.balancer import failover_decision
from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.core.openai.parsing import parse_sse_event
from app.core.resilience.overload import is_local_overload_error_code
from app.core.types import JsonValue
from app.core.utils.sse import parse_sse_data_json
from app.db.models import Account, DashboardSettings
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _extract_model_class,
    _headers_with_turn_state,
    _preferred_http_bridge_reconnect_turn_state,
    _upstream_turn_state_from_socket,
)
from app.modules.proxy._service.budget import (
    _ensure_request_budget_remaining,
    _http_bridge_request_budget_seconds,
    _raise_proxy_budget_exhausted,
    _remaining_budget_seconds,
    _request_deadline_at,
)
from app.modules.proxy._service.observability import (
    _log_http_bridge_event,
    _record_same_account_takeover,
)
from app.modules.proxy._service.service_tier import _http_bridge_text_with_account_service_tier
from app.modules.proxy._service.support import (
    _ACCOUNT_SELECTION_RECOVERABLE_WAIT_REASON,
    _ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS,
    _REQUEST_TRANSPORT_HTTP,
    _account_selection_wait_retry_after_seconds,
    _account_selection_wait_sleep_seconds,
    _await_operation_before_hard_timeout,
    _await_shielded_cleanup,
    _DiscardedAccountingResolution,
    _DiscardedRequestAccounting,
    _DownstreamWebSocketActivity,
    _event_type_from_payload,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _is_recoverable_account_selection_wait,
    _release_websocket_response_create_gate,
    _routing_strategy,
    _schedule_tracked_background_task,
    _should_retry_http_bridge_on_different_account,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy._service.websocket.events import (
    _http_error_status_from_payload,
    _pop_terminal_websocket_request_state,
)
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLease
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error
from app.modules.proxy.load_balancer import AccountSelection

logger = logging.getLogger("app.modules.proxy.service")

_HTTP_BRIDGE_RECONNECT_MAX_ACCOUNT_ATTEMPTS = 3
_HTTP_BRIDGE_DETACH_OBSERVATION_TIMEOUT_SECONDS = 1.0
_HTTP_BRIDGE_CLOSE_OBSERVATION_TIMEOUT_SECONDS = 1.0
_HTTP_BRIDGE_READER_CANCEL_OBSERVATION_TIMEOUT_SECONDS = 1.0


async def _close_http_bridge_upstream_before_deadline(
    upstream: UpstreamResponsesWebSocket,
    *,
    deadline: float,
    cleanup_tasks: set[asyncio.Task[None]],
    label: str,
) -> None:
    remaining = _remaining_budget_seconds(deadline)
    try:
        await _await_operation_before_hard_timeout(
            upstream.close(),
            timeout_seconds=max(
                0.000001,
                min(remaining, _HTTP_BRIDGE_CLOSE_OBSERVATION_TIMEOUT_SECONDS),
            ),
            tasks=cleanup_tasks,
            label=label,
        )
    except TimeoutError:
        logger.warning("HTTP bridge upstream close exceeded hard observation label=%s", label)


class _HTTPBridgeSharedRetryBlocked(Exception):
    pass


class _HTTPBridgeRequestSubmitService(Protocol):
    _http_bridge_lock: anyio.Lock
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

    @staticmethod
    def _http_bridge_codex_prewarm_enabled() -> bool: ...

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    @staticmethod
    async def _http_bridge_dashboard_settings() -> DashboardSettings: ...

    async def _cancel_http_bridge_upstream_reader(
        self,
        task: asyncio.Task[None],
        *,
        timeout_seconds: float,
    ) -> bool: ...

    async def _release_http_bridge_submit_lease(self, session: _HTTPBridgeSession) -> None: ...

    async def _fence_durable_http_bridge_session_before_submit(
        self,
        session: _HTTPBridgeSession,
    ) -> None: ...

    async def _retry_http_bridge_request_on_fresh_upstream(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
        send_request: bool = True,
        reset_response_state: bool = False,
        prefer_same_account: bool = True,
    ) -> bool: ...

    async def _reconnect_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        restart_reader: bool = False,
        prefer_same_account: bool = True,
        require_exclusive_request: bool = False,
    ) -> None: ...

    async def _reconnect_http_bridge_session_locked(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        restart_reader: bool = False,
        prefer_same_account: bool = True,
        require_exclusive_request: bool = False,
    ) -> None: ...

    def _rebind_request_account_model_concurrency(
        self,
        session: _HTTPBridgeSession,
        request_state: _WebSocketRequestState,
    ) -> None: ...

    async def _restore_http_bridge_session_after_reconnect(
        self,
        session: _HTTPBridgeSession,
        *,
        request_id: str,
        request_deadline_at: float,
    ) -> str | None: ...

    async def _await_http_bridge_detachment_before_deadline(
        self,
        detached: list[asyncio.Future[None]],
        *,
        request_deadline_at: float,
    ) -> None: ...

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
        request_deadline_at: float,
        account_ids: set[str] | None = None,
    ) -> bool: ...

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

    def _mark_http_bridge_account_permanent_failure(
        self,
        account: Account,
        error_code: str,
    ) -> None: ...

    async def _handle_websocket_connect_error(
        self,
        account: Account,
        exc: ProxyResponseError,
    ) -> ClassifiedFailure: ...

    async def _handle_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        http_status: int | None = None,
    ) -> ClassifiedFailure: ...

    def _classify_and_schedule_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        *,
        http_status: int | None = None,
        additional_error_count: int = 0,
    ) -> ClassifiedFailure: ...

    @staticmethod
    def _account_model_concurrency_overload(model: str | None) -> ProxyResponseError: ...

    @staticmethod
    def _is_retryable_http_bridge_connect_forbidden(exc: ProxyResponseError) -> bool: ...

    @staticmethod
    def _http_bridge_connect_rejected_error() -> ProxyResponseError: ...

    async def _relay_http_bridge_upstream_messages(self, session: _HTTPBridgeSession) -> None: ...

    async def _close_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
    ) -> None: ...

    def _schedule_http_bridge_session_close(
        self,
        session: _HTTPBridgeSession,
        *,
        reason: str,
        error_code: str | None = None,
        error_message: str | None = None,
        skip_reader_task: bool = False,
    ) -> asyncio.Future[None]: ...

    def _unregister_http_bridge_turn_states_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_previous_response_ids_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _detach_http_bridge_session_indexes_locked(self, session: _HTTPBridgeSession) -> bool: ...

    async def _evict_http_bridge_pressure(
        self,
        *,
        max_sessions: int,
        protected_key: _HTTPBridgeSessionKey,
        request_model: str | None,
        request_deadline_at: float,
        api_key: ApiKeyData | None = None,
    ) -> list[_HTTPBridgeSession]: ...

    async def _maybe_prewarm_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
    ) -> None: ...

    def _try_acquire_account_model_concurrency(
        self,
        *,
        account: Account,
        model: str | None,
        request_id: str,
        transport: str,
    ) -> AccountModelConcurrencyLease | None: ...

    async def _acquire_request_state_response_create_admission(
        self,
        request_state: _WebSocketRequestState,
        *,
        response_create_gate: asyncio.Semaphore | None,
        compact: bool = False,
    ) -> None: ...

    async def _cleanup_http_bridge_submit_interruption(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        gate_acquired: bool,
        request_enqueued: bool,
        queue_counted: bool,
        schedule_ambiguous_close: bool = True,
    ) -> None: ...

    async def _fail_http_bridge_replay_interruption(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
    ) -> None: ...

    async def _retire_ambiguous_http_bridge_replay_locked(
        self,
        session: _HTTPBridgeSession,
    ) -> None: ...

    async def _fail_pending_websocket_requests(
        self,
        *,
        account_id_value: str | None,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        error_code: str,
        error_message: str,
        api_key: ApiKeyData | None,
        websocket: WebSocket | None = None,
        client_send_lock: anyio.Lock | None = None,
        response_create_gate: asyncio.Semaphore | None = None,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None: ...

    def _release_request_account_model_concurrency(self, request_state: _WebSocketRequestState) -> None: ...

    async def _release_websocket_reservation(
        self,
        reservation: ApiKeyUsageReservationData | None,
    ) -> None: ...

    def _schedule_websocket_reservation_release(
        self,
        reservation: ApiKeyUsageReservationData | None,
        *,
        reason: str,
    ) -> None: ...

    async def _settle_or_release_failed_websocket_reservation(
        self,
        *,
        request_state: _WebSocketRequestState,
        reservation: ApiKeyUsageReservationData | None,
        api_key: ApiKeyData | None,
        error_code: str,
        error_message: str,
    ) -> None: ...

    async def _detach_http_bridge_request(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        schedule_ambiguous_close: bool = True,
    ) -> bool: ...


def _build_http_bridge_prewarm_text(text_data: str) -> str | None:
    try:
        payload = json.loads(text_data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("generate") is False:
        return None
    previous_response_id = payload.get("previous_response_id")
    if isinstance(previous_response_id, str) and previous_response_id.strip():
        return None
    warmup_payload = dict(payload)
    warmup_payload["generate"] = False
    return json.dumps(warmup_payload, ensure_ascii=True, separators=(",", ":"))


async def _send_http_bridge_request_before_deadline(
    session: _HTTPBridgeSession,
    request_state: _WebSocketRequestState,
    text_data: str,
    *,
    default_budget_seconds: float,
    cleanup_tasks: set[asyncio.Task[None]],
) -> None:
    remaining = _ensure_request_budget_remaining(request_state, default_budget_seconds)
    request_state.http_bridge_send_completed_at = None
    request_state.http_bridge_send_started_at = time.monotonic()
    try:
        await _await_operation_before_hard_timeout(
            session.upstream.send_text(text_data),
            timeout_seconds=remaining,
            tasks=cleanup_tasks,
            label=f"HTTP bridge send request_id={request_state.request_id}",
        )
    except TimeoutError:
        _raise_proxy_budget_exhausted()


async def _acquire_http_bridge_lock_before_deadline(
    lock: anyio.Lock,
    request_state: _WebSocketRequestState,
    *,
    default_budget_seconds: float,
    cleanup_tasks: set[asyncio.Task[None]],
) -> None:
    remaining = _ensure_request_budget_remaining(request_state, default_budget_seconds)
    try:
        # AnyIO locks are task-owned, so acquisition must happen in the caller
        # that will release the lock. Running acquire() in a helper task makes
        # the subsequent release fail even when the lock was acquired in time.
        with anyio.fail_after(remaining):
            await lock.acquire()
    except TimeoutError:
        _raise_proxy_budget_exhausted()


async def _acquire_response_create_before_deadline(
    service: _HTTPBridgeRequestSubmitService,
    request_state: _WebSocketRequestState,
    *,
    response_create_gate: asyncio.Semaphore | None,
    default_budget_seconds: float,
) -> None:
    remaining = _ensure_request_budget_remaining(request_state, default_budget_seconds)
    try:

        async def release_late_admission(_result: None) -> None:
            _release_websocket_response_create_gate(request_state, response_create_gate)

        await _await_operation_before_hard_timeout(
            service._acquire_request_state_response_create_admission(
                request_state,
                response_create_gate=response_create_gate,
            ),
            timeout_seconds=remaining,
            tasks=service._proxy_cleanup_tasks,
            label=f"HTTP bridge response-create admission request_id={request_state.request_id}",
            late_result_cleanup=release_late_admission,
        )
    except TimeoutError:
        _raise_proxy_budget_exhausted()


class _HTTPBridgeRequestSubmitMixin:
    async def _submit_http_bridge_request(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        release_submit_lease: bool = True,
    ) -> None:
        submit_lease_released = False
        gate_acquired = False
        request_enqueued = False
        queue_counted = False
        submitted = False

        async def release_submit_lease_once() -> None:
            nonlocal submit_lease_released
            if submit_lease_released:
                return
            try:
                if release_submit_lease:
                    await _await_shielded_cleanup(
                        self._release_http_bridge_submit_lease(session),
                        label="HTTP bridge submit lease release",
                    )
            finally:
                submit_lease_released = True

        request_state.http_bridge_submit_started_at = time.monotonic()
        try:
            runtime_settings = self._http_bridge_runtime_settings()
            request_budget_seconds = _http_bridge_request_budget_seconds(
                session,
                request_state,
                runtime_settings,
            )
            _ensure_request_budget_remaining(request_state, request_budget_seconds)
            async with self._http_bridge_lock:
                registered_session = self._http_bridge_sessions.get(session.key)
            if registered_session is not None and registered_session is not session:
                session.closed = True
                session.pending_changed.set()
                detached = self._schedule_http_bridge_session_close(
                    session,
                    reason="detached-key-conflict",
                )
                await self._await_http_bridge_detachment_before_deadline(
                    [detached],
                    request_deadline_at=_request_deadline_at(request_state, request_budget_seconds),
                )
                raise ProxyResponseError(
                    502,
                    openai_error("upstream_unavailable", "HTTP responses session bridge key is already owned"),
                )

            await self._fence_durable_http_bridge_session_before_submit(session)

            text_data, forwarded_service_tier = _http_bridge_text_with_account_service_tier(
                text_data,
                session.account,
                api_key=request_state.api_key,
            )
            if forwarded_service_tier is not None:
                request_state.service_tier = forwarded_service_tier
                request_state.requested_service_tier = forwarded_service_tier
            if session.closed:
                await _acquire_http_bridge_lock_before_deadline(
                    session.lifecycle_lock,
                    request_state,
                    default_budget_seconds=request_budget_seconds,
                    cleanup_tasks=self._proxy_cleanup_tasks,
                )
                try:
                    closed_after_lifecycle_settled = session.closed
                finally:
                    session.lifecycle_lock.release()
                if closed_after_lifecycle_settled:
                    recovered = await self._retry_http_bridge_request_on_fresh_upstream(
                        session,
                        request_state=request_state,
                        text_data=text_data,
                        send_request=False,
                    )
                    if recovered:
                        session.closed = False
                        restore_failure = await self._restore_http_bridge_session_after_reconnect(
                            session,
                            request_id=request_state.request_id,
                            request_deadline_at=_request_deadline_at(request_state, request_budget_seconds),
                        )
                        if restore_failure is not None:
                            session.closed = True
                            session.pending_changed.set()
                            detached = self._schedule_http_bridge_session_close(
                                session,
                                reason=f"restore-after-reconnect-{restore_failure}",
                            )
                            await self._await_http_bridge_detachment_before_deadline(
                                [detached],
                                request_deadline_at=_request_deadline_at(request_state, request_budget_seconds),
                            )
                            _log_http_bridge_event(
                                "submit_on_closed",
                                session.key,
                                account_id=session.account.id,
                                model=session.request_model,
                                detail=f"session_restore_after_reconnect_failed:{restore_failure}",
                                cache_key_family=session.key.affinity_kind,
                                model_class=(
                                    _extract_model_class(session.request_model) if session.request_model else None
                                ),
                            )
                            raise ProxyResponseError(
                                502,
                                openai_error("upstream_unavailable", "HTTP responses session bridge is closed"),
                            )
                        if session.upstream_reader is None or session.upstream_reader.done():
                            session.upstream_reader = asyncio.create_task(
                                self._relay_http_bridge_upstream_messages(session)
                            )
                    else:
                        _log_http_bridge_event(
                            "submit_on_closed",
                            session.key,
                            account_id=session.account.id,
                            model=session.request_model,
                            cache_key_family=session.key.affinity_kind,
                            model_class=_extract_model_class(session.request_model) if session.request_model else None,
                        )
                        raise ProxyResponseError(
                            502,
                            openai_error("upstream_unavailable", "HTTP responses session bridge is closed"),
                        )
            if session.closed:
                _log_http_bridge_event(
                    "submit_on_closed",
                    session.key,
                    account_id=session.account.id,
                    model=session.request_model,
                    detail="session_closed_after_reconnect",
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )
                raise ProxyResponseError(
                    502,
                    openai_error("upstream_unavailable", "HTTP responses session bridge is closed"),
                )
            await self._evict_http_bridge_pressure(
                max_sessions=0,
                protected_key=session.key,
                request_model=session.request_model,
                request_deadline_at=_request_deadline_at(request_state, request_budget_seconds),
                api_key=session.api_key,
            )
            await self._maybe_prewarm_http_bridge_session(
                session,
                request_state=request_state,
                text_data=text_data,
            )
            _ensure_request_budget_remaining(request_state, request_budget_seconds)
            request_state.account_model_concurrency = self._try_acquire_account_model_concurrency(
                account=session.account,
                model=session.request_model,
                request_id=request_state.request_id,
                transport=_REQUEST_TRANSPORT_HTTP,
            )
            if request_state.account_model_concurrency is None:
                raise self._account_model_concurrency_overload(session.request_model)
            async with session.pending_lock:
                if queue_limit > 0 and session.queued_request_count >= queue_limit:
                    _log_http_bridge_event(
                        "queue_full",
                        session.key,
                        account_id=session.account.id,
                        model=session.request_model,
                        pending_count=session.queued_request_count,
                        cache_key_family=session.key.affinity_kind,
                        model_class=_extract_model_class(session.request_model) if session.request_model else None,
                    )
                    self._release_request_account_model_concurrency(request_state)
                    raise ProxyResponseError(
                        429,
                        openai_error(
                            "rate_limit_exceeded",
                            "HTTP responses session bridge queue is full",
                            error_type="rate_limit_error",
                        ),
                    )
                session.queued_request_count += 1
                queue_counted = True
            await release_submit_lease_once()
            try:
                await _acquire_response_create_before_deadline(
                    self,
                    request_state,
                    response_create_gate=session.response_create_gate,
                    default_budget_seconds=request_budget_seconds,
                )
                gate_acquired = True
                _ensure_request_budget_remaining(request_state, request_budget_seconds)
                await _acquire_http_bridge_lock_before_deadline(
                    session.lifecycle_lock,
                    request_state,
                    default_budget_seconds=request_budget_seconds,
                    cleanup_tasks=self._proxy_cleanup_tasks,
                )
                try:
                    _ensure_request_budget_remaining(request_state, request_budget_seconds)
                    async with self._http_bridge_lock:
                        current_session = self._http_bridge_sessions.get(session.key)
                    session_replaced = current_session is not None and current_session is not session
                    session_unregistered = current_session is None
                    detached_soft_submit = (
                        not session.closed and session.key.strength == "soft" and session_unregistered
                    )
                    if session.closed or ((session_replaced or session_unregistered) and not detached_soft_submit):
                        _log_http_bridge_event(
                            "submit_on_closed",
                            session.key,
                            account_id=session.account.id,
                            model=session.request_model,
                            detail=(
                                "session_retired_after_admission"
                                if session.closed
                                else (
                                    "session_replaced_after_admission"
                                    if session_replaced
                                    else "session_unregistered_after_admission"
                                )
                            ),
                            cache_key_family=session.key.affinity_kind,
                            model_class=_extract_model_class(session.request_model) if session.request_model else None,
                        )
                        raise ProxyResponseError(
                            502,
                            openai_error("upstream_unavailable", "HTTP responses session bridge is closed"),
                        )
                    _ensure_request_budget_remaining(request_state, request_budget_seconds)
                    async with session.pending_lock:
                        session.pending_requests.append(request_state)
                        session.pending_changed.set()
                    request_enqueued = True
                    await _send_http_bridge_request_before_deadline(
                        session,
                        request_state,
                        text_data,
                        default_budget_seconds=request_budget_seconds,
                        cleanup_tasks=self._proxy_cleanup_tasks,
                    )
                    request_state.http_bridge_send_completed_at = time.monotonic()
                    session.last_used_at = time.monotonic()
                    submitted = True
                finally:
                    session.lifecycle_lock.release()
            except (ProxyResponseError, asyncio.CancelledError):
                raise
            except Exception as exc:
                _log_http_bridge_event(
                    "send_failure",
                    session.key,
                    account_id=session.account.id,
                    model=session.request_model,
                    detail=str(exc) or None,
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )
                send_is_ambiguous = request_state.http_bridge_send_started_at is not None
                retried = False
                if not send_is_ambiguous:
                    retried = await self._retry_http_bridge_request_on_fresh_upstream(
                        session,
                        request_state=request_state,
                        text_data=text_data,
                    )
                if retried:
                    submitted = True
                    return
                await self._cleanup_http_bridge_submit_interruption(
                    session,
                    request_state=request_state,
                    gate_acquired=gate_acquired,
                    request_enqueued=request_enqueued,
                    queue_counted=queue_counted,
                    schedule_ambiguous_close=False,
                )
                queue_counted = False
                request_enqueued = False
                gate_acquired = False
                if not send_is_ambiguous:
                    await self._fail_pending_websocket_requests(
                        account_id_value=session.account.id,
                        pending_requests=deque([request_state]),
                        pending_lock=anyio.Lock(),
                        error_code="stream_incomplete",
                        error_message="Upstream websocket closed before response.completed",
                        api_key=None,
                        response_create_gate=session.response_create_gate,
                    )
                session.closed = True
                async with self._http_bridge_lock:
                    self._detach_http_bridge_session_indexes_locked(session)
                    self._schedule_http_bridge_session_close(
                        session,
                        reason="initial-send-failure",
                        error_code="stream_incomplete",
                        error_message="HTTP bridge upstream send failed before response.completed",
                    )
                raise ProxyResponseError(
                    502,
                    openai_error("upstream_unavailable", "HTTP bridge upstream send failed"),
                ) from exc
        except BaseException:
            if not submitted and (
                queue_counted
                or request_enqueued
                or gate_acquired
                or request_state.account_model_concurrency is not None
            ):
                await _await_shielded_cleanup(
                    self._cleanup_http_bridge_submit_interruption(
                        session,
                        request_state=request_state,
                        gate_acquired=gate_acquired,
                        request_enqueued=request_enqueued,
                        queue_counted=queue_counted,
                    ),
                    label="HTTP bridge interrupted submit cleanup",
                )
            raise
        finally:
            await release_submit_lease_once()

    async def _maybe_prewarm_http_bridge_session(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
    ) -> None:
        if (
            not session.codex_session
            or session.prewarmed
            or request_state.previous_response_id is not None
            or not self._http_bridge_codex_prewarm_enabled()
        ):
            return
        prewarm_lock = session.prewarm_lock
        if prewarm_lock is None:
            return
        request_budget_seconds = _http_bridge_request_budget_seconds(
            session,
            request_state,
            self._http_bridge_runtime_settings(),
        )
        await _acquire_http_bridge_lock_before_deadline(
            prewarm_lock,
            request_state,
            default_budget_seconds=request_budget_seconds,
            cleanup_tasks=self._proxy_cleanup_tasks,
        )
        try:
            if session.prewarmed:
                return
            warmup_text = _build_http_bridge_prewarm_text(text_data)
            session.prewarmed = True
            if warmup_text is None:
                return

            warmup_state = _WebSocketRequestState(
                request_id=f"http_prewarm_{uuid4().hex}",
                model=request_state.model,
                service_tier=request_state.service_tier,
                reasoning_effort=request_state.reasoning_effort,
                api_key_reservation=None,
                started_at=request_state.started_at,
                request_budget_seconds=request_state.request_budget_seconds,
                request_deadline_at=request_state.request_deadline_at,
                requested_service_tier=request_state.requested_service_tier,
                actual_service_tier=request_state.actual_service_tier,
                awaiting_response_created=True,
                event_queue=asyncio.Queue(),
                transport=_REQUEST_TRANSPORT_HTTP,
                request_text=warmup_text,
                skip_request_log=True,
            )
            gate_acquired = False
            request_enqueued = False
            queue_counted = False
            try:
                event_queue = warmup_state.event_queue
                assert event_queue is not None
                await _acquire_response_create_before_deadline(
                    self,
                    warmup_state,
                    response_create_gate=session.response_create_gate,
                    default_budget_seconds=request_budget_seconds,
                )
                gate_acquired = True
                await _acquire_http_bridge_lock_before_deadline(
                    session.lifecycle_lock,
                    warmup_state,
                    default_budget_seconds=request_budget_seconds,
                    cleanup_tasks=self._proxy_cleanup_tasks,
                )
                try:
                    _ensure_request_budget_remaining(warmup_state, request_budget_seconds)
                    if session.closed:
                        raise ProxyResponseError(
                            502,
                            openai_error("upstream_unavailable", "HTTP responses session bridge is closed"),
                        )
                    async with session.pending_lock:
                        session.pending_requests.append(warmup_state)
                        session.queued_request_count += 1
                        session.pending_changed.set()
                        queue_counted = True
                    request_enqueued = True
                    await _send_http_bridge_request_before_deadline(
                        session,
                        warmup_state,
                        warmup_text,
                        default_budget_seconds=request_budget_seconds,
                        cleanup_tasks=self._proxy_cleanup_tasks,
                    )
                    warmup_state.http_bridge_send_completed_at = time.monotonic()
                finally:
                    session.lifecycle_lock.release()
                while True:
                    remaining = _ensure_request_budget_remaining(warmup_state, request_budget_seconds)
                    try:
                        event_block = await asyncio.wait_for(event_queue.get(), timeout=remaining)
                    except TimeoutError:
                        _raise_proxy_budget_exhausted()
                    if event_block is None:
                        break
                    payload = parse_sse_data_json(event_block)
                    event = parse_sse_event(event_block)
                    event_type = _event_type_from_payload(event, payload)
                    if event_type in {"response.failed", "response.incomplete", "error"}:
                        raise ProxyResponseError(
                            502,
                            openai_error(
                                "upstream_unavailable",
                                "HTTP responses session bridge prewarm failed",
                            ),
                        )
                session.last_used_at = time.monotonic()
            except ProxyResponseError as exc:
                error = _parse_openai_error(exc.payload)
                code = _normalize_error_code(error.code if error else None, error.type if error else None)
                await self._cleanup_http_bridge_submit_interruption(
                    session,
                    request_state=warmup_state,
                    gate_acquired=gate_acquired,
                    request_enqueued=request_enqueued,
                    queue_counted=queue_counted,
                )
                if is_local_overload_error_code(code):
                    session.prewarmed = False
                    return
                session.prewarmed = False
                raise
            except BaseException:
                session.prewarmed = False
                await _await_shielded_cleanup(
                    self._cleanup_http_bridge_submit_interruption(
                        session,
                        request_state=warmup_state,
                        gate_acquired=gate_acquired,
                        request_enqueued=request_enqueued,
                        queue_counted=queue_counted,
                    ),
                    label="HTTP bridge prewarm interruption cleanup",
                )
                raise
        finally:
            prewarm_lock.release()

    async def _retry_http_bridge_request_on_fresh_upstream(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
        send_request: bool = True,
        reset_response_state: bool = False,
        prefer_same_account: bool = True,
    ) -> bool:
        runtime_settings = self._http_bridge_runtime_settings()
        request_budget_seconds = _http_bridge_request_budget_seconds(
            session,
            request_state,
            runtime_settings,
        )
        retry_text_data = text_data
        if request_state.previous_response_id is not None and send_request:
            if (
                not request_state.proxy_injected_previous_response_id
                or not request_state.fresh_upstream_request_text
                or not request_state.fresh_upstream_request_is_retry_safe
            ):
                return False
            retry_text_data = request_state.fresh_upstream_request_text
        original_response_id = request_state.response_id
        original_awaiting_response_created = request_state.awaiting_response_created
        _log_http_bridge_event(
            "retry_fresh_upstream",
            session.key,
            account_id=session.account.id,
            model=session.request_model,
            pending_count=1,
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )
        try:
            await _acquire_http_bridge_lock_before_deadline(
                session.lifecycle_lock,
                request_state,
                default_budget_seconds=request_budget_seconds,
                cleanup_tasks=self._proxy_cleanup_tasks,
            )
            try:
                if not send_request and not session.closed:
                    return True
                if request_state.replay_count >= 1:
                    return False
                request_state.replay_count += 1
                if reset_response_state:
                    request_state.response_id = None
                    request_state.awaiting_response_created = True
                session.defer_reader_start = not send_request
                try:
                    await self._reconnect_http_bridge_session_locked(
                        session,
                        request_state=request_state,
                        restart_reader=True,
                        prefer_same_account=prefer_same_account,
                        require_exclusive_request=send_request,
                    )
                finally:
                    session.defer_reader_start = False
                if send_request:
                    retry_text_data, forwarded_service_tier = _http_bridge_text_with_account_service_tier(
                        retry_text_data,
                        session.account,
                        api_key=request_state.api_key,
                    )
                    if forwarded_service_tier is not None:
                        request_state.service_tier = forwarded_service_tier
                        request_state.requested_service_tier = forwarded_service_tier
                    request_state.request_text = retry_text_data
                    self._rebind_request_account_model_concurrency(session, request_state)
                    _release_websocket_response_create_gate(request_state, session.response_create_gate)
                    await _acquire_response_create_before_deadline(
                        self,
                        request_state,
                        response_create_gate=session.response_create_gate,
                        default_budget_seconds=request_budget_seconds,
                    )
                    _ensure_request_budget_remaining(request_state, request_budget_seconds)
                    if retry_text_data != text_data:
                        request_state.previous_response_id = None
                        request_state.proxy_injected_previous_response_id = False
                        request_state.request_text = retry_text_data
                    remaining = _ensure_request_budget_remaining(request_state, request_budget_seconds)
                    request_state.http_bridge_send_started_at = time.monotonic()
                    try:
                        try:
                            await _await_operation_before_hard_timeout(
                                session.upstream.send_text(retry_text_data),
                                timeout_seconds=remaining,
                                tasks=self._proxy_cleanup_tasks,
                                label=f"HTTP bridge replay send request_id={request_state.request_id}",
                            )
                        except TimeoutError:
                            _raise_proxy_budget_exhausted()
                    except BaseException:
                        await self._retire_ambiguous_http_bridge_replay_locked(session)
                        raise
                sent_at = time.monotonic()
                if send_request:
                    request_state.http_bridge_send_completed_at = sent_at
                    session.pending_changed.set()
                session.last_used_at = sent_at
                return True
            finally:
                session.lifecycle_lock.release()
        except _HTTPBridgeSharedRetryBlocked:
            if reset_response_state:
                request_state.response_id = original_response_id
                request_state.awaiting_response_created = original_awaiting_response_created
            return False
        except asyncio.CancelledError:
            if send_request:
                await self._fail_http_bridge_replay_interruption(
                    session,
                    request_state=request_state,
                )
            raise
        except Exception:
            if reset_response_state:
                request_state.response_id = original_response_id
                request_state.awaiting_response_created = original_awaiting_response_created
            logger.warning("HTTP bridge retry on fresh upstream failed", exc_info=True)
            return False

    async def _retire_ambiguous_http_bridge_replay_locked(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
    ) -> None:
        session.closed = True
        session.pending_changed.set()
        skip_reader_task = session.upstream_reader is asyncio.current_task()

        async with self._http_bridge_lock:
            self._detach_http_bridge_session_indexes_locked(session)
            self._schedule_http_bridge_session_close(
                session,
                reason="ambiguous-replay-send",
                skip_reader_task=skip_reader_task,
            )

    def _rebind_request_account_model_concurrency(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        request_state: _WebSocketRequestState,
    ) -> None:
        previous_lease = request_state.account_model_concurrency
        if previous_lease is not None and previous_lease.account_id == session.account.id:
            return
        replacement_lease = self._try_acquire_account_model_concurrency(
            account=session.account,
            model=session.request_model,
            request_id=request_state.request_id,
            transport=_REQUEST_TRANSPORT_HTTP,
        )
        if replacement_lease is None:
            raise self._account_model_concurrency_overload(session.request_model)
        request_state.account_model_concurrency = replacement_lease
        if previous_lease is not None:
            previous_lease.release()

    async def _reconnect_http_bridge_session(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        restart_reader: bool = False,
        prefer_same_account: bool = True,
        require_exclusive_request: bool = False,
    ) -> None:
        request_budget_seconds = _http_bridge_request_budget_seconds(
            session,
            request_state,
            self._http_bridge_runtime_settings(),
        )
        await _acquire_http_bridge_lock_before_deadline(
            session.lifecycle_lock,
            request_state,
            default_budget_seconds=request_budget_seconds,
            cleanup_tasks=self._proxy_cleanup_tasks,
        )
        try:
            await self._reconnect_http_bridge_session_locked(
                session,
                request_state=request_state,
                restart_reader=restart_reader,
                prefer_same_account=prefer_same_account,
                require_exclusive_request=require_exclusive_request,
            )
        finally:
            session.lifecycle_lock.release()

    async def _reconnect_http_bridge_session_locked(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        restart_reader: bool = False,
        prefer_same_account: bool = True,
        require_exclusive_request: bool = False,
    ) -> None:
        runtime_settings = self._http_bridge_runtime_settings()
        if require_exclusive_request:
            async with session.pending_lock:
                if request_state not in session.pending_requests or any(
                    pending_request is not request_state for pending_request in session.pending_requests
                ):
                    raise _HTTPBridgeSharedRetryBlocked()
        now = time.monotonic()
        request_deadline = _request_deadline_at(
            request_state,
            _http_bridge_request_budget_seconds(session, request_state, runtime_settings),
        )
        deadline = min(
            request_deadline,
            now + runtime_settings.proxy_reconnect_request_budget_seconds,
        )
        if deadline <= now:
            _raise_proxy_budget_exhausted()

        old_account_id = session.account.id
        old_upstream = session.upstream
        old_reader = session.upstream_reader if restart_reader else None
        current_task = asyncio.current_task()
        reconnecting_reader_owns_session = old_reader is not None and old_reader is current_task

        reader_retirement_scheduled = False

        def schedule_reader_owned_session_retirement() -> None:
            nonlocal reader_retirement_scheduled
            if reader_retirement_scheduled:
                return
            reader_retirement_scheduled = True
            session.closed = True
            session.pending_changed.set()

            async def retire_reader_owned_session() -> None:
                async with self._http_bridge_lock:
                    detached = self._detach_http_bridge_session_indexes_locked(session)
                    if detached:
                        self._schedule_http_bridge_session_close(
                            session,
                            reason="reconnect-reader-cancellation-timeout",
                            error_code="upstream_unavailable",
                            error_message=(
                                "HTTP responses session bridge reader did not shut down cleanly"
                            ),
                        )

            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                retire_reader_owned_session(),
                name=f"http-bridge-reader-retire-{request_state.request_id}",
                label=(
                    "HTTP bridge reader-timeout retirement "
                    f"request_id={request_state.request_id}"
                ),
            )

        if _remaining_budget_seconds(deadline) <= 0:
            _raise_proxy_budget_exhausted()
        if old_reader is not None and not reconnecting_reader_owns_session:
            try:
                cancelled = await self._cancel_http_bridge_upstream_reader(
                    old_reader,
                    timeout_seconds=max(
                        0.000001,
                        min(
                            _remaining_budget_seconds(deadline),
                            _HTTP_BRIDGE_READER_CANCEL_OBSERVATION_TIMEOUT_SECONDS,
                        ),
                    ),
                )
            except asyncio.CancelledError:
                schedule_reader_owned_session_retirement()
                raise
            if not cancelled:
                schedule_reader_owned_session_retirement()
                raise ProxyResponseError(
                    502,
                    openai_error(
                        "upstream_unavailable",
                        "HTTP responses session bridge reader did not shut down cleanly",
                    ),
                )
        if _remaining_budget_seconds(deadline) <= 0:
            if old_reader is not None and not reconnecting_reader_owns_session:
                session.closed = False
                session.upstream_reader = asyncio.create_task(self._relay_http_bridge_upstream_messages(session))
            _raise_proxy_budget_exhausted()
        if not session.upstream_close_owned:
            session.upstream_close_owned = True
            try:
                await _close_http_bridge_upstream_before_deadline(
                    old_upstream,
                    deadline=deadline,
                    cleanup_tasks=self._proxy_cleanup_tasks,
                    label=f"HTTP bridge old websocket close request_id={request_state.request_id}",
                )
            except Exception:
                logger.debug("Failed to close HTTP bridge upstream websocket before reconnect", exc_info=True)
        session.closed = True

        remaining = _remaining_budget_seconds(deadline)
        if remaining <= 0:
            _raise_proxy_budget_exhausted()
        try:
            settings = await _await_operation_before_hard_timeout(
                self._http_bridge_dashboard_settings(),
                timeout_seconds=remaining,
                tasks=self._proxy_cleanup_tasks,
                label=f"HTTP bridge reconnect dashboard lookup request_id={request_state.request_id}",
            )
        except TimeoutError:
            _raise_proxy_budget_exhausted()
        if _remaining_budget_seconds(deadline) <= 0:
            _raise_proxy_budget_exhausted()
        session.api_key = request_state.api_key
        excluded_account_ids: set[str] = set()
        if not prefer_same_account:
            excluded_account_ids.add(session.account.id)
        retry_same_account_once = True
        preferred_candidate_id: str | None = session.account.id if prefer_same_account else None
        while True:
            selection = await self._select_account_with_budget_compatible(
                deadline,
                request_id=request_state.request_log_id or request_state.request_id,
                kind="http_bridge",
                request_stage="reattach",
                api_key=session.api_key,
                sticky_key=session.affinity.key,
                sticky_kind=session.affinity.kind,
                reallocate_sticky=session.affinity.reallocate_sticky,
                sticky_max_age_seconds=session.affinity.max_age_seconds,
                prefer_earlier_reset_accounts=settings.prefer_earlier_reset_accounts,
                routing_strategy=_routing_strategy(settings),
                model=session.request_model,
                exclude_account_ids=excluded_account_ids,
                preferred_account_id=preferred_candidate_id,
                held_http_bridge_session_account_id=session.account.id,
                required_upstream_wire_api=session.affinity.required_upstream_wire_api,
            )
            account = selection.account
            if account is None:
                _record_same_account_takeover(
                    preferred_account_id=session.account.id,
                    selected_account_id=None,
                )
                if is_local_overload_error_code(selection.error_code):
                    remaining = _remaining_budget_seconds(deadline)
                    if remaining <= 0:
                        raise self._account_model_concurrency_overload(session.request_model)
                    logger.info(
                        "http_bridge_reconnect_account_model_capacity_wait "
                        "request_id=%s model=%s remaining_seconds=%.3f",
                        request_state.request_log_id or request_state.request_id,
                        session.request_model,
                        remaining,
                    )
                    request_state.account_capacity_waiting = True
                    request_state.account_capacity_wait_reason = "waiting for account model session capacity"
                    request_state.account_capacity_wait_started_at = (
                        request_state.account_capacity_wait_started_at or time.monotonic()
                    )
                    request_state.account_capacity_wait_retry_after_seconds = min(
                        _ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS,
                        remaining,
                    )
                    try:
                        await asyncio.sleep(min(0.05, remaining))
                    finally:
                        request_state.account_capacity_waiting = False
                        request_state.account_capacity_wait_reason = None
                        request_state.account_capacity_wait_retry_after_seconds = None
                    continue
                if _is_recoverable_account_selection_wait(selection):
                    remaining = _remaining_budget_seconds(deadline)
                    if remaining <= 0:
                        _raise_proxy_budget_exhausted()
                    logger.info(
                        "http_bridge_reconnect_account_rate_limit_wait "
                        "request_id=%s model=%s remaining_seconds=%.3f retry_after_seconds=%s",
                        request_state.request_log_id or request_state.request_id,
                        session.request_model,
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
                    try:
                        await asyncio.sleep(_account_selection_wait_sleep_seconds(selection, remaining))
                    finally:
                        request_state.account_capacity_waiting = False
                        request_state.account_capacity_wait_reason = None
                        request_state.account_capacity_wait_retry_after_seconds = None
                    continue
                raise ProxyResponseError(
                    503,
                    openai_error(
                        selection.error_code or "no_accounts",
                        selection.error_message or "No active accounts available",
                        error_type="server_error",
                    ),
                )
            selected_is_preferred = account.id == session.account.id
            new_session_lease: AccountModelConcurrencyLease | None = None
            session_lease_required = account.id != session.account.id or session.account_model_session_lease is None
            if session_lease_required:
                new_session_lease = self._try_acquire_http_bridge_session_account_model_concurrency(
                    account=account,
                    model=session.request_model,
                    request_id=request_state.request_log_id or request_state.request_id,
                )
                if new_session_lease is None:
                    reclaimed = await self._evict_http_bridge_idle_session_for_account_model_capacity(
                        model=session.request_model,
                        protected_key=session.key,
                        request_deadline_at=deadline,
                        account_ids={account.id},
                    )
                    if reclaimed:
                        continue
                    excluded_account_ids.add(account.id)
                    if selected_is_preferred:
                        preferred_candidate_id = None
                    remaining = _remaining_budget_seconds(deadline)
                    if remaining <= 0:
                        raise self._account_model_concurrency_overload(session.request_model)
                    continue
            try:
                account = await self._ensure_fresh_with_budget(
                    account,
                    timeout_seconds=_remaining_budget_seconds(deadline),
                )
                connect_headers = _headers_with_turn_state(
                    session.headers,
                    _preferred_http_bridge_reconnect_turn_state(session),
                )
                upstream = await self._open_upstream_websocket_with_budget(
                    account,
                    connect_headers,
                    timeout_seconds=_remaining_budget_seconds(deadline),
                )
            except asyncio.CancelledError:
                if new_session_lease is not None:
                    new_session_lease.release()
                raise
            except RefreshError as exc:
                if new_session_lease is not None:
                    new_session_lease.release()
                if exc.is_permanent:
                    self._mark_http_bridge_account_permanent_failure(account, exc.code)
                remaining = _remaining_budget_seconds(deadline)
                if remaining > 0:
                    if retry_same_account_once and not exc.is_permanent:
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                raise ProxyResponseError(
                    401 if exc.is_permanent else 502,
                    openai_error(
                        "invalid_api_key" if exc.is_permanent else "upstream_unavailable",
                        "Upstream credential refresh failed",
                        error_type="authentication_error" if exc.is_permanent else "server_error",
                    ),
                ) from exc
            except ProxyResponseError as exc:
                if new_session_lease is not None:
                    new_session_lease.release()
                classified = await self._handle_websocket_connect_error(account, exc)
                failure_class = classified["failure_class"] if isinstance(classified, dict) else "non_retryable"
                action = failover_decision(
                    failure_class=failure_class,
                    downstream_visible=False,
                    candidates_remaining=max(
                        0,
                        _HTTP_BRIDGE_RECONNECT_MAX_ACCOUNT_ATTEMPTS - len(excluded_account_ids) - 1,
                    ),
                )
                logger.info(
                    "Failover decision request_id=%s transport=http_bridge_reconnect account_id=%s "
                    "failure_class=%s action=%s",
                    request_state.request_log_id or request_state.request_id,
                    account.id,
                    failure_class,
                    action,
                )
                remaining = _remaining_budget_seconds(deadline)
                if selected_is_preferred and remaining > 0:
                    if retry_same_account_once and not self._is_retryable_http_bridge_connect_forbidden(exc):
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                if action == "failover_next" and remaining > 0:
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                if self._is_retryable_http_bridge_connect_forbidden(exc):
                    raise self._http_bridge_connect_rejected_error() from exc
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if new_session_lease is not None:
                    new_session_lease.release()
                message = (
                    "HTTP bridge upstream connection timed out"
                    if isinstance(exc, asyncio.TimeoutError)
                    else "HTTP bridge upstream connection failed"
                )
                await self._handle_websocket_connect_error(
                    account,
                    ProxyResponseError(
                        504 if isinstance(exc, asyncio.TimeoutError) else 502,
                        openai_error("upstream_unavailable", message),
                    ),
                )
                remaining = _remaining_budget_seconds(deadline)
                if remaining > 0:
                    if retry_same_account_once:
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                raise ProxyResponseError(502, openai_error("upstream_unavailable", message)) from exc
            except Exception:
                if new_session_lease is not None:
                    new_session_lease.release()
                raise
            else:
                if _remaining_budget_seconds(deadline) <= 0:
                    try:
                        await _close_http_bridge_upstream_before_deadline(
                            upstream,
                            deadline=deadline,
                            cleanup_tasks=self._proxy_cleanup_tasks,
                            label="expired HTTP bridge replacement websocket close",
                        )
                    finally:
                        if new_session_lease is not None:
                            new_session_lease.release()
                            new_session_lease = None
                    _raise_proxy_budget_exhausted()
                _record_same_account_takeover(
                    preferred_account_id=session.account.id,
                    selected_account_id=account.id,
                )
                break
        if _remaining_budget_seconds(deadline) <= 0:
            session.closed = True
            try:
                await _close_http_bridge_upstream_before_deadline(
                    upstream,
                    deadline=deadline,
                    cleanup_tasks=self._proxy_cleanup_tasks,
                    label="unpublished HTTP bridge replacement websocket close",
                )
            finally:
                if new_session_lease is not None:
                    new_session_lease.release()
                    new_session_lease = None
            _raise_proxy_budget_exhausted()
        if new_session_lease is not None:
            old_session_lease = session.account_model_session_lease
            session.account_model_session_lease = new_session_lease
            if old_session_lease is not None:
                old_session_lease.release()
        session.account = account
        session.headers = connect_headers
        session.upstream = upstream
        session.upstream_close_owned = False
        session.upstream_reconnect_count += 1
        session.upstream_control = _WebSocketUpstreamControl()
        session.closed = False
        session.upstream_turn_state = _upstream_turn_state_from_socket(upstream) or session.upstream_turn_state
        if session.defer_reader_start:
            session.upstream_reader = None
        elif restart_reader and not reconnecting_reader_owns_session:
            session.upstream_reader = asyncio.create_task(self._relay_http_bridge_upstream_messages(session))
        elif reconnecting_reader_owns_session:
            session.upstream_reader = current_task
        _log_http_bridge_event(
            "reconnect",
            session.key,
            account_id=account.id,
            model=session.request_model,
            detail=(
                f"request_stage=reattach, previous_account={old_account_id}, "
                f"preferred_account_id={preferred_candidate_id}, selected_account_id={account.id}, "
                f"prefer_same_account={prefer_same_account}, "
                f"durable_session_id={session.durable_session_id}"
            ),
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )

    async def _retry_http_bridge_terminal_failure(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str | None,
        payload: dict[str, JsonValue] | None,
        response_id: str | None,
        prefer_previous_response_not_found: bool,
        previous_response_id_hint: str | None,
        reset_response_state: bool,
        allow_precreated_terminal_fallback: bool,
    ) -> tuple[
        bool,
        _WebSocketRequestState | None,
        bool,
        ApiKeyUsageReservationData | None,
    ]:
        classified_failure = self._classify_and_schedule_stream_error(
            session.account,
            {"message": error_message or "Upstream error"},
            error_code,
            http_status=_http_error_status_from_payload(payload),
        )
        request_text = request_state.request_text
        if isinstance(request_text, str):
            retried = await self._retry_http_bridge_request_on_fresh_upstream(
                session,
                request_state=request_state,
                text_data=request_text,
                reset_response_state=reset_response_state,
                prefer_same_account=not _should_retry_http_bridge_on_different_account(classified_failure),
            )
            if retried:
                return True, None, False, None

        async with session.pending_lock:
            # Keep the terminal request and its reservation registered in the
            # shared pending queue across this coroutine's return boundary.
            # The upstream event handler atomically removes the matched state
            # and enrolls its finalizer.  Returning a reservation that was
            # already cleared here would leave no cleanup owner if the caller
            # were cancelled while resuming from this await.
            pending_snapshot = deque(session.pending_requests)
            terminal_request_state = _pop_terminal_websocket_request_state(
                pending_snapshot,
                response_id=response_id,
                fallback_request_state=request_state,
                prefer_previous_response_not_found=prefer_previous_response_not_found,
                previous_response_id_hint=previous_response_id_hint,
                error_message=error_message,
                allow_precreated_terminal_fallback=allow_precreated_terminal_fallback,
            )
            has_other_pending_requests = bool(pending_snapshot)
        return (
            False,
            terminal_request_state,
            has_other_pending_requests,
            None,
        )

    async def _retry_http_bridge_precreated_request(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        expected_request_tokens: frozenset[tuple[str, float]] | None = None,
    ) -> bool:
        runtime_settings = self._http_bridge_runtime_settings()
        request_state: _WebSocketRequestState | None = None
        lifecycle_lock_acquired = False
        try:
            async with session.pending_lock:
                if len(session.pending_requests) != 1:
                    return False
                request_state = session.pending_requests[0]
            request_budget_seconds = _http_bridge_request_budget_seconds(
                session,
                request_state,
                runtime_settings,
            )
            await _acquire_http_bridge_lock_before_deadline(
                session.lifecycle_lock,
                request_state,
                default_budget_seconds=request_budget_seconds,
                cleanup_tasks=self._proxy_cleanup_tasks,
            )
            lifecycle_lock_acquired = True
            async with session.pending_lock:
                if len(session.pending_requests) != 1 or session.pending_requests[0] is not request_state:
                    return False
                if (
                    expected_request_tokens is not None
                    and (
                        request_state.request_id,
                        request_state.http_bridge_send_completed_at,
                    )
                    not in expected_request_tokens
                ):
                    return False
                if (
                    request_state.response_id is not None
                    or not request_state.awaiting_response_created
                    or not request_state.request_text
                    or request_state.replay_count >= 1
                    # Once response.create starts, a close or startup timeout
                    # is ambiguous even if send_text returned. Replaying could
                    # duplicate an accepted upstream execution.
                    or request_state.http_bridge_send_started_at is not None
                ):
                    return False
                using_fresh_full_resend = request_state.previous_response_id is not None
                if using_fresh_full_resend:
                    if (
                        not request_state.proxy_injected_previous_response_id
                        or not request_state.fresh_upstream_request_is_retry_safe
                        or not request_state.fresh_upstream_request_text
                    ):
                        return False
                    request_text = request_state.fresh_upstream_request_text
                else:
                    request_text = request_state.request_text
                request_state.replay_count += 1
            try:
                _log_http_bridge_event(
                    "retry_precreated",
                    session.key,
                    account_id=session.account.id,
                    model=session.request_model,
                    pending_count=1,
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )
                await self._reconnect_http_bridge_session_locked(
                    session,
                    request_state=request_state,
                    prefer_same_account=False,
                    require_exclusive_request=True,
                )
                async with session.pending_lock:
                    if len(session.pending_requests) != 1 or session.pending_requests[0] is not request_state:
                        return False
                    if (
                        expected_request_tokens is not None
                        and (
                            request_state.request_id,
                            request_state.http_bridge_send_completed_at,
                        )
                        not in expected_request_tokens
                    ):
                        return False
                    if using_fresh_full_resend:
                        request_state.previous_response_id = None
                        request_state.proxy_injected_previous_response_id = False
                        request_state.request_text = request_text
                    request_text, forwarded_service_tier = _http_bridge_text_with_account_service_tier(
                        request_text,
                        session.account,
                        api_key=request_state.api_key,
                    )
                    if forwarded_service_tier is not None:
                        request_state.service_tier = forwarded_service_tier
                        request_state.requested_service_tier = forwarded_service_tier
                    request_state.request_text = request_text
                self._rebind_request_account_model_concurrency(session, request_state)
                _release_websocket_response_create_gate(request_state, session.response_create_gate)
                await _acquire_response_create_before_deadline(
                    self,
                    request_state,
                    response_create_gate=session.response_create_gate,
                    default_budget_seconds=request_budget_seconds,
                )
                _ensure_request_budget_remaining(request_state, request_budget_seconds)
                await _send_http_bridge_request_before_deadline(
                    session,
                    request_state,
                    request_text,
                    default_budget_seconds=request_budget_seconds,
                    cleanup_tasks=self._proxy_cleanup_tasks,
                )
                sent_at = time.monotonic()
                request_state.http_bridge_send_completed_at = sent_at
                session.last_used_at = sent_at
                session.pending_changed.set()
                return True
            finally:
                session.lifecycle_lock.release()
                lifecycle_lock_acquired = False
        except _HTTPBridgeSharedRetryBlocked:
            return False
        except asyncio.CancelledError:
            if request_state is not None:
                await self._fail_http_bridge_replay_interruption(
                    session,
                    request_state=request_state,
                )
            raise
        except Exception:
            logger.warning("HTTP bridge pre-created retry failed", exc_info=True)
            return False
        finally:
            if lifecycle_lock_acquired:
                session.lifecycle_lock.release()

    async def _cleanup_http_bridge_submit_interruption(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        gate_acquired: bool,
        request_enqueued: bool,
        queue_counted: bool,
        schedule_ambiguous_close: bool = True,
    ) -> None:
        if request_enqueued and request_state.http_bridge_send_started_at is not None:
            await self._detach_http_bridge_request(
                session,
                request_state=request_state,
                schedule_ambiguous_close=schedule_ambiguous_close,
            )
            return
        async with session.pending_lock:
            decrement_queue = False
            if request_enqueued and request_state in session.pending_requests:
                session.pending_requests.remove(request_state)
                decrement_queue = True
            elif queue_counted and not request_enqueued:
                decrement_queue = True
            if decrement_queue:
                session.queued_request_count = max(0, session.queued_request_count - 1)
            session.pending_changed.set()
        if gate_acquired:
            _release_websocket_response_create_gate(request_state, session.response_create_gate)
        self._release_request_account_model_concurrency(request_state)

    async def _fail_http_bridge_replay_interruption(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
    ) -> None:
        await _await_shielded_cleanup(
            self._detach_http_bridge_request(
                session,
                request_state=request_state,
            ),
            label="HTTP bridge replay interruption detach",
        )

    async def _detach_http_bridge_request(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        schedule_ambiguous_close: bool = True,
    ) -> bool:
        request_state.event_queue = None
        submitted_upstream = request_state.http_bridge_send_started_at is not None

        def schedule_local_settlement(
            reservation: ApiKeyUsageReservationData | None,
            resolution_future: asyncio.Future[_DiscardedAccountingResolution] | None = None,
        ) -> None:
            if resolution_future is not None:
                if not resolution_future.done():
                    resolution_future.set_result("fallback")
                return
            if reservation is not None:
                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    self._settle_or_release_failed_websocket_reservation(
                        request_state=request_state,
                        reservation=reservation,
                        api_key=request_state.api_key or session.api_key,
                        error_code="stream_incomplete",
                        error_message="HTTP bridge request detached before response.completed",
                    ),
                    name=f"http-bridge-detach-settlement-{request_state.request_id}",
                    label=f"HTTP bridge detach settlement request_id={request_state.request_id}",
                )

        async def claim_detach_accounting() -> None:
            async with session.pending_lock:
                request_registered = request_state in session.pending_requests
                reservation = request_state.api_key_reservation
                resolution_future = request_state.discarded_accounting_resolution_future
                if not request_registered and reservation is None:
                    return
                request_state.api_key_reservation = None
                request_state.discarded_accounting_resolution_future = None
                if not submitted_upstream:
                    schedule_local_settlement(reservation, resolution_future)
                    return

                discarded_accounting = _DiscardedRequestAccounting(
                    request_state=request_state,
                    api_key_reservation=reservation,
                    resolution_future=resolution_future,
                )
                if request_state.response_id is not None:
                    existing = session.discarded_request_accounting.get(
                        request_state.response_id
                    )
                    if existing is not None:
                        if existing.request_state is not request_state:
                            schedule_local_settlement(reservation, resolution_future)
                        return
                    if session.discarded_accounting_reconciliation_owned:
                        schedule_local_settlement(reservation, resolution_future)
                        return
                    session.discarded_response_ids.add(request_state.response_id)
                    session.discarded_request_accounting[
                        request_state.response_id
                    ] = discarded_accounting
                    return

                existing = session.anonymous_discarded_request_accounting.get(
                    request_state.request_id
                )
                if existing is not None:
                    if existing.request_state is not request_state:
                        schedule_local_settlement(reservation, resolution_future)
                    return
                if session.discarded_accounting_reconciliation_owned:
                    schedule_local_settlement(reservation, resolution_future)
                    return
                session.anonymous_discarded_request_accounting[
                    request_state.request_id
                ] = discarded_accounting

        async def detach_request() -> bool:
            removed = False
            retire_ambiguous_transport = False
            # Cancellation while acquiring this lock leaves the reservation on
            # the shared request state. Once the claim completes, its successor
            # map entry or tracked local settlement is enrolled before return.
            await claim_detach_accounting()
            async with session.lifecycle_lock:
                async with session.pending_lock:
                    if request_state in session.pending_requests:
                        if submitted_upstream and request_state.response_id is None:
                            session.closed = True
                            retire_ambiguous_transport = True
                        session.pending_requests.remove(request_state)
                        session.queued_request_count = max(0, session.queued_request_count - 1)
                        session.pending_changed.set()
                        removed = True
                if retire_ambiguous_transport and schedule_ambiguous_close:
                    async with self._http_bridge_lock:
                        self._detach_http_bridge_session_indexes_locked(session)
                        self._schedule_http_bridge_session_close(
                            session,
                            reason="ambiguous-downstream-detach",
                        )

            if not removed:
                return False
            _release_websocket_response_create_gate(request_state, session.response_create_gate)
            self._release_request_account_model_concurrency(request_state)
            return True

        try:
            return await _await_operation_before_hard_timeout(
                detach_request(),
                timeout_seconds=_HTTP_BRIDGE_DETACH_OBSERVATION_TIMEOUT_SECONDS,
                tasks=self._proxy_cleanup_tasks,
                label=f"HTTP bridge request detach request_id={request_state.request_id}",
            )
        except TimeoutError:
            logger.warning(
                "HTTP bridge request detach exceeded hard observation timeout request_id=%s",
                request_state.request_id,
            )
            return False
