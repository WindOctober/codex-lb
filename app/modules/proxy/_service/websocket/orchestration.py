from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from typing import Protocol

import anyio
from fastapi import WebSocket
from pydantic import ValidationError

from app.core.balancer import RoutingStrategy
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket, filter_inbound_websocket_headers
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.core.exceptions import AppError
from app.core.openai.exceptions import ClientPayloadError
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.db.models import Account, DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _headers_with_turn_state,
    _owner_lookup_session_id_from_headers,
    _prompt_cache_key_from_request_model,
    _response_create_client_metadata,
    _sticky_key_from_turn_state_header,
    _upstream_turn_state_from_socket,
)
from app.modules.proxy._service.affinity import (
    _sticky_key_for_responses_request as _sticky_key_for_responses_request_impl,
)
from app.modules.proxy._service.budget import _set_request_budget
from app.modules.proxy._service.observability import (
    _maybe_log_proxy_request_shape as _maybe_log_proxy_request_shape_impl,
)
from app.modules.proxy._service.service_tier import (
    _http_bridge_text_with_account_service_tier,
    _normalize_service_tier_value,
)
from app.modules.proxy._service.support import (
    _REQUEST_TRANSPORT_WEBSOCKET,
    _AffinityPolicy,
    _await_cancelled_task,
    _DownstreamWebSocketActivity,
    _PreparedWebSocketRequest,
    _release_websocket_response_create_gate,
    _routing_strategy,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy._service.websocket.events import (
    _app_error_to_websocket_event,
    _is_websocket_response_create,
    _parse_websocket_payload,
    _pop_replayable_precreated_websocket_request_state,
    _serialize_websocket_error_event,
    _wrapped_websocket_error_event,
)
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error
from app.modules.proxy.request_policy import (
    apply_api_key_enforcement,
    normalize_responses_request_payload,
    openai_invalid_payload_error,
    openai_validation_error,
    validate_model_access,
)

logger = logging.getLogger("app.modules.proxy.service")

_DOWNSTREAM_WEBSOCKET_IDLE_CLOSE_REASON = "Idle downstream websocket timeout"
_DOWNSTREAM_WEBSOCKET_RECEIVE_POLL_SECONDS = 1.0


class _WebSocketOrchestrationService(Protocol):
    @staticmethod
    def _proxy_runtime_settings() -> Settings: ...

    @staticmethod
    async def _proxy_dashboard_settings() -> DashboardSettings: ...

    async def _release_websocket_reservation(
        self,
        reservation: ApiKeyUsageReservationData | None,
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

    async def _downstream_websocket_is_idle(
        self,
        pending_requests: deque[_WebSocketRequestState],
        *,
        pending_lock: anyio.Lock,
        downstream_activity: _DownstreamWebSocketActivity,
        idle_timeout_seconds: float,
    ) -> bool: ...

    async def _prepare_websocket_response_create_request(
        self,
        payload: dict[str, JsonValue],
        *,
        headers: Mapping[str, str],
        codex_session_affinity: bool,
        openai_cache_affinity: bool,
        sticky_threads_enabled: bool,
        openai_cache_affinity_max_age_seconds: int,
        api_key: ApiKeyData | None,
    ) -> _PreparedWebSocketRequest: ...

    async def _resolve_websocket_previous_response_owner(
        self,
        *,
        previous_response_id: str | None,
        api_key: ApiKeyData | None,
        session_id: str | None = None,
        surface: str,
    ) -> str | None: ...

    async def _write_websocket_connect_failure(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_state: _WebSocketRequestState,
        error_code: str,
        error_message: str,
    ) -> None: ...

    async def _acquire_request_state_response_create_admission(
        self,
        request_state: _WebSocketRequestState,
        *,
        response_create_gate: asyncio.Semaphore | None,
        compact: bool = False,
    ) -> None: ...

    async def _connect_proxy_websocket(
        self,
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
    ) -> tuple[Account | None, UpstreamResponsesWebSocket | None]: ...

    async def _relay_upstream_websocket_messages(
        self,
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

    async def _refresh_websocket_api_key_policy(self, api_key: ApiKeyData | None) -> ApiKeyData | None: ...

    async def _reserve_websocket_api_key_usage(
        self,
        api_key: ApiKeyData | None,
        *,
        request_model: str | None,
        request_service_tier: str | None,
    ) -> ApiKeyUsageReservationData | None: ...

    def _prepare_response_bridge_request_state(
        self,
        payload: ResponsesRequest,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        include_type_field: bool,
        attach_event_queue: bool,
        transport: str,
        client_metadata: Mapping[str, JsonValue] | None,
        session_id: str | None = None,
        request_id: str | None = None,
        request_log_id: str | None = None,
    ) -> tuple[_WebSocketRequestState, str]: ...


class _WebSocketOrchestrationMixin:
    async def proxy_responses_websocket(
        self: _WebSocketOrchestrationService,
        websocket: WebSocket,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool,
        openai_cache_affinity: bool,
        api_key: ApiKeyData | None,
    ) -> None:
        filtered_headers = filter_inbound_websocket_headers(dict(headers))
        runtime_settings = self._proxy_runtime_settings()
        settings = await self._proxy_dashboard_settings()
        prefer_earlier_reset = settings.prefer_earlier_reset_accounts
        sticky_threads_enabled = settings.sticky_threads_enabled
        openai_cache_affinity_max_age_seconds = settings.openai_cache_affinity_max_age_seconds
        routing_strategy = _routing_strategy(settings)
        pending_requests: deque[_WebSocketRequestState] = deque()
        pending_lock = anyio.Lock()
        client_send_lock = anyio.Lock()
        response_create_gate = asyncio.Semaphore(1)
        upstream: UpstreamResponsesWebSocket | None = None
        upstream_reader: asyncio.Task[None] | None = None
        upstream_control: _WebSocketUpstreamControl | None = None
        account: Account | None = None
        upstream_turn_state: str | None = _sticky_key_from_turn_state_header(headers)
        downstream_activity = _DownstreamWebSocketActivity()
        replay_request_state: _WebSocketRequestState | None = None

        try:
            while True:
                if upstream_reader is not None and upstream_reader.done():
                    try:
                        await upstream_reader
                    except asyncio.CancelledError:
                        pass
                    if replay_request_state is None and upstream_control is not None:
                        replay_request_state = upstream_control.replay_request_state
                    upstream_reader = None
                    upstream_control = None
                    if upstream is not None:
                        try:
                            await upstream.close()
                        except Exception:
                            logger.debug("Failed to close upstream websocket", exc_info=True)
                    upstream = None
                    account = None

                text_data: str | None = None
                bytes_data: bytes | None = None
                request_state: _WebSocketRequestState | None = None
                request_state_registered = False
                request_affinity = _AffinityPolicy()
                payload: dict[str, JsonValue] | None = None

                if replay_request_state is not None:
                    request_state = replay_request_state
                    replay_request_state = None
                    _set_request_budget(
                        request_state,
                        runtime_settings.proxy_reconnect_request_budget_seconds,
                        restart_from_now=True,
                    )
                    request_affinity = request_state.affinity_policy
                    text_data = request_state.request_text
                    if text_data is None:
                        await self._release_websocket_reservation(request_state.api_key_reservation)
                        await self._emit_websocket_terminal_error(
                            websocket,
                            client_send_lock=client_send_lock,
                            request_state=request_state,
                            error_code="stream_incomplete",
                            error_message="Upstream websocket closed before response.completed",
                            error_type="server_error",
                            downstream_activity=downstream_activity,
                        )
                        _release_websocket_response_create_gate(request_state, response_create_gate)
                        continue
                    payload = _parse_websocket_payload(text_data)
                    if payload is None:
                        await self._release_websocket_reservation(request_state.api_key_reservation)
                        await self._emit_websocket_terminal_error(
                            websocket,
                            client_send_lock=client_send_lock,
                            request_state=request_state,
                            error_code="upstream_error",
                            error_message="Invalid replay request payload",
                            error_type="server_error",
                            downstream_activity=downstream_activity,
                        )
                        _release_websocket_response_create_gate(request_state, response_create_gate)
                        continue
                    async with pending_lock:
                        pending_requests.append(request_state)
                    request_state_registered = True
                else:
                    downstream_idle_timeout_seconds = runtime_settings.proxy_downstream_websocket_idle_timeout_seconds
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive(),
                            timeout=min(downstream_idle_timeout_seconds, _DOWNSTREAM_WEBSOCKET_RECEIVE_POLL_SECONDS),
                        )
                    except asyncio.TimeoutError:
                        if not await self._downstream_websocket_is_idle(
                            pending_requests,
                            pending_lock=pending_lock,
                            downstream_activity=downstream_activity,
                            idle_timeout_seconds=downstream_idle_timeout_seconds,
                        ):
                            continue
                        idle_close = False
                        async with client_send_lock:
                            if await self._downstream_websocket_is_idle(
                                pending_requests,
                                pending_lock=pending_lock,
                                downstream_activity=downstream_activity,
                                idle_timeout_seconds=downstream_idle_timeout_seconds,
                            ):
                                try:
                                    message = await asyncio.wait_for(websocket.receive(), timeout=0.05)
                                except asyncio.TimeoutError:
                                    try:
                                        await websocket.close(code=1001, reason=_DOWNSTREAM_WEBSOCKET_IDLE_CLOSE_REASON)
                                    except Exception:
                                        logger.debug("Failed to close idle downstream websocket", exc_info=True)
                                    idle_close = True
                        if idle_close:
                            break
                    downstream_activity.mark()
                    message_type = message["type"]

                    if message_type == "websocket.disconnect":
                        break
                    if message_type != "websocket.receive":
                        continue

                    text_data = message.get("text")
                    bytes_data = message.get("bytes")

                    if text_data is not None:
                        payload = _parse_websocket_payload(text_data)
                        if payload is not None and _is_websocket_response_create(payload):
                            try:
                                prepared_request = await self._prepare_websocket_response_create_request(
                                    payload,
                                    headers=headers,
                                    codex_session_affinity=codex_session_affinity,
                                    openai_cache_affinity=openai_cache_affinity,
                                    sticky_threads_enabled=sticky_threads_enabled,
                                    openai_cache_affinity_max_age_seconds=openai_cache_affinity_max_age_seconds,
                                    api_key=api_key,
                                )
                                request_state = prepared_request.request_state
                                request_affinity = prepared_request.affinity_policy
                                text_data = prepared_request.text_data
                            except ProxyResponseError as exc:
                                async with client_send_lock:
                                    await websocket.send_text(
                                        _serialize_websocket_error_event(
                                            _wrapped_websocket_error_event(exc.status_code, exc.payload)
                                        )
                                    )
                                continue
                            except AppError as exc:
                                async with client_send_lock:
                                    await websocket.send_text(
                                        _serialize_websocket_error_event(_app_error_to_websocket_event(exc))
                                    )
                                continue
                            except ClientPayloadError as exc:
                                async with client_send_lock:
                                    await websocket.send_text(
                                        _serialize_websocket_error_event(
                                            _wrapped_websocket_error_event(400, openai_invalid_payload_error(exc.param))
                                        )
                                    )
                                continue
                            except ValidationError as exc:
                                async with client_send_lock:
                                    await websocket.send_text(
                                        _serialize_websocket_error_event(
                                            _wrapped_websocket_error_event(400, openai_validation_error(exc))
                                        )
                                    )
                                continue

                if upstream_reader is not None and upstream_reader.done():
                    try:
                        await upstream_reader
                    except asyncio.CancelledError:
                        pass
                    if replay_request_state is None and upstream_control is not None:
                        replay_request_state = upstream_control.replay_request_state
                    upstream_reader = None
                    upstream_control = None
                    if upstream is not None:
                        try:
                            await upstream.close()
                        except Exception:
                            logger.debug("Failed to close upstream websocket", exc_info=True)
                    upstream = None
                    account = None

                if (
                    request_state is not None
                    and upstream_control is not None
                    and upstream_control.reconnect_requested
                    and upstream_reader is not None
                ):
                    await upstream_reader
                    if replay_request_state is None:
                        replay_request_state = upstream_control.replay_request_state
                    upstream_reader = None
                    upstream_control = None
                    if upstream is not None:
                        try:
                            await upstream.close()
                        except Exception:
                            logger.debug("Failed to close upstream websocket", exc_info=True)
                    upstream = None
                    account = None

                if (
                    request_state is not None
                    and request_state.previous_response_id is not None
                    and request_state.preferred_account_id is None
                ):
                    try:
                        request_state.preferred_account_id = await self._resolve_websocket_previous_response_owner(
                            previous_response_id=request_state.previous_response_id,
                            api_key=request_state.api_key or api_key,
                            session_id=request_state.session_id,
                            surface="websocket",
                        )
                    except ProxyResponseError as exc:
                        error = _parse_openai_error(exc.payload)
                        error_code = _normalize_error_code(
                            error.code if error else None,
                            error.type if error else None,
                        )
                        error_message = error.message if error and error.message else "Upstream error"
                        error_type = error.type if error and error.type else "server_error"
                        await self._release_websocket_reservation(request_state.api_key_reservation)
                        await self._write_websocket_connect_failure(
                            account_id=None,
                            api_key=api_key,
                            request_state=request_state,
                            error_code=error_code or "upstream_error",
                            error_message=error_message,
                        )
                        await self._emit_websocket_terminal_error(
                            websocket,
                            client_send_lock=client_send_lock,
                            request_state=request_state,
                            error_code=error_code or "upstream_error",
                            error_message=error_message,
                            error_type=error_type,
                            downstream_activity=downstream_activity,
                        )
                        request_state = None
                        text_data = None
                        payload = None
                        continue

                if request_state is not None and not request_state_registered:
                    try:
                        await self._acquire_request_state_response_create_admission(
                            request_state,
                            response_create_gate=response_create_gate,
                        )
                        async with pending_lock:
                            pending_requests.append(request_state)
                        request_state_registered = True
                    except ProxyResponseError as exc:
                        error = _parse_openai_error(exc.payload)
                        error_code = _normalize_error_code(
                            error.code if error else None,
                            error.type if error else None,
                        )
                        error_message = error.message if error and error.message else "Upstream error"
                        error_type = error.type if error and error.type else "server_error"
                        await self._release_websocket_reservation(request_state.api_key_reservation)
                        await self._write_websocket_connect_failure(
                            account_id=account.id if account else None,
                            api_key=api_key,
                            request_state=request_state,
                            error_code=error_code or "upstream_error",
                            error_message=error_message,
                        )
                        await self._emit_websocket_terminal_error(
                            websocket,
                            client_send_lock=client_send_lock,
                            request_state=request_state,
                            error_code=error_code or "upstream_error",
                            error_message=error_message,
                            error_type=error_type,
                            downstream_activity=downstream_activity,
                        )
                        _release_websocket_response_create_gate(request_state, response_create_gate)
                        continue
                    except asyncio.CancelledError:
                        await self._release_websocket_reservation(request_state.api_key_reservation)
                        if request_state_registered:
                            async with pending_lock:
                                if request_state in pending_requests:
                                    pending_requests.remove(request_state)
                        _release_websocket_response_create_gate(request_state, response_create_gate)
                        raise
                    except Exception:
                        await self._release_websocket_reservation(request_state.api_key_reservation)
                        if request_state_registered:
                            async with pending_lock:
                                if request_state in pending_requests:
                                    pending_requests.remove(request_state)
                        _release_websocket_response_create_gate(request_state, response_create_gate)
                        raise

                if upstream is None:
                    if text_data is not None and payload is None:
                        async with client_send_lock:
                            await websocket.send_text(
                                _serialize_websocket_error_event(
                                    _wrapped_websocket_error_event(400, openai_invalid_payload_error())
                                )
                            )
                        continue
                    if request_state is None:
                        async with client_send_lock:
                            await websocket.send_text(
                                _serialize_websocket_error_event(
                                    _wrapped_websocket_error_event(
                                        400,
                                        openai_error(
                                            "invalid_request_error",
                                            "WebSocket connection has no active upstream session",
                                            error_type="invalid_request_error",
                                        ),
                                    )
                                )
                            )
                        continue
                    connect_headers = _headers_with_turn_state(filtered_headers, upstream_turn_state)
                    account, upstream = await self._connect_proxy_websocket(
                        connect_headers,
                        sticky_key=request_affinity.key,
                        sticky_kind=request_affinity.kind,
                        reallocate_sticky=request_affinity.reallocate_sticky,
                        sticky_max_age_seconds=request_affinity.max_age_seconds,
                        prefer_earlier_reset=prefer_earlier_reset,
                        routing_strategy=routing_strategy,
                        model=request_state.model,
                        request_state=request_state,
                        api_key=api_key,
                        client_send_lock=client_send_lock,
                        websocket=websocket,
                    )
                    if upstream is None or account is None:
                        if request_state_registered:
                            async with pending_lock:
                                if request_state in pending_requests:
                                    pending_requests.remove(request_state)
                            _release_websocket_response_create_gate(request_state, response_create_gate)
                        continue
                    upstream_turn_state = _upstream_turn_state_from_socket(upstream) or upstream_turn_state
                    upstream_control = _WebSocketUpstreamControl()
                    upstream_reader = asyncio.create_task(
                        self._relay_upstream_websocket_messages(
                            websocket,
                            upstream,
                            account=account,
                            account_id_value=account.id,
                            pending_requests=pending_requests,
                            pending_lock=pending_lock,
                            client_send_lock=client_send_lock,
                            api_key=api_key,
                            upstream_control=upstream_control,
                            response_create_gate=response_create_gate,
                            proxy_request_budget_seconds=runtime_settings.proxy_request_budget_seconds,
                            stream_idle_timeout_seconds=runtime_settings.stream_idle_timeout_seconds,
                            downstream_activity=downstream_activity,
                        )
                    )

                try:
                    if text_data is not None:
                        if account is not None and request_state is not None:
                            text_data, forwarded_service_tier = _http_bridge_text_with_account_service_tier(
                                text_data,
                                account,
                                api_key=request_state.api_key or api_key,
                            )
                            if forwarded_service_tier is not None:
                                request_state.service_tier = forwarded_service_tier
                                request_state.requested_service_tier = forwarded_service_tier
                        await upstream.send_text(text_data)
                    elif bytes_data is not None:
                        await upstream.send_bytes(bytes_data)
                except Exception:
                    replay_candidate = await _pop_replayable_precreated_websocket_request_state(
                        pending_requests,
                        pending_lock=pending_lock,
                    )
                    if replay_candidate is not None:
                        logger.info(
                            "Transparent websocket replay after upstream send failure request_id=%s",
                            replay_candidate.request_log_id or replay_candidate.request_id,
                        )
                        replay_request_state = replay_candidate
                        if upstream_reader is not None:
                            await _await_cancelled_task(upstream_reader, label="proxy websocket upstream reader")
                            upstream_reader = None
                        upstream_control = None
                        if upstream is not None:
                            try:
                                await upstream.close()
                            except Exception:
                                logger.debug(
                                    "Failed to close upstream websocket after replayable send failure",
                                    exc_info=True,
                                )
                        upstream = None
                        account = None
                        continue
                    await self._fail_pending_websocket_requests(
                        account_id_value=account.id if account else None,
                        pending_requests=pending_requests,
                        pending_lock=pending_lock,
                        error_code="stream_incomplete",
                        error_message="Upstream websocket closed before response.completed",
                        api_key=api_key,
                        websocket=websocket,
                        client_send_lock=client_send_lock,
                        response_create_gate=response_create_gate,
                        downstream_activity=downstream_activity,
                    )
                    if upstream_reader is not None:
                        await _await_cancelled_task(upstream_reader, label="proxy websocket upstream reader")
                        upstream_reader = None
                    upstream_control = None
                    if upstream is not None:
                        try:
                            await upstream.close()
                        except Exception:
                            logger.debug("Failed to close upstream websocket after send failure", exc_info=True)
                    upstream = None
                    account = None
                    continue
        finally:
            if upstream_reader is not None:
                await _await_cancelled_task(upstream_reader, label="proxy websocket upstream reader")
            if upstream is not None:
                try:
                    await upstream.close()
                except Exception:
                    logger.debug("Failed to close upstream websocket", exc_info=True)
            await self._fail_pending_websocket_requests(
                account_id_value=account.id if account else None,
                pending_requests=pending_requests,
                pending_lock=pending_lock,
                error_code="stream_incomplete",
                error_message="Upstream websocket closed before response.completed",
                api_key=api_key,
                websocket=websocket,
                client_send_lock=client_send_lock,
                response_create_gate=response_create_gate,
                downstream_activity=downstream_activity,
            )

    async def _prepare_websocket_response_create_request(
        self: _WebSocketOrchestrationService,
        payload: dict[str, JsonValue],
        *,
        headers: Mapping[str, str],
        codex_session_affinity: bool,
        openai_cache_affinity: bool,
        sticky_threads_enabled: bool,
        openai_cache_affinity_max_age_seconds: int,
        api_key: ApiKeyData | None,
    ) -> _PreparedWebSocketRequest:
        refreshed_api_key = await self._refresh_websocket_api_key_policy(api_key)
        client_metadata = _response_create_client_metadata(payload, headers=headers)
        responses_payload = normalize_responses_request_payload(payload, openai_compat=openai_cache_affinity)
        apply_api_key_enforcement(responses_payload, refreshed_api_key)
        validate_model_access(refreshed_api_key, responses_payload.model)
        reservation = await self._reserve_websocket_api_key_usage(
            refreshed_api_key,
            request_model=responses_payload.model,
            request_service_tier=_normalize_service_tier_value(
                dict(responses_payload.to_payload()).get("service_tier")
            ),
        )
        try:
            session_id = _owner_lookup_session_id_from_headers(headers)
            request_state, text_data = self._prepare_response_bridge_request_state(
                responses_payload,
                api_key=refreshed_api_key,
                api_key_reservation=reservation,
                include_type_field=True,
                attach_event_queue=False,
                transport=_REQUEST_TRANSPORT_WEBSOCKET,
                client_metadata=client_metadata,
                session_id=session_id,
            )
        except ProxyResponseError:
            await self._release_websocket_reservation(reservation)
            raise
        had_prompt_cache_key = _prompt_cache_key_from_request_model(responses_payload) is not None
        affinity_policy = _sticky_key_for_responses_request_impl(
            responses_payload,
            headers,
            codex_session_affinity=codex_session_affinity,
            openai_cache_affinity=openai_cache_affinity,
            openai_cache_affinity_max_age_seconds=openai_cache_affinity_max_age_seconds,
            sticky_threads_enabled=sticky_threads_enabled,
            settings=self._proxy_runtime_settings(),
            api_key=api_key,
        )
        sticky_key_source = "none"
        if affinity_policy.kind == StickySessionKind.CODEX_SESSION:
            sticky_key_source = (
                "turn_state_header" if _sticky_key_from_turn_state_header(headers) is not None else "session_header"
            )
        elif affinity_policy.key:
            sticky_key_source = "payload" if had_prompt_cache_key else "derived"
        _maybe_log_proxy_request_shape_impl(
            "websocket",
            responses_payload,
            headers,
            settings=self._proxy_runtime_settings(),
            sticky_kind=affinity_policy.kind.value if affinity_policy.kind is not None else None,
            sticky_key_source=sticky_key_source,
            prompt_cache_key_set=_prompt_cache_key_from_request_model(responses_payload) is not None,
        )
        request_state.affinity_policy = affinity_policy

        return _PreparedWebSocketRequest(
            text_data=text_data,
            request_state=request_state,
            affinity_policy=affinity_policy,
        )
