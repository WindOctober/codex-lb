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
    _raise_proxy_budget_exhausted,
    _remaining_budget_seconds,
    _set_request_budget,
    _websocket_connect_deadline,
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
    _DownstreamWebSocketActivity,
    _event_type_from_payload,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _is_recoverable_account_selection_wait,
    _release_websocket_response_create_gate,
    _routing_strategy,
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


class _HTTPBridgeRequestSubmitService(Protocol):
    _http_bridge_lock: anyio.Lock
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]

    @staticmethod
    def _http_bridge_codex_prewarm_enabled() -> bool: ...

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    @staticmethod
    async def _http_bridge_dashboard_settings() -> DashboardSettings: ...

    @staticmethod
    async def _cancel_http_bridge_upstream_reader(task: asyncio.Task[None]) -> bool: ...

    async def _release_http_bridge_submit_lease(self, session: _HTTPBridgeSession) -> None: ...

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
    ) -> None: ...

    async def _restore_http_bridge_session_after_reconnect(
        self,
        session: _HTTPBridgeSession,
        *,
        request_id: str,
    ) -> str | None: ...

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

    async def _mark_http_bridge_account_permanent_failure(
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

    @staticmethod
    def _account_model_concurrency_overload(model: str | None) -> ProxyResponseError: ...

    @staticmethod
    def _is_retryable_http_bridge_connect_forbidden(exc: ProxyResponseError) -> bool: ...

    @staticmethod
    def _http_bridge_connect_rejected_error() -> ProxyResponseError: ...

    async def _relay_http_bridge_upstream_messages(self, session: _HTTPBridgeSession) -> None: ...

    async def _evict_http_bridge_pressure(
        self,
        *,
        max_sessions: int,
        protected_key: _HTTPBridgeSessionKey,
        request_model: str | None,
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

        async def release_submit_lease_once() -> None:
            nonlocal submit_lease_released
            if submit_lease_released:
                return
            submit_lease_released = True
            if release_submit_lease:
                await self._release_http_bridge_submit_lease(session)

        request_state.http_bridge_submit_started_at = time.monotonic()
        try:
            text_data, forwarded_service_tier = _http_bridge_text_with_account_service_tier(
                text_data,
                session.account,
                api_key=request_state.api_key,
            )
            if forwarded_service_tier is not None:
                request_state.service_tier = forwarded_service_tier
                request_state.requested_service_tier = forwarded_service_tier
            if session.closed:
                async with session.lifecycle_lock:
                    if session.closed:
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
                            )
                            if restore_failure is not None:
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
                        else:
                            _log_http_bridge_event(
                                "submit_on_closed",
                                session.key,
                                account_id=session.account.id,
                                model=session.request_model,
                                cache_key_family=session.key.affinity_kind,
                                model_class=_extract_model_class(session.request_model)
                                if session.request_model
                                else None,
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
                api_key=session.api_key,
            )
            await self._maybe_prewarm_http_bridge_session(
                session,
                request_state=request_state,
                text_data=text_data,
            )
            request_state.account_model_concurrency = self._try_acquire_account_model_concurrency(
                account=session.account,
                model=session.request_model,
                request_id=request_state.request_id,
                transport=_REQUEST_TRANSPORT_HTTP,
            )
            if request_state.account_model_concurrency is None:
                raise self._account_model_concurrency_overload(session.request_model)
            gate_acquired = False
            request_enqueued = False
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
            await release_submit_lease_once()
            try:
                await self._acquire_request_state_response_create_admission(
                    request_state,
                    response_create_gate=session.response_create_gate,
                )
                gate_acquired = True
                async with session.lifecycle_lock:
                    async with self._http_bridge_lock:
                        current_session = self._http_bridge_sessions.get(session.key)
                    session_replaced = current_session is not None and current_session is not session
                    session_unregistered = current_session is None
                    detached_soft_submit = (
                        not session.closed
                        and session.key.strength == "soft"
                        and (session_replaced or session_unregistered)
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
                    async with session.pending_lock:
                        session.pending_requests.append(request_state)
                    request_enqueued = True
                    await session.upstream.send_text(text_data)
                    request_state.http_bridge_send_completed_at = time.monotonic()
                    session.last_used_at = time.monotonic()
            except ProxyResponseError:
                await self._cleanup_http_bridge_submit_interruption(
                    session,
                    request_state=request_state,
                    gate_acquired=gate_acquired,
                    request_enqueued=request_enqueued,
                )
                raise
            except asyncio.CancelledError:
                await self._cleanup_http_bridge_submit_interruption(
                    session,
                    request_state=request_state,
                    gate_acquired=gate_acquired,
                    request_enqueued=request_enqueued,
                )
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
                retried = await self._retry_http_bridge_request_on_fresh_upstream(
                    session,
                    request_state=request_state,
                    text_data=text_data,
                )
                if retried:
                    return
                await self._cleanup_http_bridge_submit_interruption(
                    session,
                    request_state=request_state,
                    gate_acquired=gate_acquired,
                    request_enqueued=request_enqueued,
                )
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
                try:
                    await session.upstream.close()
                except Exception:
                    logger.debug("Failed to close HTTP bridge upstream websocket after send failure", exc_info=True)
                raise ProxyResponseError(
                    502,
                    openai_error("upstream_unavailable", str(exc) or "Upstream websocket closed"),
                ) from exc
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
        async with prewarm_lock:
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
                started_at=time.monotonic(),
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
            try:
                event_queue = warmup_state.event_queue
                assert event_queue is not None
                await self._acquire_request_state_response_create_admission(
                    warmup_state,
                    response_create_gate=session.response_create_gate,
                )
                gate_acquired = True
                async with session.pending_lock:
                    session.pending_requests.append(warmup_state)
                request_enqueued = True
                await session.upstream.send_text(warmup_text)
                warmup_state.http_bridge_send_completed_at = time.monotonic()
                while True:
                    event_block = await event_queue.get()
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
                )
                if is_local_overload_error_code(code):
                    session.prewarmed = False
                    return
                session.prewarmed = False
                raise
            except BaseException:
                session.prewarmed = False
                await self._cleanup_http_bridge_submit_interruption(
                    session,
                    request_state=warmup_state,
                    gate_acquired=gate_acquired,
                    request_enqueued=request_enqueued,
                )
                raise

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
        retry_text_data = text_data
        if request_state.previous_response_id is not None and send_request:
            if (
                not request_state.proxy_injected_previous_response_id
                or not request_state.fresh_upstream_request_text
                or not request_state.fresh_upstream_request_is_retry_safe
            ):
                return False
            retry_text_data = request_state.fresh_upstream_request_text
        if request_state.replay_count >= 1:
            return False
        request_state.replay_count += 1
        original_response_id = request_state.response_id
        original_awaiting_response_created = request_state.awaiting_response_created
        if reset_response_state:
            request_state.response_id = None
            request_state.awaiting_response_created = True
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
            await self._reconnect_http_bridge_session(
                session,
                request_state=request_state,
                restart_reader=True,
                prefer_same_account=prefer_same_account,
            )
            if send_request:
                if retry_text_data != text_data:
                    request_state.previous_response_id = None
                    request_state.proxy_injected_previous_response_id = False
                    request_state.request_text = retry_text_data
                await session.upstream.send_text(retry_text_data)
            sent_at = time.monotonic()
            if send_request:
                request_state.http_bridge_send_completed_at = sent_at
            session.last_used_at = sent_at
            return True
        except Exception:
            if reset_response_state:
                request_state.response_id = original_response_id
                request_state.awaiting_response_created = original_awaiting_response_created
            logger.warning("HTTP bridge retry on fresh upstream failed", exc_info=True)
            return False

    async def _reconnect_http_bridge_session(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        restart_reader: bool = False,
        prefer_same_account: bool = True,
    ) -> None:
        old_account_id = session.account.id
        old_upstream = session.upstream
        old_reader = session.upstream_reader if restart_reader else None
        if old_reader is not None and old_reader is not asyncio.current_task():
            cancelled = await self._cancel_http_bridge_upstream_reader(old_reader)
            if not cancelled:
                session.closed = True
                raise ProxyResponseError(
                    502,
                    openai_error(
                        "upstream_unavailable",
                        "HTTP responses session bridge reader did not shut down cleanly",
                    ),
                )
        try:
            await old_upstream.close()
        except Exception:
            logger.debug("Failed to close HTTP bridge upstream websocket before reconnect", exc_info=True)

        runtime_settings = self._http_bridge_runtime_settings()
        _set_request_budget(
            request_state,
            runtime_settings.proxy_reconnect_request_budget_seconds,
            restart_from_now=True,
        )
        deadline = _websocket_connect_deadline(
            request_state,
            runtime_settings.proxy_reconnect_request_budget_seconds,
        )
        settings = await self._http_bridge_dashboard_settings()
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
                _record_same_account_takeover(
                    preferred_account_id=session.account.id,
                    selected_account_id=account.id,
                )
                break
            except RefreshError as exc:
                if new_session_lease is not None:
                    new_session_lease.release()
                if exc.is_permanent:
                    await self._mark_http_bridge_account_permanent_failure(account, exc.code)
                if selected_is_preferred and _remaining_budget_seconds(deadline) > 0:
                    if retry_same_account_once and not exc.is_permanent:
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                raise
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
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if new_session_lease is not None:
                    new_session_lease.release()
                if selected_is_preferred and _remaining_budget_seconds(deadline) > 0:
                    if retry_same_account_once:
                        retry_same_account_once = False
                        continue
                    excluded_account_ids.add(account.id)
                    preferred_candidate_id = None
                    continue
                raise
        if new_session_lease is not None:
            old_session_lease = session.account_model_session_lease
            session.account_model_session_lease = new_session_lease
            if old_session_lease is not None:
                old_session_lease.release()
        session.account = account
        session.headers = connect_headers
        session.upstream = upstream
        session.upstream_reconnect_count += 1
        session.upstream_control = _WebSocketUpstreamControl()
        session.closed = False
        session.upstream_turn_state = _upstream_turn_state_from_socket(upstream) or session.upstream_turn_state
        if restart_reader:
            session.upstream_reader = asyncio.create_task(self._relay_http_bridge_upstream_messages(session))
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
    ) -> tuple[bool, _WebSocketRequestState | None, bool]:
        classified_failure = await self._handle_stream_error(
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
                return True, None, False

        async with session.pending_lock:
            terminal_request_state = _pop_terminal_websocket_request_state(
                session.pending_requests,
                response_id=response_id,
                fallback_request_state=request_state,
                prefer_previous_response_not_found=prefer_previous_response_not_found,
                previous_response_id_hint=previous_response_id_hint,
                error_message=error_message,
                allow_precreated_terminal_fallback=allow_precreated_terminal_fallback,
            )
            if terminal_request_state is not None:
                session.queued_request_count = max(0, session.queued_request_count - 1)
            has_other_pending_requests = bool(session.pending_requests)
        return False, terminal_request_state, has_other_pending_requests

    async def _retry_http_bridge_precreated_request(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        expected_request_tokens: frozenset[tuple[str, float]] | None = None,
    ) -> bool:
        async with session.pending_lock:
            if len(session.pending_requests) != 1:
                return False
            request_state = session.pending_requests[0]
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
            ):
                return False
            if request_state.replay_count >= 1:
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
        _log_http_bridge_event(
            "retry_precreated",
            session.key,
            account_id=session.account.id,
            model=session.request_model,
            pending_count=1,
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )
        try:
            await self._reconnect_http_bridge_session(
                session,
                request_state=request_state,
                prefer_same_account=False,
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
                await session.upstream.send_text(request_text)
                sent_at = time.monotonic()
                request_state.http_bridge_send_completed_at = sent_at
                session.last_used_at = sent_at
            return True
        except Exception:
            logger.warning("HTTP bridge pre-created retry failed", exc_info=True)
            return False

    async def _cleanup_http_bridge_submit_interruption(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        gate_acquired: bool,
        request_enqueued: bool,
    ) -> None:
        async with session.pending_lock:
            if request_enqueued and request_state in session.pending_requests:
                session.pending_requests.remove(request_state)
            session.queued_request_count = max(0, session.queued_request_count - 1)
        if gate_acquired:
            _release_websocket_response_create_gate(request_state, session.response_create_gate)
        self._release_request_account_model_concurrency(request_state)

    async def _detach_http_bridge_request(
        self: _HTTPBridgeRequestSubmitService,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
    ) -> bool:
        removed = False
        async with session.pending_lock:
            if request_state in session.pending_requests:
                session.pending_requests.remove(request_state)
                session.queued_request_count = max(0, session.queued_request_count - 1)
                removed = True
        request_state.event_queue = None
        if not removed:
            return False
        _release_websocket_response_create_gate(request_state, session.response_create_gate)
        self._release_request_account_model_concurrency(request_state)
        await self._release_websocket_reservation(request_state.api_key_reservation)
        request_state.api_key_reservation = None
        return True
