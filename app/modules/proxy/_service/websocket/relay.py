from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import anyio
from fastapi import WebSocket

from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.proxy_websocket import (
    UpstreamResponsesWebSocket,
    UpstreamWebSocketMessage,
    sanitize_upstream_websocket_event_text,
)
from app.core.errors import OpenAIErrorEnvelope, openai_error, response_failed_event
from app.core.openai.models import OpenAIEvent
from app.core.openai.parsing import parse_sse_event
from app.core.types import JsonValue
from app.core.utils.sse import format_sse_event, parse_sse_data_json
from app.db.models import Account
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.budget import _remaining_budget_seconds, _request_deadline_at
from app.modules.proxy._service.service_tier import _service_tier_from_event_payload
from app.modules.proxy._service.support import (
    _anonymous_terminal_candidate_count,
    _apply_usage_charges_to_settlement,
    _await_cancelled_task,
    _await_operation_before_hard_timeout,
    _await_shielded_cleanup,
    _DiscardedRequestAccounting,
    _DownstreamWebSocketActivity,
    _event_type_from_payload,
    _release_websocket_response_create_gate,
    _schedule_tracked_background_task,
    _should_penalize_stream_error,
    _stream_settlement_error_payload,
    _stream_settlement_has_authoritative_usage,
    _StreamSettlement,
    _track_existing_background_task,
    _usage_charge_from_response_usage,
    _WebSocketReceiveTimeout,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy._service.websocket.events import (
    _WEBSOCKET_TRANSPARENT_REPLAY_ERROR_CODES,
    _assign_websocket_response_id,
    _build_stream_incomplete_terminal_event_for_request,
    _find_websocket_request_state_by_response_id,
    _is_previous_response_not_found_error,
    _match_websocket_request_state_for_anonymous_event,
    _matching_websocket_request_states_for_previous_response_error,
    _pop_matching_websocket_request_states,
    _pop_replayable_precreated_websocket_request_state,
    _pop_terminal_websocket_request_state,
    _previous_response_id_from_not_found_message,
    _serialize_websocket_error_event,
    _upstream_websocket_disconnect_message,
    _websocket_event_error_code,
    _websocket_event_error_message,
    _websocket_event_error_param,
    _websocket_event_error_type,
    _websocket_precreated_retry_error_code,
    _websocket_response_id,
    _wrapped_websocket_error_event,
)
from app.modules.proxy.helpers import (
    _normalize_error_code,
    _upstream_error_from_openai,
)
from app.modules.proxy.load_balancer import LoadBalancer

logger = logging.getLogger("app.modules.proxy.service")

_DIRECT_WEBSOCKET_CLOSE_OBSERVATION_SECONDS = 1.0
_WEBSOCKET_TERMINAL_LOG_TIMEOUT_SECONDS = 0.1


def _websocket_receive_timeout_for_pending_requests(
    pending_requests: Sequence[float | _WebSocketRequestState],
    *,
    proxy_request_budget_seconds: float,
    stream_idle_timeout_seconds: float,
) -> _WebSocketReceiveTimeout | None:
    if not pending_requests:
        return None

    idle_timeout_seconds = max(0.001, stream_idle_timeout_seconds)
    request_deadline = min(
        (
            request_or_started_at + proxy_request_budget_seconds
            if isinstance(request_or_started_at, (int, float))
            else _request_deadline_at(request_or_started_at, proxy_request_budget_seconds)
        )
        for request_or_started_at in pending_requests
    )
    remaining_budget = _remaining_budget_seconds(request_deadline)

    if remaining_budget <= 0:
        return _WebSocketReceiveTimeout(
            timeout_seconds=0.0,
            error_code="upstream_request_timeout",
            error_message="Proxy request budget exhausted",
        )
    if idle_timeout_seconds <= remaining_budget:
        return _WebSocketReceiveTimeout(
            timeout_seconds=idle_timeout_seconds,
            error_code="stream_idle_timeout",
            error_message="Upstream stream idle timeout",
            fail_all_pending=True,
        )
    return _WebSocketReceiveTimeout(
        timeout_seconds=remaining_budget,
        error_code="upstream_request_timeout",
        error_message="Proxy request budget exhausted",
    )


class _WebSocketRelayService(Protocol):
    _load_balancer: LoadBalancer
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

    async def _write_websocket_log_before_deadline(
        self,
        request_state: _WebSocketRequestState,
        operation: Callable[[], Awaitable[None]],
        *,
        label: str,
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

    @staticmethod
    def _maybe_rewrite_websocket_previous_response_not_found_event_compatible(
        *,
        request_state: _WebSocketRequestState,
        event: OpenAIEvent | None,
        payload: dict[str, JsonValue] | None,
        event_type: str | None,
        upstream_control: _WebSocketUpstreamControl,
        original_text: str,
    ) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]: ...

    @staticmethod
    def _rewrite_websocket_previous_response_owner_unavailable_event_compatible(
        *,
        request_state: _WebSocketRequestState,
    ) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]: ...

    @staticmethod
    def _sanitize_websocket_connect_failure_compatible(
        *,
        request_state: _WebSocketRequestState,
        status_code: int,
        payload: OpenAIErrorEnvelope,
        error_code: str,
        error_message: str,
    ) -> tuple[int, OpenAIErrorEnvelope, str, str]: ...

    @staticmethod
    def _maybe_dump_oversized_response_create_request_compatible(
        request_state: _WebSocketRequestState,
        *,
        account_id_value: str | None,
        error_code: str,
        error_message: str | None,
    ) -> None: ...

    @staticmethod
    def _release_request_account_model_concurrency(request_state: _WebSocketRequestState) -> None: ...

    def _remember_websocket_previous_response_owner(
        self,
        *,
        previous_response_id: str | None,
        api_key_id: str | None,
        account_id: str | None,
        session_id: str | None = None,
    ) -> None: ...

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

    async def _settle_stream_api_key_usage(
        self,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool: ...

    async def _settle_stream_api_key_usage_with_fallback(
        self,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool: ...

    async def _release_websocket_reservation(
        self,
        reservation: ApiKeyUsageReservationData | None,
    ) -> None: ...

    async def _release_websocket_reservation_with_retry(
        self,
        reservation: ApiKeyUsageReservationData,
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

    def _enroll_discarded_accounting_guardian(
        self,
        accounting: _DiscardedRequestAccounting,
        *,
        api_key: ApiKeyData | None,
        error_message: str,
    ) -> None: ...

    def _schedule_websocket_reservation_release(
        self,
        reservation: ApiKeyUsageReservationData | None,
        *,
        reason: str,
    ) -> None: ...

    def _schedule_websocket_terminal_cleanup(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
    ) -> None: ...

    async def _write_request_log(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        model: str | None,
        latency_ms: int,
        status: str,
        latency_first_token_ms: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        reasoning_effort: str | None = None,
        transport: str | None = None,
        service_tier: str | None = None,
        requested_service_tier: str | None = None,
        actual_service_tier: str | None = None,
        session_id: str | None = None,
    ) -> None: ...

    async def _write_websocket_connect_failure(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
    ) -> None: ...

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

    async def _fail_expired_pending_websocket_requests(
        self,
        *,
        account_id_value: str | None,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        request_budget_seconds: float,
        error_code: str,
        error_message: str,
        api_key: ApiKeyData | None,
        websocket: WebSocket | None = None,
        client_send_lock: anyio.Lock | None = None,
        response_create_gate: asyncio.Semaphore | None = None,
        upstream_control: _WebSocketUpstreamControl,
    ) -> bool: ...

    async def _transfer_pending_websocket_request_accounting(
        self,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        upstream_control: _WebSocketUpstreamControl,
        api_key: ApiKeyData | None = None,
    ) -> deque[_WebSocketRequestState] | None: ...

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

    async def _next_websocket_receive_timeout(
        self,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        proxy_request_budget_seconds: float,
        stream_idle_timeout_seconds: float,
    ) -> _WebSocketReceiveTimeout | None: ...

    async def _process_upstream_websocket_text(
        self,
        text: str,
        *,
        account: Account,
        account_id_value: str,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        api_key: ApiKeyData | None,
        upstream_control: _WebSocketUpstreamControl,
        response_create_gate: asyncio.Semaphore | None,
    ) -> str: ...

    async def _send_downstream_websocket_text(
        self,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        text: str,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None: ...

    async def _send_downstream_websocket_bytes(
        self,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        data: bytes,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None: ...

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

    async def _emit_websocket_terminal_error(
        self,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
        error_type: str = "server_error",
        error_param: str | None = None,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None: ...


class _WebSocketRelayMixin:
    async def _write_websocket_log_before_deadline(
        self: _WebSocketRelayService,
        request_state: _WebSocketRequestState,
        operation: Callable[[], Awaitable[None]],
        *,
        label: str,
    ) -> None:
        timeout_seconds = _WEBSOCKET_TERMINAL_LOG_TIMEOUT_SECONDS
        if request_state.request_deadline_at is not None:
            timeout_seconds = min(
                timeout_seconds,
                _remaining_budget_seconds(request_state.request_deadline_at),
            )
        if timeout_seconds <= 0:
            logger.warning(
                "Skipping websocket request log after deadline request_id=%s",
                request_state.request_log_id or request_state.request_id,
            )
            return
        try:
            await _await_operation_before_hard_timeout(
                operation(),
                timeout_seconds=timeout_seconds,
                tasks=self._proxy_cleanup_tasks,
                label=label,
            )
        except TimeoutError:
            logger.warning(
                "Websocket request log exceeded hard observation bound request_id=%s",
                request_state.request_log_id or request_state.request_id,
            )

    def _enroll_discarded_accounting_guardian(
        self: _WebSocketRelayService,
        accounting: _DiscardedRequestAccounting,
        *,
        api_key: ApiKeyData | None,
        error_message: str,
    ) -> None:
        if accounting.resolution_future is not None:
            return
        resolution_future = asyncio.get_running_loop().create_future()
        accounting.resolution_future = resolution_future

        def spawn_guardian() -> None:
            started = False

            async def guard_accounting() -> None:
                nonlocal started
                started = True
                try:
                    resolution = await asyncio.shield(resolution_future)
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        current_task.uncancel()
                    if resolution_future.done() and not resolution_future.cancelled():
                        resolution = resolution_future.result()
                    else:
                        resolution = "fallback"
                    if not resolution_future.done():
                        resolution_future.set_result(resolution)

                if resolution == "fallback":
                    operation = self._settle_or_release_failed_websocket_reservation(
                        request_state=accounting.request_state,
                        reservation=accounting.api_key_reservation,
                        api_key=accounting.request_state.api_key or api_key,
                        error_code="stream_incomplete",
                        error_message=error_message,
                    )
                else:
                    operation = resolution()
                await _await_shielded_cleanup(
                    operation,
                    label=(
                        "direct WebSocket discarded accounting guardian "
                        f"request_id={accounting.request_state.request_id}"
                    ),
                )

            guardian_task = asyncio.create_task(
                guard_accounting(),
                name=(
                    "direct-websocket-discarded-guardian-"
                    f"{accounting.request_state.request_id}"
                ),
            )
            _track_existing_background_task(
                self._proxy_cleanup_tasks,
                guardian_task,
                label=(
                    "direct WebSocket discarded guardian "
                    f"request_id={accounting.request_state.request_id}"
                ),
            )

            def recover_prestart_cancellation(completed: asyncio.Task[None]) -> None:
                if completed.cancelled() and not started:
                    spawn_guardian()

            guardian_task.add_done_callback(recover_prestart_cancellation)

        spawn_guardian()

    async def _relay_upstream_websocket_messages(
        self: _WebSocketRelayService,
        websocket: WebSocket,
        upstream: UpstreamResponsesWebSocket,
        *,
        account: Account,
        account_id_value: str,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        client_send_lock: anyio.Lock,
        api_key: ApiKeyData | None,
        upstream_control: _WebSocketUpstreamControl,
        response_create_gate: asyncio.Semaphore | None,
        proxy_request_budget_seconds: float,
        stream_idle_timeout_seconds: float,
        downstream_activity: _DownstreamWebSocketActivity,
        pending_changed: asyncio.Event | None = None,
    ) -> None:
        upstream_close_started = False
        pending_changed = pending_changed or asyncio.Event()
        receive_task: asyncio.Task[UpstreamWebSocketMessage] | None = None

        async def close_upstream_after_receive() -> None:
            nonlocal upstream_close_started
            if upstream_close_started:
                return
            upstream_close_started = True
            try:
                await upstream.close()
            except Exception:
                logger.debug("Failed to close upstream websocket", exc_info=True)

        def mark_receive_detached() -> None:
            upstream_control.detached_receive_pending = True

        async def drain_discarded_request_accounting() -> None:
            async with pending_lock:
                upstream_control.discarded_accounting_sealed = True
                upstream_control.detached_receive_pending = False
                discarded = (
                    *upstream_control.discarded_request_accounting.values(),
                    *upstream_control.anonymous_discarded_request_accounting.values(),
                )
                upstream_control.discarded_request_accounting.clear()
                upstream_control.anonymous_discarded_request_accounting.clear()
                upstream_control.discarded_response_ids.clear()
            for accounting in discarded:
                resolution_future = accounting.resolution_future
                if resolution_future is not None:
                    if not resolution_future.done():
                        resolution_future.set_result("fallback")
                    continue
                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    self._settle_or_release_failed_websocket_reservation(
                        request_state=accounting.request_state,
                        reservation=accounting.api_key_reservation,
                        api_key=accounting.request_state.api_key or api_key,
                        error_code="stream_incomplete",
                        error_message=(
                            "Upstream WebSocket closed before discarded response completed"
                        ),
                    ),
                    name=f"direct-websocket-discarded-settlement-{accounting.request_state.request_id}",
                    label=(
                        "direct WebSocket discarded settlement "
                        f"request_id={accounting.request_state.request_id}"
                    ),
                )

        async def reconcile_late_receive(message: UpstreamWebSocketMessage) -> None:
            if message.kind == "text" and message.text is not None:
                await self._process_upstream_websocket_text(
                    getattr(message, "raw_text", None) or message.text,
                    account=account,
                    account_id_value=account_id_value,
                    pending_requests=pending_requests,
                    pending_lock=pending_lock,
                    api_key=api_key,
                    upstream_control=upstream_control,
                    response_create_gate=response_create_gate,
                )

        async def finish_late_receive() -> None:
            await drain_discarded_request_accounting()
            await close_upstream_after_receive()

        async def stop_pending_change_wait(task: asyncio.Task[bool]) -> None:
            if task.done():
                task.result()
                return
            await _await_cancelled_task(
                task,
                label=f"direct WebSocket pending-change wait account_id={account_id_value}",
            )

        async def retire_active_receive() -> None:
            """Transfer an in-flight receive to bounded cleanup before retiring its socket."""
            nonlocal receive_task
            active_receive = receive_task
            receive_task = None
            if active_receive is None:
                return
            upstream_control.upstream_close_owned = True
            try:
                completed_message = await _await_operation_before_hard_timeout(
                    active_receive,
                    timeout_seconds=0.000001,
                    tasks=self._proxy_cleanup_tasks,
                    label=f"direct WebSocket receive retirement account_id={account_id_value}",
                    late_result_cleanup=reconcile_late_receive,
                    late_completion_cleanup=finish_late_receive,
                    on_detach=mark_receive_detached,
                )
                await reconcile_late_receive(completed_message)
            except (asyncio.CancelledError, TimeoutError, Exception):
                pass
            if not upstream_control.detached_receive_pending:
                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    close_upstream_after_receive(),
                    name=f"direct-websocket-receive-close-{time.monotonic_ns()}",
                    label=f"direct WebSocket receive close account_id={account_id_value}",
                )

        async def close_upstream_for_reconnect(
            request_state: _WebSocketRequestState | None,
            *,
            reason: str,
        ) -> None:
            upstream_control.upstream_close_owned = True
            remaining = _DIRECT_WEBSOCKET_CLOSE_OBSERVATION_SECONDS
            if request_state is not None:
                remaining = min(
                    remaining,
                    _remaining_budget_seconds(
                        _request_deadline_at(request_state, proxy_request_budget_seconds),
                    ),
                )
            try:
                await _await_operation_before_hard_timeout(
                    upstream.close(),
                    timeout_seconds=max(0.000001, remaining),
                    tasks=self._proxy_cleanup_tasks,
                    label=(
                        f"direct WebSocket {reason} close account_id={account_id_value} "
                        f"request_id={request_state.request_id if request_state is not None else 'none'}"
                    ),
                )
            except TimeoutError:
                logger.warning(
                    "Direct WebSocket close exceeded hard observation reason=%s account_id=%s request_id=%s",
                    reason,
                    account_id_value,
                    request_state.request_id if request_state is not None else None,
                )
            except Exception:
                logger.debug("Failed to close direct upstream websocket for %s", reason, exc_info=True)

        try:
            while True:
                # Clear before inspecting the queue. An append before the clear is
                # visible in the queue; an append after it sets the event and wakes
                # the reader. This avoids losing the empty -> pending transition.
                pending_changed.clear()
                receive_timeout = await self._next_websocket_receive_timeout(
                    pending_requests,
                    pending_lock=pending_lock,
                    proxy_request_budget_seconds=proxy_request_budget_seconds,
                    stream_idle_timeout_seconds=stream_idle_timeout_seconds,
                )
                try:
                    if receive_timeout is not None and receive_timeout.timeout_seconds <= 0:
                        await retire_active_receive()
                        raise asyncio.TimeoutError()
                    if receive_task is None:
                        receive_task = asyncio.create_task(upstream.receive())
                    pending_change_task = asyncio.create_task(pending_changed.wait())
                    wait_timeout = receive_timeout.timeout_seconds if receive_timeout is not None else None
                    try:
                        completed, _pending = await asyncio.wait(
                            {receive_task, pending_change_task},
                            timeout=wait_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except BaseException:
                        await stop_pending_change_wait(pending_change_task)
                        await retire_active_receive()
                        raise
                    if receive_task in completed:
                        await stop_pending_change_wait(pending_change_task)
                        completed_receive = receive_task
                        receive_task = None
                        message = completed_receive.result()
                    elif pending_change_task in completed:
                        pending_change_task.result()
                        # Preserve the one receive owner while recomputing the
                        # deadline introduced by the newly pending request.
                        continue
                    else:
                        await stop_pending_change_wait(pending_change_task)
                        await retire_active_receive()
                        raise asyncio.TimeoutError()
                except asyncio.TimeoutError:
                    if receive_timeout is None:
                        raise
                    if receive_timeout.fail_all_pending:
                        requests_to_fail = pending_requests
                        requests_to_fail_lock = pending_lock
                        if upstream_control.detached_receive_pending:
                            transferred_requests = await self._transfer_pending_websocket_request_accounting(
                                pending_requests,
                                pending_lock=pending_lock,
                                upstream_control=upstream_control,
                                api_key=api_key,
                            )
                            if transferred_requests is not None:
                                requests_to_fail = transferred_requests
                                requests_to_fail_lock = anyio.Lock()
                        await self._fail_pending_websocket_requests(
                            account_id_value=account_id_value,
                            pending_requests=requests_to_fail,
                            pending_lock=requests_to_fail_lock,
                            error_code=receive_timeout.error_code,
                            error_message=receive_timeout.error_message,
                            api_key=api_key,
                            websocket=websocket,
                            client_send_lock=client_send_lock,
                            response_create_gate=response_create_gate,
                        )
                        upstream_control.reconnect_requested = True
                        if not upstream_control.detached_receive_pending:
                            await close_upstream_after_receive()
                        break
                    retire_ambiguous_transport = await self._fail_expired_pending_websocket_requests(
                        account_id_value=account_id_value,
                        pending_requests=pending_requests,
                        pending_lock=pending_lock,
                        request_budget_seconds=proxy_request_budget_seconds,
                        error_code=receive_timeout.error_code,
                        error_message=receive_timeout.error_message,
                        api_key=api_key,
                        websocket=websocket,
                        client_send_lock=client_send_lock,
                        response_create_gate=response_create_gate,
                        upstream_control=upstream_control,
                    )
                    if retire_ambiguous_transport:
                        requests_to_fail = pending_requests
                        requests_to_fail_lock = pending_lock
                        if upstream_control.detached_receive_pending:
                            transferred_requests = await self._transfer_pending_websocket_request_accounting(
                                pending_requests,
                                pending_lock=pending_lock,
                                upstream_control=upstream_control,
                                api_key=api_key,
                            )
                            if transferred_requests is not None:
                                requests_to_fail = transferred_requests
                                requests_to_fail_lock = anyio.Lock()
                        await self._fail_pending_websocket_requests(
                            account_id_value=account_id_value,
                            pending_requests=requests_to_fail,
                            pending_lock=requests_to_fail_lock,
                            error_code="stream_incomplete",
                            error_message=(
                                "Upstream websocket retired after an unidentified request exceeded its deadline"
                            ),
                            api_key=api_key,
                            websocket=websocket,
                            client_send_lock=client_send_lock,
                            response_create_gate=response_create_gate,
                        )
                        upstream_control.reconnect_requested = True
                        if not upstream_control.detached_receive_pending:
                            await close_upstream_after_receive()
                        break
                    # A hard receive deadline retires the transport even when the
                    # expired request was identifiable. Remaining siblings cannot
                    # safely reuse a socket whose receive owner was cancelled.
                    requests_to_fail = pending_requests
                    requests_to_fail_lock = pending_lock
                    if upstream_control.detached_receive_pending:
                        transferred_requests = await self._transfer_pending_websocket_request_accounting(
                            pending_requests,
                            pending_lock=pending_lock,
                            upstream_control=upstream_control,
                            api_key=api_key,
                        )
                        if transferred_requests is not None:
                            requests_to_fail = transferred_requests
                            requests_to_fail_lock = anyio.Lock()
                    await self._fail_pending_websocket_requests(
                        account_id_value=account_id_value,
                        pending_requests=requests_to_fail,
                        pending_lock=requests_to_fail_lock,
                        error_code="stream_incomplete",
                        error_message="Upstream websocket retired after a request exceeded its deadline",
                        api_key=api_key,
                        websocket=websocket,
                        client_send_lock=client_send_lock,
                        response_create_gate=response_create_gate,
                    )
                    upstream_control.reconnect_requested = True
                    break
                if message.kind == "text" and message.text is not None:
                    downstream_activity.mark()
                    downstream_text = await self._process_upstream_websocket_text(
                        getattr(message, "raw_text", None) or message.text,
                        account=account,
                        account_id_value=account_id_value,
                        pending_requests=pending_requests,
                        pending_lock=pending_lock,
                        api_key=api_key,
                        upstream_control=upstream_control,
                        response_create_gate=response_create_gate,
                    )
                    suppress_downstream_event = upstream_control.suppress_downstream_event
                    downstream_texts = upstream_control.downstream_texts
                    upstream_control.suppress_downstream_event = False
                    upstream_control.downstream_texts = None
                    if upstream_control.retire_ambiguous_transport:
                        await self._fail_pending_websocket_requests(
                            account_id_value=account_id_value,
                            pending_requests=pending_requests,
                            pending_lock=pending_lock,
                            error_code="stream_incomplete",
                            error_message=(
                                "Upstream websocket retired after an anonymous late event could not be correlated"
                            ),
                            api_key=api_key,
                            websocket=websocket,
                            client_send_lock=client_send_lock,
                            response_create_gate=response_create_gate,
                            downstream_activity=downstream_activity,
                        )
                        upstream_control.reconnect_requested = True
                        await close_upstream_for_reconnect(None, reason="ambiguous-event retirement")
                        break
                    if downstream_texts is not None:
                        for emitted_text in downstream_texts:
                            await self._send_downstream_websocket_text(
                                websocket,
                                client_send_lock=client_send_lock,
                                text=emitted_text,
                                downstream_activity=downstream_activity,
                            )
                    elif not suppress_downstream_event:
                        await self._send_downstream_websocket_text(
                            websocket,
                            client_send_lock=client_send_lock,
                            text=downstream_text,
                            downstream_activity=downstream_activity,
                        )
                    if upstream_control.reconnect_requested:
                        should_reconnect = upstream_control.replay_request_state is not None
                        if not should_reconnect:
                            async with pending_lock:
                                should_reconnect = not pending_requests
                        if should_reconnect:
                            await close_upstream_for_reconnect(
                                upstream_control.replay_request_state,
                                reason="terminal replay",
                            )
                            break
                    continue
                if message.kind == "binary" and message.data is not None:
                    downstream_activity.mark()
                    await self._send_downstream_websocket_bytes(
                        websocket,
                        client_send_lock=client_send_lock,
                        data=message.data,
                        downstream_activity=downstream_activity,
                    )
                    continue
                replay_request_state = await _pop_replayable_precreated_websocket_request_state(
                    pending_requests,
                    pending_lock=pending_lock,
                )
                if replay_request_state is not None:
                    upstream_control.reconnect_requested = True
                    upstream_control.replay_request_state = replay_request_state
                    logger.info(
                        "Transparent websocket replay after upstream close request_id=%s close_code=%s",
                        replay_request_state.request_log_id or replay_request_state.request_id,
                        message.close_code,
                    )
                    await close_upstream_for_reconnect(
                        replay_request_state,
                        reason="pre-created close replay",
                    )
                    break
                await self._fail_pending_websocket_requests(
                    account_id_value=account_id_value,
                    pending_requests=pending_requests,
                    pending_lock=pending_lock,
                    error_code="stream_incomplete",
                    error_message=_upstream_websocket_disconnect_message(message),
                    api_key=api_key,
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    response_create_gate=response_create_gate,
                    downstream_activity=downstream_activity,
                )
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Upstream websocket reader crashed account_id=%s",
                account_id_value,
                exc_info=True,
            )
            await self._fail_pending_websocket_requests(
                account_id_value=account_id_value,
                pending_requests=pending_requests,
                pending_lock=pending_lock,
                error_code="stream_incomplete",
                error_message="Upstream websocket reader crashed before response.completed",
                api_key=api_key,
                websocket=websocket,
                client_send_lock=client_send_lock,
                response_create_gate=response_create_gate,
                downstream_activity=downstream_activity,
            )
        finally:
            if receive_task is not None:
                await retire_active_receive()
            if not upstream_control.detached_receive_pending:
                await drain_discarded_request_accounting()
            async with pending_lock:
                has_pending_requests = bool(pending_requests)
            if not upstream_control.reconnect_requested and has_pending_requests:
                try:
                    await websocket.close()
                except Exception:
                    logger.debug("Failed to close downstream websocket", exc_info=True)

    async def _process_upstream_websocket_text(
        self: _WebSocketRelayService,
        text: str,
        *,
        account: Account,
        account_id_value: str,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        api_key: ApiKeyData | None,
        upstream_control: _WebSocketUpstreamControl,
        response_create_gate: asyncio.Semaphore | None,
    ) -> str:
        event_block = f"data: {text}\n\n"
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
        terminal_event = event_type in {
            "response.completed",
            "response.failed",
            "response.incomplete",
            "error",
        }

        async with pending_lock:
            if response_id is not None and response_id in upstream_control.discarded_response_ids:
                if event_type in {"response.completed", "response.failed", "response.incomplete", "error"}:
                    upstream_control.discarded_response_ids.discard(response_id)
                    discarded_accounting = upstream_control.discarded_request_accounting.pop(
                        response_id,
                        None,
                    )
                    if discarded_accounting is not None:
                        discarded_accounting.request_state.discarded_accounting_resolution_future = (
                            discarded_accounting.resolution_future
                        )
                        self._schedule_websocket_request_finalization(
                            discarded_accounting.request_state,
                            account=account,
                            account_id_value=account_id_value,
                            event=event,
                            event_type=event_type,
                            payload=payload,
                            accounting_event=accounting_event,
                            accounting_event_type=accounting_event_type,
                            accounting_payload=accounting_payload,
                            api_key=discarded_accounting.request_state.api_key or api_key,
                            api_key_reservation=discarded_accounting.api_key_reservation,
                            reservation_preclaimed=True,
                            upstream_control=upstream_control,
                            response_create_gate=response_create_gate,
                        )
                upstream_control.suppress_downstream_event = True
                return text

            anonymous_discarded = upstream_control.anonymous_discarded_request_accounting
            if (
                response_id is not None
                and response_id not in upstream_control.discarded_response_ids
                and len(anonymous_discarded) == 1
                and (event_type == "response.created" or terminal_event)
            ):
                anonymous_key, discarded_accounting = next(iter(anonymous_discarded.items()))
                anonymous_discarded.pop(anonymous_key, None)
                discarded_accounting.request_state.response_id = response_id
                if event_type == "response.created":
                    upstream_control.discarded_response_ids.add(response_id)
                    upstream_control.discarded_request_accounting[response_id] = discarded_accounting
                else:
                    discarded_accounting.request_state.discarded_accounting_resolution_future = (
                        discarded_accounting.resolution_future
                    )
                    self._schedule_websocket_request_finalization(
                        discarded_accounting.request_state,
                        account=account,
                        account_id_value=account_id_value,
                        event=event,
                        event_type=event_type,
                        payload=payload,
                        accounting_event=accounting_event,
                        accounting_event_type=accounting_event_type,
                        accounting_payload=accounting_payload,
                        api_key=discarded_accounting.request_state.api_key or api_key,
                        api_key_reservation=discarded_accounting.api_key_reservation,
                        reservation_preclaimed=True,
                        upstream_control=upstream_control,
                        response_create_gate=response_create_gate,
                    )
                upstream_control.suppress_downstream_event = True
                return text

            discarded_count = len(upstream_control.discarded_request_accounting) + len(
                anonymous_discarded
            )
            anonymous_terminal_candidate_count = _anonymous_terminal_candidate_count(
                pending_requests,
                discarded_response_ids=upstream_control.discarded_response_ids,
                discarded_request_accounting=upstream_control.discarded_request_accounting,
                anonymous_discarded_request_accounting=anonymous_discarded,
            )
            if (
                response_id is None
                and terminal_event
                and anonymous_terminal_candidate_count == 1
                and discarded_count == 1
                and not upstream_control.discarded_response_ids.difference(
                    upstream_control.discarded_request_accounting
                )
            ):
                if upstream_control.discarded_request_accounting:
                    discarded_response_id, discarded_accounting = next(
                        iter(upstream_control.discarded_request_accounting.items())
                    )
                    upstream_control.discarded_request_accounting.pop(discarded_response_id, None)
                    upstream_control.discarded_response_ids.discard(discarded_response_id)
                else:
                    anonymous_key, discarded_accounting = next(iter(anonymous_discarded.items()))
                    anonymous_discarded.pop(anonymous_key, None)
                discarded_accounting.request_state.discarded_accounting_resolution_future = (
                    discarded_accounting.resolution_future
                )
                self._schedule_websocket_request_finalization(
                    discarded_accounting.request_state,
                    account=account,
                    account_id_value=account_id_value,
                    event=event,
                    event_type=event_type,
                    payload=payload,
                    accounting_event=accounting_event,
                    accounting_event_type=accounting_event_type,
                    accounting_payload=accounting_payload,
                    api_key=discarded_accounting.request_state.api_key or api_key,
                    api_key_reservation=discarded_accounting.api_key_reservation,
                    reservation_preclaimed=True,
                    upstream_control=upstream_control,
                    response_create_gate=response_create_gate,
                )
                upstream_control.suppress_downstream_event = True
                return text

            if (
                response_id is None
                and terminal_event
                and not is_previous_response_not_found_event
                and anonymous_terminal_candidate_count != 1
            ):
                upstream_control.retire_ambiguous_transport = True
                upstream_control.suppress_downstream_event = True
                return text

            if response_id is None and (
                discarded_count or upstream_control.discarded_response_ids
            ):
                upstream_control.retire_ambiguous_transport = True
                upstream_control.suppress_downstream_event = True
                return text

            request_state = None
            created_request_state = None
            has_other_pending_requests = False
            grouped_previous_response_request_states: list[_WebSocketRequestState] = []
            if event_type == "response.created":
                request_state = _assign_websocket_response_id(pending_requests, response_id)
                created_request_state = request_state
                release_create_gate = request_state is not None
            elif response_id is not None:
                request_state = _find_websocket_request_state_by_response_id(pending_requests, response_id)
                release_create_gate = False
            elif response_id is None:
                request_state = _match_websocket_request_state_for_anonymous_event(
                    pending_requests,
                    prefer_previous_response_not_found=is_previous_response_not_found_event,
                    previous_response_id_hint=previous_response_id_hint,
                    error_message=error_message,
                )
                release_create_gate = False
            else:
                release_create_gate = False
            if request_state is not None:
                actual_service_tier = _service_tier_from_event_payload(payload)
                if actual_service_tier is not None:
                    request_state.actual_service_tier = actual_service_tier
                    request_state.service_tier = actual_service_tier
            if (
                event_type in {"response.completed", "response.failed", "response.incomplete", "error"}
                and pending_requests
            ):
                request_state = _pop_terminal_websocket_request_state(
                    pending_requests,
                    response_id=response_id,
                    fallback_request_state=request_state,
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
                if request_state is None and is_previous_response_not_found_event:
                    grouped_previous_response_request_states = _pop_matching_websocket_request_states(
                        pending_requests,
                        _matching_websocket_request_states_for_previous_response_error(
                            pending_requests,
                            previous_response_id_hint=previous_response_id_hint,
                            error_message=error_message,
                        ),
                    )
                has_other_pending_requests = bool(pending_requests)
            else:
                request_state = None

        if event_type == "response.created" and release_create_gate and created_request_state is not None:
            _release_websocket_response_create_gate(created_request_state, response_create_gate)

        if len(grouped_previous_response_request_states) > 1:
            upstream_control.reconnect_requested = True
            downstream_texts: list[str] = []
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
            for grouped_request_state in grouped_previous_response_request_states:
                (
                    grouped_downstream_text,
                    _grouped_event_block,
                    grouped_event,
                    grouped_payload,
                    grouped_event_type,
                ) = _build_stream_incomplete_terminal_event_for_request(grouped_request_state)
                downstream_texts.append(grouped_downstream_text)
                self._schedule_websocket_request_finalization(
                    grouped_request_state,
                    account=account,
                    account_id_value=account_id_value,
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
                    api_key=api_key,
                    upstream_control=upstream_control,
                    response_create_gate=response_create_gate,
                )
            await asyncio.sleep(0)
            upstream_control.suppress_downstream_event = True
            upstream_control.downstream_texts = downstream_texts
            return downstream_texts[0]

        if len(grouped_previous_response_request_states) == 1 and request_state is None:
            request_state = grouped_previous_response_request_states[0]

        if request_state is None:
            if is_previous_response_not_found_event:
                upstream_control.suppress_downstream_event = True
                return text
            return sanitize_upstream_websocket_event_text(text)

        retry_is_previous_response_not_found = is_previous_response_not_found_event
        retry_error_code = _websocket_precreated_retry_error_code(
            request_state,
            event_type=event_type,
            payload=payload,
            has_other_pending_requests=has_other_pending_requests,
        )
        upstream_retry_event = event
        upstream_retry_payload = payload
        upstream_retry_event_type = event_type
        event, payload, event_type, downstream_text = (
            self._maybe_rewrite_websocket_previous_response_not_found_event_compatible(
                request_state=request_state,
                event=event,
                payload=payload,
                event_type=event_type,
                upstream_control=upstream_control,
                original_text=text,
            )
        )
        if retry_error_code is None:
            retry_error_code = _websocket_precreated_retry_error_code(
                request_state,
                event_type=event_type,
                payload=payload,
                has_other_pending_requests=has_other_pending_requests,
            )
        if (
            retry_error_code in _WEBSOCKET_TRANSPARENT_REPLAY_ERROR_CODES
            and request_state.previous_response_id is not None
            and request_state.preferred_account_id is not None
        ):
            self._classify_and_schedule_stream_error(
                account,
                {"message": _websocket_event_error_message(event_type, payload) or "Upstream error"},
                retry_error_code,
            )
            event, payload, event_type, downstream_text = (
                self._rewrite_websocket_previous_response_owner_unavailable_event_compatible(
                    request_state=request_state,
                )
            )
            retry_error_code = None
        if retry_error_code is not None:
            self._capture_websocket_retry_usage(
                request_state,
                account_id_value=account_id_value,
                event=upstream_retry_event,
                event_type=upstream_retry_event_type,
                payload=upstream_retry_payload,
                api_key=api_key,
                service_tier=(
                    _service_tier_from_event_payload(upstream_retry_payload)
                    or request_state.actual_service_tier
                    or request_state.service_tier
                ),
                requested_service_tier=request_state.requested_service_tier,
            )
            upstream_control.reconnect_requested = True
            if retry_is_previous_response_not_found:
                request_state.replay_count += 1
                request_state.awaiting_response_created = True
                request_state.response_id = None
                upstream_control.suppress_downstream_event = True
                upstream_control.replay_request_state = request_state
            else:
                request_state.replay_count += 1
                request_state.awaiting_response_created = True
                request_state.response_id = None
                upstream_control.suppress_downstream_event = True
                upstream_control.replay_request_state = request_state
                self._classify_and_schedule_stream_error(
                    account,
                    {"message": _websocket_event_error_message(event_type, payload) or "Upstream error"},
                    retry_error_code,
                )
            return downstream_text

        self._schedule_websocket_request_finalization(
            request_state,
            account=account,
            account_id_value=account_id_value,
            event=event,
            event_type=event_type,
            payload=payload,
            accounting_event=accounting_event,
            accounting_event_type=accounting_event_type,
            accounting_payload=accounting_payload,
            api_key=api_key,
            upstream_control=upstream_control,
            response_create_gate=response_create_gate,
        )
        await asyncio.sleep(0)
        if downstream_text != text:
            # Proxy-generated terminal events already contain the deliberate
            # public contract message. Only unchanged provider frames need the
            # provider-error sanitizer at this final delivery boundary.
            return downstream_text
        return sanitize_upstream_websocket_event_text(downstream_text)

    async def _next_websocket_receive_timeout(
        self: _WebSocketRelayService,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        proxy_request_budget_seconds: float,
        stream_idle_timeout_seconds: float,
        response_created_timeout_seconds: float | None = None,
    ) -> _WebSocketReceiveTimeout | None:
        async with pending_lock:
            requests = list(pending_requests)
            response_created_deadlines = [
                (
                    request_state.http_bridge_send_completed_at + response_created_timeout_seconds,
                    request_state.request_id,
                    request_state.http_bridge_send_completed_at,
                )
                for request_state in pending_requests
                if response_created_timeout_seconds is not None
                and request_state.http_bridge_send_completed_at is not None
                and request_state.response_id is None
                and request_state.awaiting_response_created
            ]
        receive_timeout = _websocket_receive_timeout_for_pending_requests(
            requests,
            proxy_request_budget_seconds=proxy_request_budget_seconds,
            stream_idle_timeout_seconds=stream_idle_timeout_seconds,
        )
        if not response_created_deadlines:
            return receive_timeout

        next_deadline = min(deadline for deadline, _request_id, _sent_at in response_created_deadlines)
        selected_requests = tuple(
            (request_id, sent_at)
            for deadline, request_id, sent_at in response_created_deadlines
            if deadline == next_deadline
        )
        startup_timeout = _WebSocketReceiveTimeout(
            timeout_seconds=max(0.0, next_deadline - time.monotonic()),
            error_code="response_created_timeout",
            error_message="Upstream did not create a response within the startup window",
            response_created_request_ids=frozenset(request_id for request_id, _sent_at in selected_requests),
            response_created_request_tokens=frozenset(selected_requests),
        )
        if receive_timeout is None or startup_timeout.timeout_seconds < receive_timeout.timeout_seconds:
            return startup_timeout
        return receive_timeout

    async def _downstream_websocket_is_idle(
        self: _WebSocketRelayService,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        downstream_activity: _DownstreamWebSocketActivity,
        idle_timeout_seconds: float,
    ) -> bool:
        async with pending_lock:
            if pending_requests:
                return False
        return (time.monotonic() - downstream_activity.last_activity_at) >= idle_timeout_seconds

    async def _fail_expired_pending_websocket_requests(
        self: _WebSocketRelayService,
        *,
        account_id_value: str | None,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        request_budget_seconds: float,
        error_code: str,
        error_message: str,
        api_key: ApiKeyData | None,
        websocket: WebSocket | None = None,
        client_send_lock: anyio.Lock | None = None,
        response_create_gate: asyncio.Semaphore | None = None,
        upstream_control: _WebSocketUpstreamControl,
    ) -> bool:
        now = time.monotonic()
        with anyio.CancelScope(shield=True):
            async with pending_lock:
                expired_requests = [
                    request_state
                    for request_state in list(pending_requests)
                    if now >= _request_deadline_at(request_state, request_budget_seconds)
                ]
                retire_ambiguous_transport = any(
                    request_state.response_id is None for request_state in expired_requests
                )
                if retire_ambiguous_transport:
                    upstream_control.retire_ambiguous_transport = True
                for request_state in expired_requests:
                    pending_requests.remove(request_state)
                    resolution_future = request_state.discarded_accounting_resolution_future
                    request_state.discarded_accounting_resolution_future = None
                    existing_accounting = (
                        upstream_control.discarded_request_accounting.get(
                            request_state.response_id
                        )
                        if request_state.response_id is not None
                        else upstream_control.anonymous_discarded_request_accounting.get(
                            request_state.request_id
                        )
                    )
                    if existing_accounting is not None:
                        if existing_accounting.resolution_future is None:
                            existing_accounting.resolution_future = resolution_future
                        request_state.api_key_reservation = None
                        continue
                    discarded_accounting = _DiscardedRequestAccounting(
                        request_state=request_state,
                        api_key_reservation=request_state.api_key_reservation,
                        resolution_future=resolution_future,
                    )
                    self._enroll_discarded_accounting_guardian(
                        discarded_accounting,
                        api_key=request_state.api_key or api_key,
                        error_message=(
                            "Upstream WebSocket closed before discarded response completed"
                        ),
                    )
                    if request_state.response_id is not None:
                        upstream_control.discarded_response_ids.add(request_state.response_id)
                        upstream_control.discarded_request_accounting[request_state.response_id] = (
                            discarded_accounting
                        )
                    else:
                        upstream_control.anonymous_discarded_request_accounting[
                            request_state.request_id
                        ] = discarded_accounting
                    request_state.api_key_reservation = None
            if not expired_requests:
                return False
            await _await_shielded_cleanup(
                self._fail_pending_websocket_requests(
                    account_id_value=account_id_value,
                    pending_requests=deque(expired_requests),
                    pending_lock=anyio.Lock(),
                    error_code=error_code,
                    error_message=error_message,
                    api_key=api_key,
                    websocket=websocket,
                    client_send_lock=client_send_lock,
                    response_create_gate=response_create_gate,
                ),
                label="expired websocket request cleanup",
            )
            return retire_ambiguous_transport

    async def _transfer_pending_websocket_request_accounting(
        self: _WebSocketRelayService,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        upstream_control: _WebSocketUpstreamControl,
        api_key: ApiKeyData | None = None,
    ) -> deque[_WebSocketRequestState] | None:
        async with pending_lock:
            if upstream_control.discarded_accounting_sealed:
                return None
            transferred = deque(pending_requests)
            pending_requests.clear()
            for request_state in transferred:
                resolution_future = request_state.discarded_accounting_resolution_future
                request_state.discarded_accounting_resolution_future = None
                existing_accounting = (
                    upstream_control.discarded_request_accounting.get(
                        request_state.response_id
                    )
                    if request_state.response_id is not None
                    else upstream_control.anonymous_discarded_request_accounting.get(
                        request_state.request_id
                    )
                )
                if existing_accounting is not None:
                    if existing_accounting.resolution_future is None:
                        existing_accounting.resolution_future = resolution_future
                    request_state.api_key_reservation = None
                    continue
                discarded_accounting = _DiscardedRequestAccounting(
                    request_state=request_state,
                    api_key_reservation=request_state.api_key_reservation,
                    resolution_future=resolution_future,
                )
                self._enroll_discarded_accounting_guardian(
                    discarded_accounting,
                    api_key=request_state.api_key or api_key,
                    error_message=(
                        "Upstream WebSocket closed before discarded response completed"
                    ),
                )
                if request_state.response_id is not None:
                    upstream_control.discarded_response_ids.add(request_state.response_id)
                    upstream_control.discarded_request_accounting.setdefault(
                        request_state.response_id,
                        discarded_accounting,
                    )
                else:
                    upstream_control.anonymous_discarded_request_accounting.setdefault(
                        request_state.request_id,
                        discarded_accounting,
                    )
                request_state.api_key_reservation = None
            return transferred

    def _capture_websocket_retry_usage(
        self: _WebSocketRelayService,
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
    ) -> None:
        if event_type not in {"response.failed", "response.incomplete"}:
            return
        response = event.response if event is not None else None
        usage = response.usage if response is not None else None
        charge = _usage_charge_from_response_usage(
            usage,
            model=request_state.model or "",
            service_tier=service_tier,
        )
        if charge is None:
            return
        if append_usage_charge:
            request_state.usage_charges.append(charge)
        if not schedule_request_log or request_state.skip_request_log:
            return

        response_id = response.id if response is not None and response.id else request_state.response_id
        response_id = response_id or request_state.request_log_id or request_state.request_id
        error = response.error if response is not None else None
        error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
        error_message = error.message if error else None
        details = usage.input_tokens_details if usage is not None else None
        output_details = usage.output_tokens_details if usage is not None else None
        retry_model = request_state.model or ""
        retry_started_at = request_state.started_at
        retry_reasoning_effort = request_state.reasoning_effort
        retry_transport = request_state.transport
        retry_requested_service_tier = requested_service_tier
        retry_latency_first_token_ms = request_state.latency_first_token_ms
        retry_session_id = request_state.session_id

        async def write_retry_attempt_log() -> None:
            await self._write_websocket_log_before_deadline(
                request_state,
                lambda: self._write_request_log(
                    account_id=account_id_value,
                    api_key=api_key,
                    request_id=response_id,
                    model=retry_model,
                    latency_ms=int((time.monotonic() - retry_started_at) * 1000),
                    status="error",
                    error_code=error_code,
                    error_message=error_message,
                    input_tokens=usage.input_tokens if usage is not None else None,
                    output_tokens=usage.output_tokens if usage is not None else None,
                    cached_input_tokens=details.cached_tokens if details is not None else None,
                    cache_write_tokens=details.cache_write_tokens if details is not None else None,
                    reasoning_tokens=output_details.reasoning_tokens if output_details is not None else None,
                    reasoning_effort=retry_reasoning_effort,
                    transport=retry_transport,
                    service_tier=service_tier,
                    requested_service_tier=retry_requested_service_tier,
                    actual_service_tier=service_tier,
                    latency_first_token_ms=retry_latency_first_token_ms,
                    session_id=retry_session_id,
                ),
                label=f"websocket retry attempt request log request_id={response_id}",
            )

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            write_retry_attempt_log(),
            name=f"websocket-retry-log-{response_id}-{time.monotonic_ns()}",
            label=f"websocket retry attempt request log request_id={response_id}",
        )

    async def _finalize_websocket_request_state(
        self: _WebSocketRelayService,
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
    ) -> None:
        status = "success"
        error_code = None
        error_message = None
        usage = None
        error_payload: UpstreamError | None = None
        response_id = request_state.response_id or request_state.request_id
        response_service_tier = request_state.service_tier

        if event_type == "error":
            status = "error"
            error = event.error if event else None
            error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
            error_message = error.message if error else None
            error_payload = _upstream_error_from_openai(error)
        elif event_type in {"response.failed", "response.incomplete"}:
            status = "error"
            error = event.response.error if event and event.response else None
            error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
            error_message = error.message if error else None
            if event_type == "response.failed":
                error_payload = _upstream_error_from_openai(error)
            usage = event.response.usage if event and event.response else None
            if event and event.response and event.response.id:
                response_id = event.response.id
        elif event_type == "response.completed":
            usage = event.response.usage if event and event.response else None
            if event and event.response and event.response.id:
                response_id = event.response.id

        accounting_response = accounting_event.response if accounting_event is not None else None
        accounting_usage = accounting_response.usage if accounting_response is not None else None
        if accounting_usage is not None:
            usage = accounting_usage

        actual_service_tier = _service_tier_from_event_payload(accounting_payload)
        if actual_service_tier is None:
            actual_service_tier = _service_tier_from_event_payload(payload)
        if actual_service_tier is not None:
            request_state.actual_service_tier = actual_service_tier
            response_service_tier = actual_service_tier

        settlement = _StreamSettlement(
            status=status,
            model=request_state.model or "",
            service_tier=response_service_tier,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cached_input_tokens=(
                usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else None
            ),
            cache_write_tokens=(
                usage.input_tokens_details.cache_write_tokens if usage and usage.input_tokens_details else None
            ),
            error_code=error_code,
            error_message=error_message,
            error=error_payload,
        )
        usage_charges = list(request_state.usage_charges)
        current_charge = _usage_charge_from_response_usage(
            usage,
            model=request_state.model or "",
            service_tier=response_service_tier,
        )
        if current_charge is not None:
            usage_charges.append(current_charge)
        _apply_usage_charges_to_settlement(settlement, usage_charges)
        if event_type in {"response.failed", "response.incomplete", "error"}:
            settlement.record_success = False
        if event_type in {"response.failed", "error"} and not stream_error_already_classified:
            settlement.account_health_error = _should_penalize_stream_error(error_code)
        _release_websocket_response_create_gate(request_state, response_create_gate)
        self._release_request_account_model_concurrency(request_state)
        settlement_finished = False
        try:
            await self._settle_stream_api_key_usage_with_fallback(
                api_key,
                api_key_reservation,
                settlement,
                response_id,
            )
            settlement_finished = True
        finally:
            if not settlement_finished:
                if _stream_settlement_has_authoritative_usage(settlement):
                    logger.error(
                        "Authoritative WebSocket settlement interrupted; reservation retained "
                        "request_id=%s",
                        request_state.request_log_id or request_state.request_id,
                    )
                else:
                    self._schedule_websocket_reservation_release(
                        api_key_reservation,
                        reason=f"terminal-finalization-failed-{request_state.request_id}",
                    )
        if settlement.account_health_error:
            await self._handle_stream_error(
                account,
                _stream_settlement_error_payload(settlement),
                settlement.error_code or "upstream_error",
            )
            upstream_control.reconnect_requested = True
        elif settlement.record_success:
            await self._load_balancer.record_success(account)
            self._remember_websocket_previous_response_owner(
                previous_response_id=response_id,
                api_key_id=api_key.id if api_key is not None else None,
                account_id=account_id_value,
                session_id=request_state.session_id,
            )

        latency_ms = int((time.monotonic() - request_state.started_at) * 1000)
        cached_input_tokens = usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else None
        cache_write_tokens = (
            usage.input_tokens_details.cache_write_tokens if usage and usage.input_tokens_details else None
        )
        reasoning_tokens = (
            usage.output_tokens_details.reasoning_tokens if usage and usage.output_tokens_details else None
        )
        if not request_state.skip_request_log:
            await self._write_websocket_log_before_deadline(
                request_state,
                lambda: self._write_request_log(
                    account_id=account_id_value,
                    api_key=api_key,
                    request_id=response_id,
                    model=request_state.model or "",
                    latency_ms=latency_ms,
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    cached_input_tokens=cached_input_tokens,
                    cache_write_tokens=cache_write_tokens,
                    reasoning_tokens=reasoning_tokens,
                    reasoning_effort=request_state.reasoning_effort,
                    transport=request_state.transport,
                    service_tier=response_service_tier,
                    requested_service_tier=request_state.requested_service_tier,
                    actual_service_tier=request_state.actual_service_tier,
                    latency_first_token_ms=request_state.latency_first_token_ms,
                    session_id=request_state.session_id,
                ),
                label=f"websocket terminal request log request_id={response_id}",
            )

    def _schedule_websocket_request_finalization(
        self: _WebSocketRelayService,
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
    ) -> None:
        _release_websocket_response_create_gate(request_state, response_create_gate)
        self._release_request_account_model_concurrency(request_state)
        owned_api_key_reservation = api_key_reservation
        if not reservation_preclaimed:
            owned_api_key_reservation = request_state.api_key_reservation
            request_state.api_key_reservation = None

        error_code: str | None = None
        if event_type == "error":
            error = event.error if event else None
            error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
        elif event_type == "response.failed":
            error = event.response.error if event and event.response else None
            error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
        if _should_penalize_stream_error(error_code):
            upstream_control.reconnect_requested = True

        async def finalize_owned_request() -> None:
            await self._finalize_websocket_request_state(
                request_state,
                account=account,
                account_id_value=account_id_value,
                event=event,
                event_type=event_type,
                payload=payload,
                accounting_event=accounting_event,
                accounting_payload=accounting_payload,
                stream_error_already_classified=stream_error_already_classified,
                api_key=api_key,
                api_key_reservation=owned_api_key_reservation,
                upstream_control=upstream_control,
                response_create_gate=response_create_gate,
            )

        resolution_future = request_state.discarded_accounting_resolution_future
        request_state.discarded_accounting_resolution_future = None
        if resolution_future is not None:
            if not resolution_future.done():
                resolution_future.set_result(finalize_owned_request)
            return

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            finalize_owned_request(),
            name=f"websocket-finalize-{request_state.request_id}",
            label=f"websocket request finalization request_id={request_state.request_id}",
        )

    async def _write_websocket_connect_failure(
        self: _WebSocketRelayService,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
    ) -> None:
        if request_state.skip_request_log:
            return
        await self._write_websocket_log_before_deadline(
            request_state,
            lambda: self._write_request_log(
                account_id=account_id,
                api_key=api_key,
                request_id=request_state.request_log_id or request_state.request_id,
                model=request_state.model or "",
                latency_ms=int((time.monotonic() - request_state.started_at) * 1000),
                status="error",
                error_code=error_code,
                error_message=error_message,
                reasoning_effort=request_state.reasoning_effort,
                transport=request_state.transport,
                service_tier=request_state.service_tier,
                requested_service_tier=request_state.requested_service_tier,
                actual_service_tier=request_state.actual_service_tier,
                latency_first_token_ms=request_state.latency_first_token_ms,
                session_id=request_state.session_id,
            ),
            label=f"websocket connect failure log request_id={request_state.request_id}",
        )

    async def _settle_or_release_failed_websocket_reservation(
        self: _WebSocketRelayService,
        *,
        request_state: _WebSocketRequestState,
        reservation: ApiKeyUsageReservationData | None,
        api_key: ApiKeyData | None,
        error_code: str,
        error_message: str,
    ) -> None:
        if reservation is None:
            return
        usage_charges = tuple(request_state.usage_charges)
        if not usage_charges:
            await self._release_websocket_reservation_with_retry(
                reservation,
                reason=f"failed-request-{request_state.request_id}",
            )
            return

        settlement = _StreamSettlement(
            status="error",
            model=request_state.model or reservation.model or "",
            service_tier=request_state.actual_service_tier or request_state.service_tier,
            error_code=error_code,
            error_message=error_message,
            error={"message": error_message},
            record_success=False,
        )
        _apply_usage_charges_to_settlement(settlement, usage_charges)
        await self._settle_stream_api_key_usage_with_fallback(
            request_state.api_key or api_key,
            reservation,
            settlement,
            request_state.response_id or request_state.request_log_id or request_state.request_id,
        )

    def _schedule_websocket_terminal_cleanup(
        self: _WebSocketRelayService,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
    ) -> None:
        reservation = request_state.api_key_reservation
        request_state.api_key_reservation = None

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            self._settle_or_release_failed_websocket_reservation(
                request_state=request_state,
                reservation=reservation,
                api_key=api_key,
                error_code=error_code,
                error_message=error_message,
            ),
            name=f"websocket-terminal-settlement-{request_state.request_id}",
            label=f"websocket terminal settlement request_id={request_state.request_id}",
        )
        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            self._write_websocket_connect_failure(
                account_id=account_id,
                api_key=api_key,
                request_state=request_state,
                error_code=error_code,
                error_message=error_message,
            ),
            name=f"websocket-terminal-log-{request_state.request_id}",
            label=f"websocket terminal log request_id={request_state.request_id}",
        )

    async def _emit_websocket_connect_failure(
        self: _WebSocketRelayService,
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
    ) -> None:
        status_code, payload, error_code, error_message = self._sanitize_websocket_connect_failure_compatible(
            request_state=request_state,
            status_code=status_code,
            payload=payload,
            error_code=error_code,
            error_message=error_message,
        )
        self._schedule_websocket_terminal_cleanup(
            account_id=account_id,
            api_key=api_key,
            request_state=request_state,
            error_code=error_code,
            error_message=error_message,
        )
        response_create_gate = request_state.response_create_gate
        if (
            response_create_gate is not None
            or request_state.response_create_admission is not None
            or request_state.awaiting_response_created
        ):
            _release_websocket_response_create_gate(request_state, response_create_gate)
        async with client_send_lock:
            await websocket.send_text(
                _serialize_websocket_error_event(_wrapped_websocket_error_event(status_code, payload))
            )

    async def _emit_websocket_proxy_request_timeout(
        self: _WebSocketRelayService,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
    ) -> None:
        await self._emit_websocket_connect_failure(
            websocket,
            client_send_lock=client_send_lock,
            account_id=account_id,
            api_key=api_key,
            request_state=request_state,
            status_code=502,
            payload=openai_error(
                "upstream_request_timeout",
                "Proxy request budget exhausted",
                error_type="server_error",
            ),
            error_code="upstream_request_timeout",
            error_message="Proxy request budget exhausted",
        )

    async def _fail_pending_websocket_requests(
        self: _WebSocketRelayService,
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
    ) -> None:
        async with pending_lock:
            remaining = list(pending_requests)
            pending_requests.clear()

        async def persist_removed_request(
            request_state: _WebSocketRequestState,
            reservation: ApiKeyUsageReservationData | None,
            *,
            request_error_code: str,
            request_error_message: str,
        ) -> None:
            if reservation is not None:
                try:
                    await self._settle_or_release_failed_websocket_reservation(
                        request_state=request_state,
                        reservation=reservation,
                        api_key=api_key,
                        error_code=request_error_code,
                        error_message=request_error_message,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist removed websocket request reservation request_id=%s",
                        request_state.request_log_id or request_state.request_id,
                    )
            if account_id_value is None or request_state.skip_request_log:
                return
            latency_ms = int((time.monotonic() - request_state.started_at) * 1000)
            await self._write_websocket_log_before_deadline(
                request_state,
                lambda: self._write_request_log(
                    account_id=account_id_value,
                    api_key=api_key,
                    request_id=request_state.response_id or request_state.request_log_id or request_state.request_id,
                    model=request_state.model or "",
                    latency_ms=latency_ms,
                    status="error",
                    error_code=request_error_code,
                    error_message=request_error_message,
                    reasoning_effort=request_state.reasoning_effort,
                    transport=request_state.transport,
                    service_tier=request_state.service_tier,
                    requested_service_tier=request_state.requested_service_tier,
                    actual_service_tier=request_state.actual_service_tier,
                    latency_first_token_ms=request_state.latency_first_token_ms,
                    session_id=request_state.session_id,
                ),
                label=f"removed websocket request log request_id={request_state.request_id}",
            )

        async def deliver_removed_requests() -> None:
            last_index = len(remaining) - 1
            for index, request_state in enumerate(remaining):
                request_error_code = request_state.error_code_override or error_code
                request_error_message = request_state.error_message_override or error_message
                request_error_type = request_state.error_type_override or "server_error"
                request_error_param = request_state.error_param_override
                reservation = request_state.api_key_reservation
                request_state.api_key_reservation = None
                if reservation is not None or (account_id_value is not None and not request_state.skip_request_log):
                    _schedule_tracked_background_task(
                        self._proxy_cleanup_tasks,
                        persist_removed_request(
                            request_state,
                            reservation,
                            request_error_code=request_error_code,
                            request_error_message=request_error_message,
                        ),
                        name=f"websocket-failure-{request_state.request_id}",
                        label=f"websocket failure persistence request_id={request_state.request_id}",
                    )
                try:
                    if index == last_index:
                        self._maybe_dump_oversized_response_create_request_compatible(
                            request_state,
                            account_id_value=account_id_value,
                            error_code=request_error_code,
                            error_message=request_error_message,
                        )
                    state_response_create_gate = response_create_gate or request_state.response_create_gate
                    if (
                        state_response_create_gate is not None
                        or request_state.response_create_admission is not None
                        or request_state.awaiting_response_created
                    ):
                        _release_websocket_response_create_gate(request_state, state_response_create_gate)
                    self._release_request_account_model_concurrency(request_state)
                    if request_state.event_queue is not None:
                        await request_state.event_queue.put(
                            format_sse_event(
                                response_failed_event(
                                    request_error_code,
                                    request_error_message,
                                    error_type=request_error_type,
                                    response_id=request_state.response_id or request_state.request_id,
                                    error_param=request_error_param,
                                )
                            )
                        )
                        await request_state.event_queue.put(None)
                    if websocket is not None and client_send_lock is not None:
                        await self._emit_websocket_terminal_error(
                            websocket,
                            client_send_lock=client_send_lock,
                            request_state=request_state,
                            error_code=request_error_code,
                            error_message=request_error_message,
                            error_type=request_error_type,
                            error_param=request_error_param,
                            downstream_activity=downstream_activity,
                        )
                except Exception:
                    logger.exception(
                        "Failed to deliver removed websocket request terminal event request_id=%s",
                        request_state.request_log_id or request_state.request_id,
                    )
                finally:
                    _release_websocket_response_create_gate(
                        request_state,
                        response_create_gate or request_state.response_create_gate,
                    )
                    self._release_request_account_model_concurrency(request_state)

        await _await_shielded_cleanup(
            deliver_removed_requests(),
            label="pending websocket request batch terminal delivery",
        )

    async def _emit_websocket_terminal_error(
        self: _WebSocketRelayService,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
        error_type: str = "server_error",
        error_param: str | None = None,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None:
        event = response_failed_event(
            error_code,
            error_message,
            error_type=error_type,
            response_id=request_state.response_id or request_state.request_id,
            error_param=error_param,
        )
        response_create_gate = request_state.response_create_gate
        if (
            response_create_gate is not None
            or request_state.response_create_admission is not None
            or request_state.awaiting_response_created
        ):
            _release_websocket_response_create_gate(request_state, response_create_gate)
        try:
            await self._send_downstream_websocket_text(
                websocket,
                client_send_lock=client_send_lock,
                text=json.dumps(event, ensure_ascii=True, separators=(",", ":")),
                downstream_activity=downstream_activity,
            )
        except Exception:
            logger.debug("Failed to emit websocket terminal error", exc_info=True)

    async def _send_downstream_websocket_text(
        self: _WebSocketRelayService,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        text: str,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None:
        if downstream_activity is not None:
            downstream_activity.mark()
        async with client_send_lock:
            if downstream_activity is not None:
                downstream_activity.mark()
            await websocket.send_text(text)
            if downstream_activity is not None:
                downstream_activity.mark()

    async def _send_downstream_websocket_bytes(
        self: _WebSocketRelayService,
        websocket: WebSocket,
        *,
        client_send_lock: anyio.Lock,
        data: bytes,
        downstream_activity: _DownstreamWebSocketActivity | None = None,
    ) -> None:
        if downstream_activity is not None:
            downstream_activity.mark()
        async with client_send_lock:
            if downstream_activity is not None:
                downstream_activity.mark()
            await websocket.send_bytes(data)
            if downstream_activity is not None:
                downstream_activity.mark()
