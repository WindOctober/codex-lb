from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Protocol

import anyio

from app.core.clients.proxy_websocket import UpstreamWebSocketMessage, sanitize_upstream_websocket_event_text
from app.core.config.settings import Settings
from app.core.openai.models import OpenAIEvent
from app.core.openai.parsing import parse_sse_event
from app.core.types import JsonValue
from app.core.utils.sse import parse_sse_data_json
from app.db.models import Account
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import _extract_model_class
from app.modules.proxy._service.budget import _remaining_budget_seconds, _request_deadline_at
from app.modules.proxy._service.observability import (
    _log_http_bridge_event,
    _log_http_bridge_latency_breakdown,
    _record_http_bridge_upstream_event,
)
from app.modules.proxy._service.service_tier import _service_tier_from_event_payload
from app.modules.proxy._service.support import (
    _anonymous_terminal_candidate_count,
    _await_cancelled_task,
    _await_operation_before_hard_timeout,
    _await_shielded_cleanup,
    _DiscardedRequestAccounting,
    _event_type_from_payload,
    _HTTPBridgeExpiryResult,
    _HTTPBridgeSession,
    _release_websocket_response_create_gate,
    _schedule_tracked_background_task,
    _WebSocketReceiveTimeout,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy._service.websocket.events import (
    _assign_websocket_response_id,
    _build_stream_incomplete_terminal_event_for_request,
    _find_websocket_request_state_by_response_id,
    _has_other_pending_requests,
    _http_bridge_no_text_failover_error_code,
    _http_bridge_precreated_failover_error_code,
    _http_error_status_from_payload,
    _is_previous_response_not_found_error,
    _match_websocket_request_state_for_anonymous_event,
    _match_websocket_request_state_for_precreated_terminal_event,
    _matching_websocket_request_states_for_previous_response_error,
    _maybe_rewrite_websocket_previous_response_not_found_event,
    _normalize_http_bridge_error_event,
    _pop_matching_websocket_request_states,
    _pop_terminal_websocket_request_state,
    _previous_response_id_from_not_found_message,
    _upstream_websocket_disconnect_message,
    _websocket_event_error_code,
    _websocket_event_error_message,
    _websocket_event_error_param,
    _websocket_event_error_type,
    _websocket_response_id,
)
from app.modules.proxy.helpers import _normalize_error_code

logger = logging.getLogger("app.modules.proxy.service")

_HTTP_BRIDGE_RECONNECT_CLOSE_OBSERVATION_SECONDS = 1.0


class _HTTPBridgeUpstreamEventsService(Protocol):
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    async def _next_websocket_receive_timeout(
        self,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        proxy_request_budget_seconds: float,
        stream_idle_timeout_seconds: float,
        response_created_timeout_seconds: float | None = None,
    ) -> _WebSocketReceiveTimeout | None: ...

    async def _retry_http_bridge_precreated_request(
        self,
        session: _HTTPBridgeSession,
        *,
        expected_request_tokens: frozenset[tuple[str, float]] | None = None,
    ) -> bool: ...

    async def _retry_http_bridge_terminal_failure(
        self,
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
    ]: ...

    async def _fail_pending_websocket_requests(
        self,
        *,
        account_id_value: str | None,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        error_code: str,
        error_message: str,
        api_key: ApiKeyData | None,
        response_create_gate: asyncio.Semaphore | None = None,
    ) -> None: ...

    async def _fail_http_bridge_expired_requests(
        self,
        session: _HTTPBridgeSession,
        *,
        request_budget_seconds: float,
        error_code: str,
        error_message: str,
    ) -> _HTTPBridgeExpiryResult: ...

    async def _evict_http_bridge_session_after_upstream_disconnect(
        self,
        session: _HTTPBridgeSession,
        *,
        error_message: str,
    ) -> None: ...

    async def _fail_response_created_timeout_requests(
        self,
        session: _HTTPBridgeSession,
        *,
        request_ids: frozenset[str],
        timeout_seconds: float,
        error_code: str,
        error_message: str,
    ) -> tuple[_WebSocketRequestState, ...]: ...

    async def _register_http_bridge_previous_response_id(
        self,
        session: _HTTPBridgeSession,
        response_id: str,
        *,
        input_item_count: int | None = None,
        input_full_fingerprint: str | None = None,
    ) -> None: ...

    async def _http_bridge_pending_count(self, session: _HTTPBridgeSession) -> int: ...

    async def _finalize_websocket_request_state(
        self,
        request_state: _WebSocketRequestState,
        *,
        account: Account,
        account_id_value: str,
        event: OpenAIEvent | None,
        event_type: str | None,
        payload: dict[str, JsonValue] | None,
        accounting_event: OpenAIEvent | None = None,
        accounting_payload: dict[str, JsonValue] | None = None,
        stream_error_already_classified: bool = False,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        upstream_control: _WebSocketUpstreamControl,
        response_create_gate: asyncio.Semaphore | None,
    ) -> None: ...

    def _schedule_websocket_request_finalization(
        self,
        request_state: _WebSocketRequestState,
        *,
        account: Account,
        account_id_value: str,
        event: OpenAIEvent | None,
        event_type: str | None,
        payload: dict[str, JsonValue] | None,
        accounting_event: OpenAIEvent | None = None,
        accounting_event_type: str | None = None,
        accounting_payload: dict[str, JsonValue] | None = None,
        stream_error_already_classified: bool = False,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None = None,
        reservation_preclaimed: bool = False,
        upstream_control: _WebSocketUpstreamControl,
        response_create_gate: asyncio.Semaphore | None,
    ) -> None: ...

    def _capture_websocket_retry_usage(
        self,
        request_state: _WebSocketRequestState,
        *,
        account_id_value: str,
        event: OpenAIEvent | None,
        event_type: str | None,
        payload: dict[str, JsonValue] | None,
        api_key: ApiKeyData | None,
        service_tier: str | None,
        requested_service_tier: str | None,
        append_usage_charge: bool = True,
        schedule_request_log: bool = True,
    ) -> None: ...

    async def _process_http_bridge_upstream_text(
        self,
        session: _HTTPBridgeSession,
        text: str,
    ) -> None: ...


class _HTTPBridgeUpstreamEventsMixin:
    async def _relay_http_bridge_upstream_messages(
        self: _HTTPBridgeUpstreamEventsService,
        session: _HTTPBridgeSession,
    ) -> None:
        runtime_settings = self._http_bridge_runtime_settings()
        receive_task: asyncio.Task[UpstreamWebSocketMessage] | None = None
        pending_changed_task: asyncio.Task[bool] | None = None

        async def reconcile_completed_receive(
            task: asyncio.Task[UpstreamWebSocketMessage],
        ) -> bool:
            try:
                completed_message = task.result()
            except (asyncio.CancelledError, Exception):
                return False
            if completed_message.kind != "text" or completed_message.text is None:
                return False
            await self._process_http_bridge_upstream_text(
                session,
                getattr(completed_message, "raw_text", None) or completed_message.text,
            )
            return True

        async def cancel_receive_task() -> tuple[bool, bool]:
            nonlocal receive_task
            if receive_task is None:
                return session.detached_upstream_receive is None, False
            task = receive_task
            if task.done():
                receive_task = None
                return True, await reconcile_completed_receive(task)
            stopped = await _await_cancelled_task(task, label="HTTP bridge upstream receive")
            receive_task = None
            if not stopped:
                session.detached_upstream_receive = task
                return False, False
            return True, await reconcile_completed_receive(task)

        async def preserve_submitted_pending_accounting() -> None:
            with anyio.CancelScope(shield=True):
                async with session.pending_lock:
                    for request_state in session.pending_requests:
                        if request_state.http_bridge_send_started_at is None:
                            continue
                        reservation = request_state.api_key_reservation
                        request_state.api_key_reservation = None
                        discarded_accounting = _DiscardedRequestAccounting(
                            request_state=request_state,
                            api_key_reservation=reservation,
                        )
                        if request_state.response_id is not None:
                            session.discarded_response_ids.add(request_state.response_id)
                            session.discarded_request_accounting.setdefault(
                                request_state.response_id,
                                discarded_accounting,
                            )
                        else:
                            session.anonymous_discarded_request_accounting.setdefault(
                                request_state.request_id,
                                discarded_accounting,
                            )

        try:
            while True:
                session.pending_changed.clear()
                receive_timeout = await self._next_websocket_receive_timeout(
                    session.pending_requests,
                    pending_lock=session.pending_lock,
                    proxy_request_budget_seconds=(
                        runtime_settings.http_responses_session_bridge_request_budget_seconds
                    ),
                    stream_idle_timeout_seconds=runtime_settings.stream_idle_timeout_seconds,
                    response_created_timeout_seconds=(
                        runtime_settings.http_responses_session_bridge_response_created_timeout_seconds
                    ),
                )
                timed_out = receive_timeout is not None and receive_timeout.timeout_seconds <= 0
                message: UpstreamWebSocketMessage | None = None
                if not timed_out:
                    if receive_task is None:
                        receive_task = asyncio.create_task(session.upstream.receive())
                    pending_changed_task = asyncio.create_task(session.pending_changed.wait())
                    done, _pending = await asyncio.wait(
                        {receive_task, pending_changed_task},
                        timeout=None if receive_timeout is None else receive_timeout.timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receive_task in done:
                        message = receive_task.result()
                        receive_task = None
                    elif pending_changed_task in done:
                        pending_changed_task = None
                        continue
                    else:
                        timed_out = True
                    if not pending_changed_task.done():
                        await _await_cancelled_task(
                            pending_changed_task,
                            label="HTTP bridge pending-change wait",
                        )
                    pending_changed_task = None

                if timed_out:
                    assert receive_timeout is not None
                    terminal_error_code = receive_timeout.error_code
                    terminal_error_message = receive_timeout.error_message
                    if receive_timeout.response_created_request_ids:
                        receive_stopped, receive_reconciled = await cancel_receive_task()
                        if receive_reconciled:
                            continue
                        if not receive_stopped:
                            await preserve_submitted_pending_accounting()
                        retried = receive_stopped and await self._retry_http_bridge_precreated_request(
                            session, expected_request_tokens=receive_timeout.response_created_request_tokens
                        )
                        if retried:
                            continue
                        async with session.lifecycle_lock:
                            expired_requests = await self._fail_response_created_timeout_requests(
                                session,
                                request_ids=receive_timeout.response_created_request_ids,
                                timeout_seconds=(
                                    runtime_settings.http_responses_session_bridge_response_created_timeout_seconds
                                ),
                                error_code=receive_timeout.error_code,
                                error_message=receive_timeout.error_message,
                            )
                            if not expired_requests:
                                continue
                            session.closed = True
                            async with session.pending_lock:
                                if session.pending_requests:
                                    terminal_error_code = "stream_incomplete"
                                    terminal_error_message = (
                                        "Upstream bridge retired after another request timed out "
                                        "before response.created"
                                    )
                                session.queued_request_count = 0
                            await self._fail_pending_websocket_requests(
                                account_id_value=session.account.id,
                                pending_requests=session.pending_requests,
                                pending_lock=session.pending_lock,
                                error_code=terminal_error_code,
                                error_message=terminal_error_message,
                                api_key=None,
                                response_create_gate=session.response_create_gate,
                            )
                            await self._evict_http_bridge_session_after_upstream_disconnect(
                                session,
                                error_message=terminal_error_message,
                            )
                        break

                    if not receive_timeout.fail_all_pending:
                        expiry_result = await self._fail_http_bridge_expired_requests(
                            session,
                            request_budget_seconds=(
                                runtime_settings.http_responses_session_bridge_request_budget_seconds
                            ),
                            error_code=terminal_error_code,
                            error_message=terminal_error_message,
                        )
                        if expiry_result.retire_ambiguous_transport:
                            receive_stopped, _receive_reconciled = await cancel_receive_task()
                            if not receive_stopped:
                                await preserve_submitted_pending_accounting()
                            break
                        continue

                    receive_stopped, receive_reconciled = await cancel_receive_task()
                    if receive_reconciled:
                        continue
                    if not receive_stopped:
                        await preserve_submitted_pending_accounting()
                    retried = receive_stopped and await self._retry_http_bridge_precreated_request(session)
                    if retried:
                        continue
                    async with session.lifecycle_lock:
                        session.closed = True
                        async with session.pending_lock:
                            session.queued_request_count = 0
                        await self._fail_pending_websocket_requests(
                            account_id_value=session.account.id,
                            pending_requests=session.pending_requests,
                            pending_lock=session.pending_lock,
                            error_code=terminal_error_code,
                            error_message=terminal_error_message,
                            api_key=None,
                            response_create_gate=session.response_create_gate,
                        )
                        await self._evict_http_bridge_session_after_upstream_disconnect(
                            session,
                            error_message=terminal_error_message,
                        )
                    break

                assert message is not None
                if message.kind == "text" and message.text is not None:
                    await self._process_http_bridge_upstream_text(
                        session,
                        getattr(message, "raw_text", None) or message.text,
                    )
                    if session.upstream_control.retire_ambiguous_transport:
                        async with session.lifecycle_lock:
                            session.closed = True
                            async with session.pending_lock:
                                session.queued_request_count = 0
                            await self._fail_pending_websocket_requests(
                                account_id_value=session.account.id,
                                pending_requests=session.pending_requests,
                                pending_lock=session.pending_lock,
                                error_code="stream_incomplete",
                                error_message=(
                                    "Upstream bridge retired after an anonymous late event could not be correlated"
                                ),
                                api_key=None,
                                response_create_gate=session.response_create_gate,
                            )
                            await self._evict_http_bridge_session_after_upstream_disconnect(
                                session,
                                error_message=(
                                    "Upstream bridge retired after an anonymous late event could not be correlated"
                                ),
                            )
                        break
                    if session.upstream_control.reconnect_requested:
                        should_close = session.upstream_control.replay_request_state is not None
                        if not should_close:
                            async with session.pending_lock:
                                should_close = not session.pending_requests
                        if should_close:
                            async with session.lifecycle_lock:
                                session.closed = True
                                replay_request_state = session.upstream_control.replay_request_state
                                close_remaining = _HTTP_BRIDGE_RECONNECT_CLOSE_OBSERVATION_SECONDS
                                if replay_request_state is not None:
                                    close_remaining = min(
                                        close_remaining,
                                        _remaining_budget_seconds(
                                            _request_deadline_at(
                                                replay_request_state,
                                                runtime_settings.http_responses_session_bridge_request_budget_seconds,
                                            )
                                        ),
                                    )
                                session.upstream_close_owned = True
                                replay_request_id = (
                                    replay_request_state.request_id if replay_request_state is not None else "none"
                                )
                                try:
                                    await _await_operation_before_hard_timeout(
                                        session.upstream.close(),
                                        timeout_seconds=max(0.000001, close_remaining),
                                        tasks=self._proxy_cleanup_tasks,
                                        label=f"HTTP bridge reconnect close request_id={replay_request_id}",
                                    )
                                except TimeoutError:
                                    logger.warning("HTTP bridge reconnect close exceeded hard observation")
                                except Exception:
                                    logger.debug(
                                        "Failed to close HTTP bridge upstream websocket for reconnect",
                                        exc_info=True,
                                    )
                            break
                    continue

                retried = await self._retry_http_bridge_precreated_request(session)
                if retried:
                    continue
                disconnect_message = _upstream_websocket_disconnect_message(message)
                async with session.lifecycle_lock:
                    session.closed = True
                    async with session.pending_lock:
                        session.queued_request_count = 0
                    await self._fail_pending_websocket_requests(
                        account_id_value=session.account.id,
                        pending_requests=session.pending_requests,
                        pending_lock=session.pending_lock,
                        error_code="stream_incomplete",
                        error_message=disconnect_message,
                        api_key=None,
                        response_create_gate=session.response_create_gate,
                    )
                    await self._evict_http_bridge_session_after_upstream_disconnect(
                        session,
                        error_message=disconnect_message,
                    )
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "HTTP bridge upstream reader crashed account_id=%s bridge_kind=%s",
                session.account.id,
                session.key.affinity_kind,
                exc_info=True,
            )
            async with session.lifecycle_lock:
                session.closed = True
                async with session.pending_lock:
                    session.queued_request_count = 0
                await self._fail_pending_websocket_requests(
                    account_id_value=session.account.id,
                    pending_requests=session.pending_requests,
                    pending_lock=session.pending_lock,
                    error_code="stream_incomplete",
                    error_message="HTTP bridge upstream reader crashed before response.completed",
                    api_key=None,
                    response_create_gate=session.response_create_gate,
                )
                await self._evict_http_bridge_session_after_upstream_disconnect(
                    session,
                    error_message="HTTP bridge upstream reader crashed before response.completed",
                )
        finally:
            if pending_changed_task is not None and not pending_changed_task.done():
                await _await_cancelled_task(
                    pending_changed_task,
                    label="HTTP bridge pending-change wait",
                )
            await cancel_receive_task()
            session.closed = True

    async def _fail_http_bridge_expired_requests(
        self: _HTTPBridgeUpstreamEventsService,
        session: _HTTPBridgeSession,
        *,
        request_budget_seconds: float,
        error_code: str,
        error_message: str,
    ) -> _HTTPBridgeExpiryResult:
        now = time.monotonic()
        with anyio.CancelScope(shield=True):
            async with session.lifecycle_lock:
                async with session.pending_lock:
                    expired_requests = tuple(
                        request_state
                        for request_state in session.pending_requests
                        if now >= _request_deadline_at(request_state, request_budget_seconds)
                    )
                    retire_ambiguous_transport = any(
                        request_state.response_id is None for request_state in expired_requests
                    )
                    if retire_ambiguous_transport:
                        session.closed = True
                    for request_state in expired_requests:
                        session.pending_requests.remove(request_state)
                        if request_state.response_id is not None:
                            session.discarded_response_ids.add(request_state.response_id)
                            session.discarded_request_accounting[request_state.response_id] = (
                                _DiscardedRequestAccounting(
                                    request_state=request_state,
                                    api_key_reservation=request_state.api_key_reservation,
                                )
                            )
                            request_state.api_key_reservation = None
                        elif request_state.http_bridge_send_started_at is not None:
                            session.anonymous_discarded_request_accounting[
                                request_state.request_id
                            ] = _DiscardedRequestAccounting(
                                request_state=request_state,
                                api_key_reservation=request_state.api_key_reservation,
                            )
                            request_state.api_key_reservation = None
                    session.queued_request_count = max(
                        0,
                        session.queued_request_count - len(expired_requests),
                    )
                    session.pending_changed.set()

                async def finalize_expired_requests() -> None:
                    if expired_requests:
                        await self._fail_pending_websocket_requests(
                            account_id_value=session.account.id,
                            pending_requests=deque(expired_requests),
                            pending_lock=anyio.Lock(),
                            error_code=error_code,
                            error_message=error_message,
                            api_key=None,
                            response_create_gate=session.response_create_gate,
                        )
                    if retire_ambiguous_transport:
                        async with session.pending_lock:
                            session.queued_request_count = 0
                        await self._fail_pending_websocket_requests(
                            account_id_value=session.account.id,
                            pending_requests=session.pending_requests,
                            pending_lock=session.pending_lock,
                            error_code="stream_incomplete",
                            error_message=(
                                "Upstream bridge retired after an unidentified request exceeded its deadline"
                            ),
                            api_key=None,
                            response_create_gate=session.response_create_gate,
                        )
                        await self._evict_http_bridge_session_after_upstream_disconnect(
                            session,
                            error_message=(
                                "Upstream bridge retired after an unidentified request exceeded its deadline"
                            ),
                        )

                await _await_shielded_cleanup(
                    finalize_expired_requests(),
                    label="HTTP bridge expired-request cleanup",
                )
        return _HTTPBridgeExpiryResult(
            expired_requests=expired_requests,
            retire_ambiguous_transport=retire_ambiguous_transport,
        )

    async def _fail_response_created_timeout_requests(
        self: _HTTPBridgeUpstreamEventsService,
        session: _HTTPBridgeSession,
        *,
        request_ids: frozenset[str],
        timeout_seconds: float,
        error_code: str,
        error_message: str,
    ) -> tuple[_WebSocketRequestState, ...]:
        now = time.monotonic()
        with anyio.CancelScope(shield=True):
            async with session.pending_lock:
                expired_requests = tuple(
                    request_state
                    for request_state in session.pending_requests
                    if request_state.request_id in request_ids
                    and request_state.http_bridge_send_completed_at is not None
                    and request_state.response_id is None
                    and request_state.awaiting_response_created
                    and now >= request_state.http_bridge_send_completed_at + timeout_seconds
                )
                for request_state in expired_requests:
                    session.pending_requests.remove(request_state)
                session.queued_request_count = max(0, session.queued_request_count - len(expired_requests))
                session.pending_changed.set()

            for request_state in expired_requests:
                sent_at = request_state.http_bridge_send_completed_at
                age_ms = int((now - sent_at) * 1000) if sent_at is not None else None
                _log_http_bridge_event(
                    "response_created_timeout",
                    session.key,
                    account_id=session.account.id,
                    model=session.request_model,
                    pending_count=len(session.pending_requests),
                    detail=(
                        f"request_id={request_state.request_id}, age_ms={age_ms}, "
                        f"first_event_type={request_state.http_bridge_upstream_first_event_type}, "
                        f"has_previous_response_id={request_state.previous_response_id is not None}, "
                        f"proxy_injected={request_state.proxy_injected_previous_response_id}, "
                        f"retry_safe={request_state.fresh_upstream_request_is_retry_safe}"
                    ),
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )
            if expired_requests:
                await _await_shielded_cleanup(
                    self._fail_pending_websocket_requests(
                        account_id_value=session.account.id,
                        pending_requests=deque(expired_requests),
                        pending_lock=anyio.Lock(),
                        error_code=error_code,
                        error_message=error_message,
                        api_key=None,
                        response_create_gate=session.response_create_gate,
                    ),
                    label="HTTP bridge response-created-timeout cleanup",
                )
        return expired_requests

    async def _process_http_bridge_upstream_text(
        self: _HTTPBridgeUpstreamEventsService,
        session: _HTTPBridgeSession,
        text: str,
    ) -> None:
        event_account = session.account
        event_block = f"data: {text}\n\n"
        public_event_block = f"data: {sanitize_upstream_websocket_event_text(text)}\n\n"
        payload = parse_sse_data_json(event_block)
        event = parse_sse_event(event_block)
        event_type = _event_type_from_payload(event, payload)
        accounting_event = event
        accounting_event_type = event_type
        accounting_payload = payload
        response_id = _websocket_response_id(event, payload)
        error_message = _websocket_event_error_message(event_type, payload)
        is_previous_response_not_found_event = _is_previous_response_not_found_error(
            code=_normalize_error_code(
                _websocket_event_error_code(event_type, payload),
                _websocket_event_error_type(event_type, payload),
            ),
            param=_websocket_event_error_param(event_type, payload),
            message=error_message,
        )
        previous_response_id_hint = _previous_response_id_from_not_found_message(error_message)
        retryable_precreated_error_code: str | None = None
        retryable_precreated_request_state: _WebSocketRequestState | None = None
        retryable_no_text_error_code: str | None = None
        retryable_no_text_request_state: _WebSocketRequestState | None = None
        terminal_api_key_reservation: ApiKeyUsageReservationData | None = None
        terminal_reservation_preclaimed = False
        terminal_finalization_scheduled = False
        terminal_public_event_prepared = False
        grouped_finalization_claims: list[
            tuple[_WebSocketRequestState, ApiKeyUsageReservationData | None]
        ] = []

        def public_terminal_metadata() -> tuple[
            OpenAIEvent | None,
            dict[str, JsonValue] | None,
            str | None,
        ]:
            public_payload = parse_sse_data_json(public_event_block)
            public_event = parse_sse_event(public_event_block)
            return (
                public_event,
                public_payload,
                _event_type_from_payload(public_event, public_payload),
            )

        async def claim_and_schedule_retry_terminal(
            candidate: _WebSocketRequestState,
            *,
            prior_usage_charge_count: int,
            usage_charge_captured: bool,
        ) -> tuple[_WebSocketRequestState | None, bool]:
            nonlocal event, event_block, event_type, payload, public_event_block
            nonlocal terminal_public_event_prepared
            async with session.pending_lock:
                if candidate not in session.pending_requests:
                    return None, bool(session.pending_requests)

                # A failed retry should account for the terminal event once,
                # through the finalizer below, rather than retaining the
                # temporary per-attempt charge captured before the retry.
                if usage_charge_captured:
                    del candidate.usage_charges[prior_usage_charge_count:]

                session.pending_requests.remove(candidate)
                session.queued_request_count = max(0, session.queued_request_count - 1)
                reservation = candidate.api_key_reservation
                candidate.api_key_reservation = None
                session.pending_changed.set()
                has_other_pending_requests = bool(session.pending_requests)

                if (
                    candidate.previous_response_id is not None
                    and is_previous_response_not_found_event
                    and (response_id is not None or has_other_pending_requests)
                ):
                    candidate.error_http_status_override = 502
                    event, payload, event_type, rewritten_text = (
                        _maybe_rewrite_websocket_previous_response_not_found_event(
                            request_state=candidate,
                            event=event,
                            payload=payload,
                            event_type=event_type,
                            upstream_control=session.upstream_control,
                            original_text=text,
                        )
                    )
                    event_block = f"data: {rewritten_text}\n\n"
                    public_event_block = event_block

                if event_type == "error":
                    candidate.error_http_status_override = _http_error_status_from_payload(payload)
                    (
                        event_block,
                        payload,
                        event,
                        event_type,
                    ) = _normalize_http_bridge_error_event(
                        event=event,
                        payload=payload,
                        request_state=candidate,
                    )
                    public_event_block = event_block

                event_block = public_event_block
                terminal_public_event_prepared = True
                public_event, public_payload, public_event_type = public_terminal_metadata()

                # Enroll the finalizer before leaving the same critical
                # section that clears the shared reservation owner.  Caller
                # cancellation can therefore only observe ownership on the
                # pending state or on this tracked finalizer, never in a local
                # tuple between the two.
                self._schedule_websocket_request_finalization(
                    candidate,
                    account=event_account,
                    account_id_value=event_account.id,
                    event=public_event,
                    event_type=public_event_type,
                    payload=public_payload,
                    accounting_event=accounting_event,
                    accounting_event_type=accounting_event_type,
                    accounting_payload=accounting_payload,
                    stream_error_already_classified=True,
                    api_key=candidate.api_key,
                    api_key_reservation=reservation,
                    reservation_preclaimed=True,
                    upstream_control=session.upstream_control,
                    response_create_gate=session.response_create_gate,
                )
                return candidate, has_other_pending_requests

        def schedule_discarded_finalization(
            discarded_accounting: _DiscardedRequestAccounting,
        ) -> None:
            public_event, public_payload, public_event_type = public_terminal_metadata()
            discarded_accounting.request_state.discarded_accounting_resolution_future = (
                discarded_accounting.resolution_future
            )
            self._schedule_websocket_request_finalization(
                discarded_accounting.request_state,
                account=event_account,
                account_id_value=event_account.id,
                event=public_event,
                event_type=public_event_type,
                payload=public_payload,
                accounting_event=accounting_event,
                accounting_event_type=accounting_event_type,
                accounting_payload=accounting_payload,
                api_key=discarded_accounting.request_state.api_key or session.api_key,
                api_key_reservation=discarded_accounting.api_key_reservation,
                reservation_preclaimed=True,
                upstream_control=session.upstream_control,
                response_create_gate=session.response_create_gate,
            )

        restored_discarded_request_state: _WebSocketRequestState | None = None

        def restore_pending_discarded_accounting(
            discarded_accounting: _DiscardedRequestAccounting,
            *,
            bound_response_id: str | None,
            terminal: bool,
        ) -> bool:
            nonlocal restored_discarded_request_state
            request_state = discarded_accounting.request_state
            if request_state not in session.pending_requests:
                return False
            if terminal:
                request_state.api_key_reservation = discarded_accounting.api_key_reservation
                request_state.discarded_accounting_resolution_future = (
                    discarded_accounting.resolution_future
                )
            if bound_response_id is not None:
                request_state.response_id = bound_response_id
            restored_discarded_request_state = request_state
            return True

        async with session.pending_lock:
            if response_id is not None and response_id in session.discarded_response_ids:
                discarded_accounting = session.discarded_request_accounting.get(response_id)
                discarded_terminal = event_type in {
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                    "error",
                }
                if discarded_accounting is not None and restore_pending_discarded_accounting(
                    discarded_accounting,
                    bound_response_id=response_id,
                    terminal=discarded_terminal,
                ):
                    if discarded_terminal:
                        session.discarded_response_ids.discard(response_id)
                        session.discarded_request_accounting.pop(response_id, None)
                elif discarded_terminal:
                    session.discarded_response_ids.discard(response_id)
                    discarded_accounting = session.discarded_request_accounting.pop(
                        response_id,
                        None,
                    )
                    if discarded_accounting is not None:
                        schedule_discarded_finalization(discarded_accounting)
                if restored_discarded_request_state is None:
                    return
            if (
                restored_discarded_request_state is None
                and response_id is not None
                and session.anonymous_discarded_request_accounting
            ):
                matched_pending_request = _find_websocket_request_state_by_response_id(
                    session.pending_requests,
                    response_id,
                )
                anonymous_claim_precedes_pending = event_type == "response.created"
                if (
                    len(session.anonymous_discarded_request_accounting) == 1
                    and (anonymous_claim_precedes_pending or matched_pending_request is None)
                ):
                    anonymous_request_id, discarded_accounting = next(
                        iter(session.anonymous_discarded_request_accounting.items())
                    )
                    session.anonymous_discarded_request_accounting.pop(
                        anonymous_request_id,
                        None,
                    )
                    if restore_pending_discarded_accounting(
                        discarded_accounting,
                        bound_response_id=response_id,
                        terminal=event_type
                        in {
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                            "error",
                        },
                    ):
                        if event_type not in {
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                            "error",
                        }:
                            session.discarded_response_ids.add(response_id)
                            session.discarded_request_accounting[
                                response_id
                            ] = discarded_accounting
                    elif event_type in {
                        "response.completed",
                        "response.failed",
                        "response.incomplete",
                        "error",
                    }:
                        schedule_discarded_finalization(discarded_accounting)
                    else:
                        session.discarded_response_ids.add(response_id)
                        session.discarded_request_accounting[response_id] = discarded_accounting
                    if restored_discarded_request_state is None:
                        return
                if anonymous_claim_precedes_pending or matched_pending_request is None:
                    if restored_discarded_request_state is None:
                        session.upstream_control.retire_ambiguous_transport = True
                        return
            anonymous_terminal = (
                response_id is None
                and event_type
                in {"response.completed", "response.failed", "response.incomplete", "error"}
            )
            discarded_accounting_count = len(session.discarded_request_accounting) + len(
                session.anonymous_discarded_request_accounting
            )
            anonymous_terminal_candidate_count = _anonymous_terminal_candidate_count(
                session.pending_requests,
                discarded_response_ids=session.discarded_response_ids,
                discarded_request_accounting=session.discarded_request_accounting,
                anonymous_discarded_request_accounting=(
                    session.anonymous_discarded_request_accounting
                ),
            )
            if (
                restored_discarded_request_state is None
                and anonymous_terminal
                and anonymous_terminal_candidate_count == 1
                and discarded_accounting_count == 1
                and not session.discarded_response_ids.difference(
                    session.discarded_request_accounting
                )
            ):
                if session.discarded_request_accounting:
                    discarded_response_id, discarded_accounting = next(
                        iter(session.discarded_request_accounting.items())
                    )
                    session.discarded_request_accounting.pop(discarded_response_id, None)
                    session.discarded_response_ids.discard(discarded_response_id)
                else:
                    anonymous_request_id, discarded_accounting = next(
                        iter(session.anonymous_discarded_request_accounting.items())
                    )
                    session.anonymous_discarded_request_accounting.pop(
                        anonymous_request_id,
                        None,
                    )
                if not restore_pending_discarded_accounting(
                    discarded_accounting,
                    bound_response_id=None,
                    terminal=True,
                ):
                    schedule_discarded_finalization(discarded_accounting)
                    return
            if (
                restored_discarded_request_state is None
                and anonymous_terminal
                and not is_previous_response_not_found_event
                and anonymous_terminal_candidate_count != 1
            ):
                session.upstream_control.retire_ambiguous_transport = True
                return
            if restored_discarded_request_state is None and response_id is None and (
                session.discarded_response_ids
                or session.anonymous_discarded_request_accounting
            ):
                session.upstream_control.retire_ambiguous_transport = True
                return

            matched_request_state = None
            created_request_state = None
            has_other_pending_requests = False
            grouped_previous_response_request_states: list[_WebSocketRequestState] = []
            if event_type == "response.created":
                matched_request_state = (
                    restored_discarded_request_state
                    or _assign_websocket_response_id(session.pending_requests, response_id)
                )
                created_request_state = matched_request_state
                release_create_gate = matched_request_state is not None
            elif response_id is not None:
                matched_request_state = _find_websocket_request_state_by_response_id(
                    session.pending_requests,
                    response_id,
                )
                release_create_gate = False
            elif response_id is None:
                matched_request_state = (
                    restored_discarded_request_state
                    or _match_websocket_request_state_for_anonymous_event(
                        session.pending_requests,
                        prefer_previous_response_not_found=is_previous_response_not_found_event,
                        previous_response_id_hint=previous_response_id_hint,
                        error_message=error_message,
                    )
                )
                release_create_gate = False
            else:
                release_create_gate = False

            if matched_request_state is not None:
                actual_service_tier = _service_tier_from_event_payload(payload)
                if actual_service_tier is not None:
                    matched_request_state.actual_service_tier = actual_service_tier
                    matched_request_state.service_tier = actual_service_tier
            if (
                restored_discarded_request_state is None
                and event_type in {"response.failed", "error"}
                and matched_request_state is not None
            ):
                retryable_precreated_error_code = _http_bridge_precreated_failover_error_code(
                    matched_request_state,
                    event_type=event_type,
                    payload=payload,
                    has_other_pending_requests=_has_other_pending_requests(
                        session.pending_requests,
                        matched_request_state,
                    ),
                )
                if retryable_precreated_error_code is not None:
                    retryable_precreated_request_state = matched_request_state
                else:
                    retryable_no_text_error_code = _http_bridge_no_text_failover_error_code(
                        matched_request_state,
                        event_type=event_type,
                        payload=payload,
                        has_other_pending_requests=any(
                            request_state is not matched_request_state for request_state in session.pending_requests
                        ),
                    )
                    if retryable_no_text_error_code is not None:
                        retryable_no_text_request_state = matched_request_state

            terminal_request_state = None
            if (
                restored_discarded_request_state is None
                and retryable_precreated_request_state is None
                and event_type in {"response.failed", "error"}
                and response_id is not None
            ):
                precreated_terminal_request_state = _match_websocket_request_state_for_precreated_terminal_event(
                    session.pending_requests,
                )
                if precreated_terminal_request_state is not None:
                    retryable_precreated_error_code = _http_bridge_precreated_failover_error_code(
                        precreated_terminal_request_state,
                        event_type=event_type,
                        payload=payload,
                        has_other_pending_requests=_has_other_pending_requests(
                            session.pending_requests,
                            precreated_terminal_request_state,
                        ),
                    )
                    if retryable_precreated_error_code is not None:
                        retryable_precreated_request_state = precreated_terminal_request_state
            if (
                retryable_precreated_request_state is None
                and retryable_no_text_request_state is None
                and event_type in {"response.completed", "response.failed", "response.incomplete", "error"}
            ):
                terminal_request_state = _pop_terminal_websocket_request_state(
                    session.pending_requests,
                    response_id=response_id,
                    fallback_request_state=matched_request_state,
                    prefer_previous_response_not_found=is_previous_response_not_found_event,
                    previous_response_id_hint=previous_response_id_hint,
                    error_message=error_message,
                    allow_precreated_terminal_fallback=event_type
                    in {
                        "response.failed",
                        "response.incomplete",
                        "error",
                    },
                )
                if terminal_request_state is None and is_previous_response_not_found_event:
                    grouped_previous_response_request_states = _pop_matching_websocket_request_states(
                        session.pending_requests,
                        _matching_websocket_request_states_for_previous_response_error(
                            session.pending_requests,
                            previous_response_id_hint=previous_response_id_hint,
                            error_message=error_message,
                        ),
                    )
                if terminal_request_state is not None:
                    session.queued_request_count = max(0, session.queued_request_count - 1)
                    terminal_api_key_reservation = terminal_request_state.api_key_reservation
                    terminal_request_state.api_key_reservation = None
                    terminal_reservation_preclaimed = True
                elif grouped_previous_response_request_states:
                    for grouped_request_state in grouped_previous_response_request_states:
                        grouped_finalization_claims.append(
                            (
                                grouped_request_state,
                                grouped_request_state.api_key_reservation,
                            )
                        )
                        grouped_request_state.api_key_reservation = None
                    session.queued_request_count = max(
                        0,
                        session.queued_request_count - len(grouped_previous_response_request_states),
                    )
                if terminal_request_state is not None or grouped_previous_response_request_states:
                    session.pending_changed.set()
                has_other_pending_requests = bool(session.pending_requests)

        if retryable_precreated_request_state is not None and retryable_precreated_error_code is not None:
            retryable_precreated_service_tier = (
                _service_tier_from_event_payload(payload)
                or retryable_precreated_request_state.actual_service_tier
                or retryable_precreated_request_state.service_tier
            )
            retryable_precreated_requested_service_tier = (
                retryable_precreated_request_state.requested_service_tier
            )
            prior_usage_charge_count = len(retryable_precreated_request_state.usage_charges)
            self._capture_websocket_retry_usage(
                retryable_precreated_request_state,
                account_id_value=event_account.id,
                event=event,
                event_type=event_type,
                payload=payload,
                api_key=retryable_precreated_request_state.api_key,
                service_tier=retryable_precreated_service_tier,
                requested_service_tier=retryable_precreated_requested_service_tier,
                schedule_request_log=False,
            )
            usage_charge_captured = (
                len(retryable_precreated_request_state.usage_charges) > prior_usage_charge_count
            )
            try:
                (
                    retried,
                    terminal_request_state,
                    has_other_pending_requests,
                    _retry_terminal_api_key_reservation,
                ) = await self._retry_http_bridge_terminal_failure(
                    session,
                    request_state=retryable_precreated_request_state,
                    error_code=retryable_precreated_error_code,
                    error_message=error_message,
                    payload=payload,
                    response_id=response_id,
                    prefer_previous_response_not_found=is_previous_response_not_found_event,
                    previous_response_id_hint=previous_response_id_hint,
                    reset_response_state=False,
                    allow_precreated_terminal_fallback=True,
                )
            except BaseException:
                if usage_charge_captured:
                    self._capture_websocket_retry_usage(
                        retryable_precreated_request_state,
                        account_id_value=event_account.id,
                        event=event,
                        event_type=event_type,
                        payload=payload,
                        api_key=retryable_precreated_request_state.api_key,
                        service_tier=retryable_precreated_service_tier,
                        requested_service_tier=retryable_precreated_requested_service_tier,
                        append_usage_charge=False,
                    )
                raise
            if retried:
                self._capture_websocket_retry_usage(
                    retryable_precreated_request_state,
                    account_id_value=event_account.id,
                    event=event,
                    event_type=event_type,
                    payload=payload,
                    api_key=retryable_precreated_request_state.api_key,
                    service_tier=retryable_precreated_service_tier,
                    requested_service_tier=retryable_precreated_requested_service_tier,
                    append_usage_charge=False,
                )
                return
            if terminal_request_state is not None:
                terminal_request_state, has_other_pending_requests = (
                    await claim_and_schedule_retry_terminal(
                        terminal_request_state,
                        prior_usage_charge_count=prior_usage_charge_count,
                        usage_charge_captured=usage_charge_captured,
                    )
                )
                terminal_finalization_scheduled = terminal_request_state is not None

        if retryable_no_text_request_state is not None and retryable_no_text_error_code is not None:
            retryable_no_text_service_tier = (
                _service_tier_from_event_payload(payload)
                or retryable_no_text_request_state.actual_service_tier
                or retryable_no_text_request_state.service_tier
            )
            retryable_no_text_requested_service_tier = retryable_no_text_request_state.requested_service_tier
            prior_usage_charge_count = len(retryable_no_text_request_state.usage_charges)
            self._capture_websocket_retry_usage(
                retryable_no_text_request_state,
                account_id_value=event_account.id,
                event=event,
                event_type=event_type,
                payload=payload,
                api_key=retryable_no_text_request_state.api_key,
                service_tier=retryable_no_text_service_tier,
                requested_service_tier=retryable_no_text_requested_service_tier,
                schedule_request_log=False,
            )
            usage_charge_captured = len(retryable_no_text_request_state.usage_charges) > prior_usage_charge_count
            try:
                (
                    retried,
                    terminal_request_state,
                    has_other_pending_requests,
                    _retry_terminal_api_key_reservation,
                ) = await self._retry_http_bridge_terminal_failure(
                    session,
                    request_state=retryable_no_text_request_state,
                    error_code=retryable_no_text_error_code,
                    error_message=error_message,
                    payload=payload,
                    response_id=response_id,
                    prefer_previous_response_not_found=is_previous_response_not_found_event,
                    previous_response_id_hint=previous_response_id_hint,
                    reset_response_state=True,
                    allow_precreated_terminal_fallback=False,
                )
            except BaseException:
                if usage_charge_captured:
                    self._capture_websocket_retry_usage(
                        retryable_no_text_request_state,
                        account_id_value=event_account.id,
                        event=event,
                        event_type=event_type,
                        payload=payload,
                        api_key=retryable_no_text_request_state.api_key,
                        service_tier=retryable_no_text_service_tier,
                        requested_service_tier=retryable_no_text_requested_service_tier,
                        append_usage_charge=False,
                    )
                raise
            if retried:
                self._capture_websocket_retry_usage(
                    retryable_no_text_request_state,
                    account_id_value=event_account.id,
                    event=event,
                    event_type=event_type,
                    payload=payload,
                    api_key=retryable_no_text_request_state.api_key,
                    service_tier=retryable_no_text_service_tier,
                    requested_service_tier=retryable_no_text_requested_service_tier,
                    append_usage_charge=False,
                )
                return
            if terminal_request_state is not None:
                terminal_request_state, has_other_pending_requests = (
                    await claim_and_schedule_retry_terminal(
                        terminal_request_state,
                        prior_usage_charge_count=prior_usage_charge_count,
                        usage_charge_captured=usage_charge_captured,
                    )
                )
                terminal_finalization_scheduled = terminal_request_state is not None

        if len(grouped_previous_response_request_states) > 1:
            session.upstream_control.reconnect_requested = True
            grouped_usage_owner = (
                min(
                    grouped_previous_response_request_states,
                    key=lambda pending: (pending.started_at, pending.request_id),
                )
                if accounting_event is not None
                and accounting_event.response is not None
                and accounting_event.response.usage is not None
                else None
            )

            grouped_terminal_deliveries: list[tuple[_WebSocketRequestState, str]] = []
            for grouped_request_state, (_claimed_state, grouped_api_key_reservation) in zip(
                grouped_previous_response_request_states,
                grouped_finalization_claims,
                strict=True,
            ):
                grouped_request_state.error_http_status_override = 502
                (
                    _grouped_downstream_text,
                    grouped_event_block,
                    grouped_event,
                    grouped_payload,
                    grouped_event_type,
                ) = _build_stream_incomplete_terminal_event_for_request(grouped_request_state)
                self._schedule_websocket_request_finalization(
                    grouped_request_state,
                    account=event_account,
                    account_id_value=event_account.id,
                    event=grouped_event,
                    event_type=grouped_event_type,
                    payload=grouped_payload,
                    accounting_event=(
                        accounting_event if grouped_request_state is grouped_usage_owner else None
                    ),
                    accounting_event_type=(
                        accounting_event_type if grouped_request_state is grouped_usage_owner else None
                    ),
                    accounting_payload=(
                        accounting_payload if grouped_request_state is grouped_usage_owner else None
                    ),
                    api_key=grouped_request_state.api_key,
                    api_key_reservation=grouped_api_key_reservation,
                    reservation_preclaimed=True,
                    upstream_control=session.upstream_control,
                    response_create_gate=session.response_create_gate,
                )
                grouped_terminal_deliveries.append((grouped_request_state, grouped_event_block))

            async def deliver_grouped_terminal_events() -> None:
                for grouped_request_state, grouped_event_block in grouped_terminal_deliveries:
                    if grouped_request_state.event_queue is not None:
                        await grouped_request_state.event_queue.put(grouped_event_block)
                        await grouped_request_state.event_queue.put(None)

            await _await_shielded_cleanup(
                deliver_grouped_terminal_events(),
                label="grouped HTTP bridge terminal delivery",
            )
            return

        if len(grouped_previous_response_request_states) == 1 and terminal_request_state is None:
            terminal_request_state = grouped_previous_response_request_states[0]
            terminal_api_key_reservation = grouped_finalization_claims[0][1]
            terminal_reservation_preclaimed = True

        status_request_state = terminal_request_state or matched_request_state
        if status_request_state is None and is_previous_response_not_found_event:
            session.upstream_control.reconnect_requested = True
            return

        if (
            not terminal_public_event_prepared
            and
            status_request_state is not None
            and status_request_state.previous_response_id is not None
            and is_previous_response_not_found_event
            and (response_id is not None or has_other_pending_requests)
        ):
            status_request_state.error_http_status_override = 502
            event, payload, event_type, rewritten_text = _maybe_rewrite_websocket_previous_response_not_found_event(
                request_state=status_request_state,
                event=event,
                payload=payload,
                event_type=event_type,
                upstream_control=session.upstream_control,
                original_text=text,
            )
            event_block = f"data: {rewritten_text}\n\n"
            public_event_block = event_block

        if event_type == "response.completed" and terminal_request_state is not None:
            # Record the completed response id regardless of input shape so
            # subsequent turns (including ones that never populated
            # input_item_count, e.g. string inputs) can still reuse this
            # anchor for continuity lookups.
            if response_id is not None:
                session.last_completed_response_id = response_id
            # Prefix trimming is only meaningful for list-shaped inputs, so
            # keep the input-count / fingerprint update scoped to that path.
            if terminal_request_state.input_item_count > 0:
                session.last_completed_input_count = terminal_request_state.input_item_count
                session.last_completed_input_prefix_fingerprint = terminal_request_state.input_full_fingerprint

        if not terminal_public_event_prepared and event_type == "error":
            http_status = _http_error_status_from_payload(payload)
            if status_request_state is not None:
                status_request_state.error_http_status_override = http_status
            (
                event_block,
                payload,
                event,
                event_type,
            ) = _normalize_http_bridge_error_event(
                event=event,
                payload=payload,
                request_state=terminal_request_state or matched_request_state,
            )
            public_event_block = event_block

        event_block = public_event_block

        if terminal_request_state is None:
            if event_type == "response.created" and release_create_gate and created_request_state is not None:
                _release_websocket_response_create_gate(created_request_state, session.response_create_gate)
            if matched_request_state is not None:
                _record_http_bridge_upstream_event(matched_request_state, event_type)
                _log_http_bridge_latency_breakdown(session, matched_request_state, event_type=event_type)
            if matched_request_state is not None and matched_request_state.event_queue is not None:
                await matched_request_state.event_queue.put(event_block)
            return

        if not terminal_finalization_scheduled:
            public_event, public_payload, public_event_type = public_terminal_metadata()
            self._schedule_websocket_request_finalization(
                terminal_request_state,
                account=event_account,
                account_id_value=event_account.id,
                event=public_event,
                event_type=public_event_type,
                payload=public_payload,
                accounting_event=accounting_event,
                accounting_event_type=accounting_event_type,
                accounting_payload=accounting_payload,
                api_key=terminal_request_state.api_key,
                api_key_reservation=terminal_api_key_reservation,
                reservation_preclaimed=terminal_reservation_preclaimed,
                upstream_control=session.upstream_control,
                response_create_gate=session.response_create_gate,
            )

        async def deliver_terminal_request() -> None:
            if matched_request_state is not None:
                _record_http_bridge_upstream_event(matched_request_state, event_type)
                _log_http_bridge_latency_breakdown(session, matched_request_state, event_type=event_type)
            if matched_request_state is not None and matched_request_state.event_queue is not None:
                await matched_request_state.event_queue.put(event_block)

            if terminal_request_state is not matched_request_state:
                _record_http_bridge_upstream_event(terminal_request_state, event_type)
                _log_http_bridge_latency_breakdown(session, terminal_request_state, event_type=event_type)
            if terminal_request_state is not matched_request_state and terminal_request_state.event_queue is not None:
                await terminal_request_state.event_queue.put(event_block)

            if response_id is not None and matched_request_state is not None and event_type == "response.completed":
                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    self._register_http_bridge_previous_response_id(
                        session,
                        response_id,
                        input_item_count=(
                            matched_request_state.input_item_count
                            if matched_request_state.input_item_count > 0
                            else None
                        ),
                        input_full_fingerprint=(
                            matched_request_state.input_full_fingerprint
                            if matched_request_state.input_item_count > 0
                            else None
                        ),
                    ),
                    name=f"bridge-terminal-alias-{response_id}",
                    label=f"HTTP bridge terminal alias registration response_id={response_id}",
                )

            if event_type in {"response.failed", "response.incomplete", "error"}:
                error_code = None
                if event_type == "error":
                    error = event.error if event else None
                    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                elif event and event.response:
                    error = event.response.error
                    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                _log_http_bridge_event(
                    "terminal_error",
                    session.key,
                    account_id=event_account.id,
                    model=session.request_model,
                    detail=error_code,
                    pending_count=await self._http_bridge_pending_count(session),
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )

            if terminal_request_state.event_queue is not None:
                await terminal_request_state.event_queue.put(None)

        await _await_shielded_cleanup(
            deliver_terminal_request(),
            label="HTTP bridge terminal request delivery",
        )
