from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Protocol

import anyio

from app.core.config.settings import Settings
from app.core.openai.models import OpenAIEvent
from app.core.openai.parsing import parse_sse_event
from app.core.types import JsonValue
from app.core.utils.sse import parse_sse_data_json
from app.db.models import Account
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _extract_model_class
from app.modules.proxy._service.observability import (
    _log_http_bridge_event,
    _log_http_bridge_latency_breakdown,
    _record_http_bridge_upstream_event,
)
from app.modules.proxy._service.service_tier import _service_tier_from_event_payload
from app.modules.proxy._service.support import (
    _event_type_from_payload,
    _HTTPBridgeSession,
    _release_websocket_response_create_gate,
    _WebSocketReceiveTimeout,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy._service.websocket.events import (
    _assign_websocket_response_id,
    _build_stream_incomplete_terminal_event_for_request,
    _find_websocket_request_state_by_response_id,
    _has_other_precreated_pending_requests,
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


class _HTTPBridgeUpstreamEventsService(Protocol):
    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    async def _next_websocket_receive_timeout(
        self,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        proxy_request_budget_seconds: float,
        stream_idle_timeout_seconds: float,
    ) -> _WebSocketReceiveTimeout | None: ...

    async def _retry_http_bridge_precreated_request(self, session: _HTTPBridgeSession) -> bool: ...

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
    ) -> tuple[bool, _WebSocketRequestState | None, bool]: ...

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

    async def _evict_http_bridge_session_after_upstream_disconnect(
        self,
        session: _HTTPBridgeSession,
        *,
        error_message: str,
    ) -> None: ...

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
        api_key: ApiKeyData | None,
        upstream_control: _WebSocketUpstreamControl,
        response_create_gate: asyncio.Semaphore | None,
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
        try:
            while True:
                receive_timeout = await self._next_websocket_receive_timeout(
                    session.pending_requests,
                    pending_lock=session.pending_lock,
                    proxy_request_budget_seconds=runtime_settings.proxy_request_budget_seconds,
                    stream_idle_timeout_seconds=runtime_settings.stream_idle_timeout_seconds,
                )
                try:
                    if receive_timeout is None:
                        message = await session.upstream.receive()
                    elif receive_timeout.timeout_seconds <= 0:
                        raise asyncio.TimeoutError()
                    else:
                        message = await asyncio.wait_for(
                            session.upstream.receive(),
                            timeout=receive_timeout.timeout_seconds,
                        )
                except asyncio.TimeoutError:
                    if receive_timeout is None:
                        raise
                    retried = await self._retry_http_bridge_precreated_request(session)
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
                            error_code=receive_timeout.error_code,
                            error_message=receive_timeout.error_message,
                            api_key=None,
                            response_create_gate=session.response_create_gate,
                        )
                        await self._evict_http_bridge_session_after_upstream_disconnect(
                            session,
                            error_message=receive_timeout.error_message,
                        )
                    break

                if message.kind == "text" and message.text is not None:
                    await self._process_http_bridge_upstream_text(session, message.text)
                    if session.upstream_control.reconnect_requested:
                        should_close = session.upstream_control.replay_request_state is not None
                        if not should_close:
                            async with session.pending_lock:
                                should_close = not session.pending_requests
                        if should_close:
                            async with session.lifecycle_lock:
                                session.closed = True
                                try:
                                    await session.upstream.close()
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
            session.closed = True

    async def _process_http_bridge_upstream_text(
        self: _HTTPBridgeUpstreamEventsService,
        session: _HTTPBridgeSession,
        text: str,
    ) -> None:
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
        retryable_precreated_error_code: str | None = None
        retryable_precreated_request_state: _WebSocketRequestState | None = None
        retryable_no_text_error_code: str | None = None
        retryable_no_text_request_state: _WebSocketRequestState | None = None

        async with session.pending_lock:
            matched_request_state = None
            created_request_state = None
            has_other_pending_requests = False
            grouped_previous_response_request_states: list[_WebSocketRequestState] = []
            if event_type == "response.created":
                matched_request_state = _assign_websocket_response_id(session.pending_requests, response_id)
                created_request_state = matched_request_state
                release_create_gate = matched_request_state is not None
            elif response_id is not None:
                matched_request_state = _find_websocket_request_state_by_response_id(
                    session.pending_requests,
                    response_id,
                )
                release_create_gate = False
            elif response_id is None:
                matched_request_state = _match_websocket_request_state_for_anonymous_event(
                    session.pending_requests,
                    prefer_previous_response_not_found=is_previous_response_not_found_event,
                    previous_response_id_hint=previous_response_id_hint,
                    error_message=error_message,
                )
                release_create_gate = False
            else:
                release_create_gate = False

            if matched_request_state is not None:
                actual_service_tier = _service_tier_from_event_payload(payload)
                if actual_service_tier is not None:
                    matched_request_state.actual_service_tier = actual_service_tier
                    matched_request_state.service_tier = actual_service_tier
            if event_type in {"response.failed", "error"} and matched_request_state is not None:
                retryable_precreated_error_code = _http_bridge_precreated_failover_error_code(
                    matched_request_state,
                    event_type=event_type,
                    payload=payload,
                    has_other_pending_requests=_has_other_precreated_pending_requests(
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
                    )
                    if retryable_no_text_error_code is not None:
                        retryable_no_text_request_state = matched_request_state

            terminal_request_state = None
            if (
                retryable_precreated_request_state is None
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
                        has_other_pending_requests=_has_other_precreated_pending_requests(
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
                if terminal_request_state is not None:
                    session.queued_request_count = max(0, session.queued_request_count - 1)
                elif is_previous_response_not_found_event:
                    grouped_previous_response_request_states = _pop_matching_websocket_request_states(
                        session.pending_requests,
                        _matching_websocket_request_states_for_previous_response_error(
                            session.pending_requests,
                            previous_response_id_hint=previous_response_id_hint,
                            error_message=error_message,
                        ),
                    )
                    if grouped_previous_response_request_states:
                        session.queued_request_count = max(
                            0,
                            session.queued_request_count - len(grouped_previous_response_request_states),
                        )
                has_other_pending_requests = bool(session.pending_requests)

        if retryable_precreated_request_state is not None and retryable_precreated_error_code is not None:
            (
                retried,
                terminal_request_state,
                has_other_pending_requests,
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
            if retried:
                return

        if retryable_no_text_request_state is not None and retryable_no_text_error_code is not None:
            (
                retried,
                terminal_request_state,
                has_other_pending_requests,
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
            if retried:
                return

        if len(grouped_previous_response_request_states) > 1:
            session.upstream_control.reconnect_requested = True
            for grouped_request_state in grouped_previous_response_request_states:
                grouped_request_state.error_http_status_override = 502
                (
                    _grouped_downstream_text,
                    grouped_event_block,
                    grouped_event,
                    grouped_payload,
                    grouped_event_type,
                ) = _build_stream_incomplete_terminal_event_for_request(grouped_request_state)
                if grouped_request_state.event_queue is not None:
                    await grouped_request_state.event_queue.put(grouped_event_block)
                try:
                    await self._finalize_websocket_request_state(
                        grouped_request_state,
                        account=session.account,
                        account_id_value=session.account.id,
                        event=grouped_event,
                        event_type=grouped_event_type,
                        payload=grouped_payload,
                        api_key=grouped_request_state.api_key,
                        upstream_control=session.upstream_control,
                        response_create_gate=session.response_create_gate,
                    )
                finally:
                    if grouped_request_state.event_queue is not None:
                        await grouped_request_state.event_queue.put(None)
            return

        if len(grouped_previous_response_request_states) == 1 and terminal_request_state is None:
            terminal_request_state = grouped_previous_response_request_states[0]

        status_request_state = terminal_request_state or matched_request_state
        if status_request_state is None and is_previous_response_not_found_event:
            session.upstream_control.reconnect_requested = True
            return

        if (
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

        if event_type == "error":
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

        if event_type == "response.created" and release_create_gate and created_request_state is not None:
            _release_websocket_response_create_gate(created_request_state, session.response_create_gate)

        if response_id is not None and matched_request_state is not None and event_type == "response.completed":
            await self._register_http_bridge_previous_response_id(
                session,
                response_id,
                input_item_count=(
                    matched_request_state.input_item_count if matched_request_state.input_item_count > 0 else None
                ),
                input_full_fingerprint=(
                    matched_request_state.input_full_fingerprint if matched_request_state.input_item_count > 0 else None
                ),
            )

        if matched_request_state is not None:
            _record_http_bridge_upstream_event(matched_request_state, event_type)
            _log_http_bridge_latency_breakdown(session, matched_request_state, event_type=event_type)
        if matched_request_state is not None and matched_request_state.event_queue is not None:
            await matched_request_state.event_queue.put(event_block)

        if terminal_request_state is None:
            return

        if terminal_request_state is not matched_request_state:
            _record_http_bridge_upstream_event(terminal_request_state, event_type)
            _log_http_bridge_latency_breakdown(session, terminal_request_state, event_type=event_type)
        if terminal_request_state is not matched_request_state and terminal_request_state.event_queue is not None:
            await terminal_request_state.event_queue.put(event_block)

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
                account_id=session.account.id,
                model=session.request_model,
                detail=error_code,
                pending_count=await self._http_bridge_pending_count(session),
                cache_key_family=session.key.affinity_kind,
                model_class=_extract_model_class(session.request_model) if session.request_model else None,
            )

        try:
            await self._finalize_websocket_request_state(
                terminal_request_state,
                account=session.account,
                account_id_value=session.account.id,
                event=event,
                event_type=event_type,
                payload=payload,
                api_key=terminal_request_state.api_key,
                upstream_control=session.upstream_control,
                response_create_gate=session.response_create_gate,
            )
        finally:
            if terminal_request_state.event_queue is not None:
                await terminal_request_state.event_queue.put(None)
