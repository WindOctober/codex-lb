from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Sequence
from typing import Protocol

import anyio
from fastapi import WebSocket

from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
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
    _DownstreamWebSocketActivity,
    _event_type_from_payload,
    _release_websocket_response_create_gate,
    _should_penalize_stream_error,
    _stream_settlement_error_payload,
    _StreamSettlement,
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

    async def _settle_stream_api_key_usage(
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
        api_key: ApiKeyData | None,
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
    ) -> None:
        try:
            while True:
                receive_timeout = await self._next_websocket_receive_timeout(
                    pending_requests,
                    pending_lock=pending_lock,
                    proxy_request_budget_seconds=proxy_request_budget_seconds,
                    stream_idle_timeout_seconds=stream_idle_timeout_seconds,
                )
                try:
                    if receive_timeout is None:
                        message = await upstream.receive()
                    elif receive_timeout.timeout_seconds <= 0:
                        raise asyncio.TimeoutError()
                    else:
                        message = await asyncio.wait_for(
                            upstream.receive(),
                            timeout=receive_timeout.timeout_seconds,
                        )
                except asyncio.TimeoutError:
                    if receive_timeout is None:
                        raise
                    if receive_timeout.fail_all_pending:
                        await self._fail_pending_websocket_requests(
                            account_id_value=account_id_value,
                            pending_requests=pending_requests,
                            pending_lock=pending_lock,
                            error_code=receive_timeout.error_code,
                            error_message=receive_timeout.error_message,
                            api_key=api_key,
                            websocket=websocket,
                            client_send_lock=client_send_lock,
                            response_create_gate=response_create_gate,
                        )
                        upstream_control.reconnect_requested = True
                        try:
                            await upstream.close()
                        except Exception:
                            logger.debug("Failed to close upstream websocket after timeout", exc_info=True)
                        break
                    await self._fail_expired_pending_websocket_requests(
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
                    )
                    continue
                if message.kind == "text" and message.text is not None:
                    downstream_activity.mark()
                    downstream_text = await self._process_upstream_websocket_text(
                        message.text,
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
                            try:
                                await upstream.close()
                            except Exception:
                                logger.debug("Failed to close upstream websocket for reconnect", exc_info=True)
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
                    try:
                        await upstream.close()
                    except Exception:
                        logger.debug("Failed to close upstream websocket for replay", exc_info=True)
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

        async with pending_lock:
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
            for grouped_request_state in grouped_previous_response_request_states:
                (
                    grouped_downstream_text,
                    _grouped_event_block,
                    grouped_event,
                    grouped_payload,
                    grouped_event_type,
                ) = _build_stream_incomplete_terminal_event_for_request(grouped_request_state)
                downstream_texts.append(grouped_downstream_text)
                await self._finalize_websocket_request_state(
                    grouped_request_state,
                    account=account,
                    account_id_value=account_id_value,
                    event=grouped_event,
                    event_type=grouped_event_type,
                    payload=grouped_payload,
                    api_key=api_key,
                    upstream_control=upstream_control,
                    response_create_gate=response_create_gate,
                )
            upstream_control.suppress_downstream_event = True
            upstream_control.downstream_texts = downstream_texts
            return downstream_texts[0]

        if len(grouped_previous_response_request_states) == 1 and request_state is None:
            request_state = grouped_previous_response_request_states[0]

        if request_state is None:
            if is_previous_response_not_found_event:
                upstream_control.suppress_downstream_event = True
            return text

        retry_is_previous_response_not_found = is_previous_response_not_found_event
        retry_error_code = _websocket_precreated_retry_error_code(
            request_state,
            event_type=event_type,
            payload=payload,
            has_other_pending_requests=has_other_pending_requests,
        )
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
            await self._handle_stream_error(
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
                await self._handle_stream_error(
                    account,
                    {"message": _websocket_event_error_message(event_type, payload) or "Upstream error"},
                    retry_error_code,
                )
            return downstream_text

        await self._finalize_websocket_request_state(
            request_state,
            account=account,
            account_id_value=account_id_value,
            event=event,
            event_type=event_type,
            payload=payload,
            api_key=api_key,
            upstream_control=upstream_control,
            response_create_gate=response_create_gate,
        )
        return downstream_text

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
    ) -> None:
        now = time.monotonic()
        async with pending_lock:
            expired_requests = [
                request_state
                for request_state in list(pending_requests)
                if now >= _request_deadline_at(request_state, request_budget_seconds)
            ]
            for request_state in expired_requests:
                pending_requests.remove(request_state)
        if not expired_requests:
            return
        await self._fail_pending_websocket_requests(
            account_id_value=account_id_value,
            pending_requests=deque(expired_requests),
            pending_lock=anyio.Lock(),
            error_code=error_code,
            error_message=error_message,
            api_key=api_key,
            websocket=websocket,
            client_send_lock=client_send_lock,
            response_create_gate=response_create_gate,
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
        api_key: ApiKeyData | None,
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
            error_code=error_code,
            error_message=error_message,
            error=error_payload,
        )
        if event_type in {"response.failed", "response.incomplete", "error"}:
            settlement.record_success = False
        if event_type in {"response.failed", "error"}:
            settlement.account_health_error = _should_penalize_stream_error(error_code)
        _release_websocket_response_create_gate(request_state, response_create_gate)
        self._release_request_account_model_concurrency(request_state)
        await self._settle_stream_api_key_usage(
            api_key,
            request_state.api_key_reservation,
            settlement,
            response_id,
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
        reasoning_tokens = (
            usage.output_tokens_details.reasoning_tokens if usage and usage.output_tokens_details else None
        )
        if not request_state.skip_request_log:
            await self._write_request_log(
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
                reasoning_tokens=reasoning_tokens,
                reasoning_effort=request_state.reasoning_effort,
                transport=request_state.transport,
                service_tier=response_service_tier,
                requested_service_tier=request_state.requested_service_tier,
                actual_service_tier=request_state.actual_service_tier,
                latency_first_token_ms=request_state.latency_first_token_ms,
                session_id=request_state.session_id,
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
        await self._write_request_log(
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
        await self._release_websocket_reservation(request_state.api_key_reservation)
        await self._write_websocket_connect_failure(
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

        last_index = len(remaining) - 1
        for index, request_state in enumerate(remaining):
            request_error_code = request_state.error_code_override or error_code
            request_error_message = request_state.error_message_override or error_message
            request_error_type = request_state.error_type_override or "server_error"
            request_error_param = request_state.error_param_override
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
            await self._release_websocket_reservation(request_state.api_key_reservation)
            if account_id_value is None or request_state.skip_request_log:
                continue
            latency_ms = int((time.monotonic() - request_state.started_at) * 1000)
            await self._write_request_log(
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
