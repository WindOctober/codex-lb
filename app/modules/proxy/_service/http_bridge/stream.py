from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Literal, Protocol, cast, overload

import anyio
from fastapi import WebSocket

from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.errors import OpenAIErrorEnvelope, openai_error, response_failed_event
from app.core.metrics.prometheus import PROMETHEUS_AVAILABLE, bridge_durable_recover_total
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.core.utils.request_id import ensure_request_id
from app.core.utils.sse import format_sse_event, parse_sse_data_json
from app.db.models import DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _extract_model_class,
    _normalize_session_id,
    _prompt_cache_key_from_request_model,
    _sticky_key_for_responses_request,
    _sticky_key_from_session_header,
    _sticky_key_from_turn_state_header,
)
from app.modules.proxy._service.budget import (
    _http_bridge_request_budget_seconds,
    _set_request_budget,
)
from app.modules.proxy._service.http_bridge.keys import _make_http_bridge_session_key
from app.modules.proxy._service.http_bridge.runtime import _http_bridge_runtime_config
from app.modules.proxy._service.http_bridge.stream_policy import (
    _effective_http_bridge_idle_ttl_seconds,
    _fingerprint_input_items,
    _http_bridge_is_context_overflow_error,
    _http_bridge_payload_looks_like_full_resend,
    _http_bridge_payload_without_previous_response_id,
    _http_bridge_request_stage,
    _http_bridge_should_attempt_local_bootstrap_rebind,
    _http_bridge_should_attempt_local_previous_response_recovery,
    _http_bridge_should_rollover_after_context_overflow,
    _input_prefix_matches_stored_context,
)
from app.modules.proxy._service.observability import (
    _log_http_bridge_event,
    _log_http_bridge_latency_breakdown,
    _maybe_log_proxy_request_shape,
    _record_bridge_reattach,
)
from app.modules.proxy._service.service_tier import _normalize_service_tier_value
from app.modules.proxy._service.support import (
    _REQUEST_TRANSPORT_HTTP,
    _AffinityPolicy,
    _await_cancelled_task,
    _DownstreamWebSocketActivity,
    _DurableAccountBinding,
    _event_type_from_payload,
    _HTTPBridgeOwnerForward,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _WebSocketRequestState,
)
from app.modules.proxy.durable_bridge_coordinator import (
    DurableBridgeLookup,
    DurableBridgeSessionCoordinator,
)

logger = logging.getLogger("app.modules.proxy.service")

_STREAM_KEEPALIVE_MAX_COUNT = 6
_TEXT_DELTA_EVENT_TYPES = frozenset({"response.output_text.delta", "response.refusal.delta"})


def _account_capacity_wait_payload(
    request_state: _WebSocketRequestState | None,
    *,
    request_id: str | None,
    reason: str | None,
    retry_after_seconds: float | None,
    started_at: float | None = None,
) -> dict[str, JsonValue]:
    wait_started_at = request_state.account_capacity_wait_started_at if request_state is not None else started_at
    waited_seconds = int(max(0.0, time.monotonic() - wait_started_at)) if wait_started_at is not None else 0
    payload: dict[str, JsonValue] = {
        "type": "codex.keepalive",
        "status": "waiting_for_account_capacity",
        "request_id": request_id or (request_state.request_id if request_state is not None else None),
        "waited_seconds": waited_seconds,
    }
    if reason:
        payload["reason"] = reason
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = int(max(0.0, retry_after_seconds))
    return payload


def _http_bridge_keepalive_frame(
    request_state: _WebSocketRequestState | None,
    *,
    request_id: str | None,
    reason: str | None = None,
    retry_after_seconds: float | None = None,
    started_at: float | None = None,
) -> str:
    return format_sse_event(
        _account_capacity_wait_payload(
            request_state,
            request_id=request_id,
            reason=reason,
            retry_after_seconds=retry_after_seconds,
            started_at=started_at,
        )
    )


def _http_bridge_keepalive_wait_timeout_seconds(
    *,
    yielded_any: bool,
    keepalive_sent: bool,
    startup_grace_seconds: float,
    heartbeat_seconds: float,
) -> float:
    if not yielded_any and not keepalive_sent:
        return max(heartbeat_seconds, startup_grace_seconds)
    return heartbeat_seconds


def _http_bridge_keepalive_max_count(settings: Settings, *, heartbeat_seconds: float) -> int:
    stream_idle_timeout_seconds = max(0.001, float(settings.stream_idle_timeout_seconds))
    return max(
        _STREAM_KEEPALIVE_MAX_COUNT,
        math.ceil(stream_idle_timeout_seconds / heartbeat_seconds),
    )


def _openai_error_envelope_from_response_failed_payload(
    payload: dict[str, JsonValue] | None,
) -> OpenAIErrorEnvelope:
    default_envelope = openai_error("upstream_error", "Upstream error")
    if not isinstance(payload, dict):
        return default_envelope
    response_payload = payload.get("response")
    if not isinstance(response_payload, dict):
        return default_envelope
    error_payload = response_payload.get("error")
    if not isinstance(error_payload, dict):
        return default_envelope

    message_value = error_payload.get("message")
    message = message_value.strip() if isinstance(message_value, str) and message_value.strip() else "Upstream error"
    code_value = error_payload.get("code")
    code = code_value.strip() if isinstance(code_value, str) and code_value.strip() else "upstream_error"
    type_value = error_payload.get("type")
    error_type = type_value.strip() if isinstance(type_value, str) and type_value.strip() else "server_error"

    envelope = openai_error(code, message, error_type)
    param_value = error_payload.get("param")
    if isinstance(param_value, str) and param_value.strip():
        envelope["error"]["param"] = param_value.strip()
    error_detail = envelope["error"]
    plan_type = error_payload.get("plan_type")
    if plan_type is not None:
        error_detail["plan_type"] = str(plan_type)
    resets_at = error_payload.get("resets_at")
    if isinstance(resets_at, int | float):
        error_detail["resets_at"] = resets_at
    resets_in = error_payload.get("resets_in_seconds")
    if isinstance(resets_in, int | float):
        error_detail["resets_in_seconds"] = resets_in
    return envelope


class _HTTPBridgeStreamService(Protocol):
    _durable_bridge: DurableBridgeSessionCoordinator
    _http_bridge_lock: anyio.Lock
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    @staticmethod
    async def _http_bridge_dashboard_settings() -> DashboardSettings: ...

    @staticmethod
    def _http_bridge_startup_keepalive_grace_seconds() -> float: ...

    @staticmethod
    def _http_bridge_recovery_heartbeat_seconds() -> float: ...

    async def _durable_account_binding(
        self,
        durable_lookup: DurableBridgeLookup | None,
        *,
        request_model: str | None,
    ) -> _DurableAccountBinding: ...

    async def _http_bridge_has_live_local_session(
        self,
        *,
        key: _HTTPBridgeSessionKey,
        incoming_turn_state: str | None,
        api_key: ApiKeyData | None,
    ) -> bool: ...

    async def _http_bridge_can_forward_to_active_owner(self, durable_lookup: DurableBridgeLookup) -> bool: ...

    async def _http_bridge_local_owner_account_id(
        self,
        *,
        key: _HTTPBridgeSessionKey,
        incoming_turn_state: str | None,
        previous_response_id: str,
        api_key: ApiKeyData | None,
        request_model: str | None,
    ) -> str | None: ...

    async def _resolve_websocket_previous_response_owner(
        self,
        *,
        previous_response_id: str | None,
        api_key: ApiKeyData | None,
        session_id: str | None = None,
        surface: str,
    ) -> str | None: ...

    @overload
    async def _get_or_create_http_bridge_session(
        self,
        key: _HTTPBridgeSessionKey,
        *,
        headers: dict[str, str],
        affinity: _AffinityPolicy,
        api_key: ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        max_sessions: int,
        previous_response_id: str | None = None,
        gateway_safe_mode: bool = False,
        allow_forward_to_owner: Literal[False] = False,
        forwarded_request: bool = False,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        allow_previous_response_recovery_rebind: bool = False,
        allow_bootstrap_owner_rebind: bool = False,
        durable_lookup: DurableBridgeLookup | None = None,
        durable_account_supports_request_model: bool = True,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
        trace_request_id: str | None = None,
    ) -> _HTTPBridgeSession: ...

    @overload
    async def _get_or_create_http_bridge_session(
        self,
        key: _HTTPBridgeSessionKey,
        *,
        headers: dict[str, str],
        affinity: _AffinityPolicy,
        api_key: ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        max_sessions: int,
        previous_response_id: str | None = None,
        gateway_safe_mode: bool = False,
        allow_forward_to_owner: Literal[True],
        forwarded_request: bool = False,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        allow_previous_response_recovery_rebind: bool = False,
        allow_bootstrap_owner_rebind: bool = False,
        durable_lookup: DurableBridgeLookup | None = None,
        durable_account_supports_request_model: bool = True,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
        trace_request_id: str | None = None,
    ) -> _HTTPBridgeSession | _HTTPBridgeOwnerForward: ...

    def _forward_http_bridge_request_to_owner(
        self,
        *,
        owner_forward: _HTTPBridgeOwnerForward,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        api_key_reservation: ApiKeyUsageReservationData | None,
        codex_session_affinity: bool,
        downstream_turn_state: str | None,
        request_started_at: float,
        proxy_api_authorization: str | None,
    ) -> AsyncIterator[str]: ...

    def _prepare_http_bridge_request(
        self,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        request_id: str | None = None,
    ) -> tuple[_WebSocketRequestState, str]: ...

    async def _submit_http_bridge_request(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        release_submit_lease: bool = True,
    ) -> None: ...

    async def _register_http_bridge_turn_state(
        self,
        session: _HTTPBridgeSession,
        turn_state: str | None,
    ) -> None: ...

    async def _detach_http_bridge_request(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
    ) -> bool: ...

    async def _reserve_websocket_api_key_usage(
        self,
        api_key: ApiKeyData | None,
        *,
        request_model: str | None,
        request_service_tier: str | None,
    ) -> ApiKeyUsageReservationData | None: ...

    async def _release_websocket_reservation(
        self,
        reservation: ApiKeyUsageReservationData | None,
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

    async def _close_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
    ) -> None: ...

    async def _reset_http_bridge_session_after_local_terminal_error(
        self,
        session: _HTTPBridgeSession,
        *,
        error_code: str,
        error_message: str,
    ) -> None: ...

    def _stream_http_bridge_session_events(
        self,
        session: _HTTPBridgeSession,
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ) -> AsyncGenerator[str, None]: ...


class _HTTPBridgeStreamMixin:
    async def _stream_via_http_bridge(
        self: _HTTPBridgeStreamService,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool,
        propagate_http_errors: bool,
        openai_cache_affinity: bool,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        suppress_text_done_events: bool,
        idle_ttl_seconds: float,
        codex_idle_ttl_seconds: float,
        max_sessions: int,
        queue_limit: int,
        prompt_cache_idle_ttl_seconds: float | None = None,
        downstream_turn_state: str | None = None,
        forwarded_request: bool = False,
        proxy_api_authorization: str | None = None,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
    ) -> AsyncIterator[str]:
        del suppress_text_done_events
        request_id = ensure_request_id()
        dashboard_settings = await self._http_bridge_dashboard_settings()
        runtime_config = _http_bridge_runtime_config(dashboard_settings, self._http_bridge_runtime_settings())
        incoming_turn_state_header = _sticky_key_from_turn_state_header(headers) if not forwarded_request else None
        incoming_session_header = _sticky_key_from_session_header(headers) if not forwarded_request else None
        had_prompt_cache_key = _prompt_cache_key_from_request_model(payload) is not None
        affinity = _sticky_key_for_responses_request(
            payload,
            headers,
            codex_session_affinity=codex_session_affinity,
            openai_cache_affinity=openai_cache_affinity,
            openai_cache_affinity_max_age_seconds=dashboard_settings.openai_cache_affinity_max_age_seconds,
            sticky_threads_enabled=dashboard_settings.sticky_threads_enabled,
            settings=self._http_bridge_runtime_settings(),
            api_key=api_key,
        )
        sticky_key_source = "none"
        if affinity.kind == StickySessionKind.CODEX_SESSION:
            sticky_key_source = (
                "turn_state_header" if _sticky_key_from_turn_state_header(headers) is not None else "session_header"
            )
        elif affinity.key:
            sticky_key_source = "payload" if had_prompt_cache_key else "derived"
        _maybe_log_proxy_request_shape(
            "stream_http_bridge",
            payload,
            headers,
            sticky_kind=affinity.kind.value if affinity.kind is not None else None,
            sticky_key_source=sticky_key_source,
            prompt_cache_key_set=_prompt_cache_key_from_request_model(payload) is not None,
            settings=self._http_bridge_runtime_settings(),
        )

        bridge_session_key = _make_http_bridge_session_key(
            payload,
            headers=headers,
            affinity=affinity,
            api_key=api_key,
            request_id=request_id,
            allow_forwarded_affinity_headers=forwarded_request,
            forwarded_affinity_kind=forwarded_affinity_kind,
            forwarded_affinity_key=forwarded_affinity_key,
        )
        try:
            durable_lookup = await self._durable_bridge.lookup_request_targets(
                session_key_kind=bridge_session_key.affinity_kind,
                session_key_value=bridge_session_key.affinity_key,
                api_key_id=bridge_session_key.api_key_id,
                turn_state=incoming_turn_state_header,
                session_header=incoming_session_header,
                previous_response_id=payload.previous_response_id,
            )
        except Exception:
            logger.warning("Durable bridge lookup failed; falling back to non-durable request handling", exc_info=True)
            durable_lookup = None
        durable_account_binding = await self._durable_account_binding(
            durable_lookup,
            request_model=payload.model,
        )
        effective_payload = payload
        proxy_injected_previous_response_id = False
        fresh_upstream_request_text: str | None = None
        durable_full_resend_anchor_count: int | None = None
        durable_full_resend_anchor_fingerprint: str | None = None
        if durable_lookup is not None:
            bridge_session_key = _HTTPBridgeSessionKey(
                durable_lookup.canonical_kind,
                durable_lookup.canonical_key,
                bridge_session_key.api_key_id,
            )
            live_local_session_exists = await self._http_bridge_has_live_local_session(
                key=bridge_session_key,
                incoming_turn_state=incoming_turn_state_header,
                api_key=api_key,
            )
            forwards_to_active_owner = await self._http_bridge_can_forward_to_active_owner(durable_lookup)
            durable_anchor_trimmable = _input_prefix_matches_stored_context(
                payload.input,
                stored_count=durable_lookup.latest_input_item_count or 0,
                stored_fingerprint=durable_lookup.latest_input_full_fingerprint,
            )
            if (
                not live_local_session_exists
                and not forwards_to_active_owner
                and payload.previous_response_id is None
                and bridge_session_key.strength == "hard"
                and durable_lookup.latest_response_id is not None
                and durable_account_binding.supports_request_model
                and (not _http_bridge_payload_looks_like_full_resend(payload) or durable_anchor_trimmable)
            ):
                effective_payload = payload.model_copy(
                    update={"previous_response_id": durable_lookup.latest_response_id}
                )
                proxy_injected_previous_response_id = True
                _fresh_request_state, fresh_upstream_request_text = self._prepare_http_bridge_request(
                    payload,
                    headers,
                    api_key=api_key,
                    api_key_reservation=api_key_reservation,
                    request_id=request_id,
                )
                del _fresh_request_state
                _log_http_bridge_event(
                    "fresh_reattach_anchor_injected",
                    bridge_session_key,
                    account_id=None,
                    model=payload.model,
                    detail=f"response_id={durable_lookup.latest_response_id}",
                    cache_key_family=bridge_session_key.affinity_kind,
                    model_class=_extract_model_class(payload.model) if payload.model else None,
                )
                if _http_bridge_payload_looks_like_full_resend(payload):
                    durable_full_resend_anchor_count = durable_lookup.latest_input_item_count
                    durable_full_resend_anchor_fingerprint = durable_lookup.latest_input_full_fingerprint
                    _log_http_bridge_event(
                        "durable_full_resend_anchor_injected",
                        bridge_session_key,
                        account_id=None,
                        model=payload.model,
                        detail=(
                            f"response_id={durable_lookup.latest_response_id} "
                            f"stored_items={durable_full_resend_anchor_count}"
                        ),
                        cache_key_family=bridge_session_key.affinity_kind,
                        model_class=_extract_model_class(payload.model) if payload.model else None,
                    )
        request_state, text_data = self._prepare_http_bridge_request(
            effective_payload,
            headers,
            api_key=api_key,
            api_key_reservation=api_key_reservation,
            request_id=request_id,
        )
        if downstream_turn_state is not None:
            request_state.session_id = _normalize_session_id(downstream_turn_state)
        request_state.transport = _REQUEST_TRANSPORT_HTTP
        request_state.request_stage = _http_bridge_request_stage(
            headers=headers,
            payload=effective_payload,
            durable_lookup=durable_lookup,
        )
        request_state.preferred_account_id = (
            durable_account_binding.account_id
            if (
                durable_lookup is not None
                and durable_account_binding.supports_request_model
                and (
                    request_state.previous_response_id is not None
                    or bridge_session_key.strength == "hard"
                    or (
                        bridge_session_key.affinity_kind == "prompt_cache"
                        and request_state.request_stage == "follow_up"
                        and durable_lookup.latest_turn_state is not None
                    )
                )
            )
            else request_state.preferred_account_id
        )
        if request_state.previous_response_id is not None and request_state.preferred_account_id is None:
            request_state.http_bridge_local_owner_lookup_started_at = time.monotonic()
            request_state.preferred_account_id = await self._http_bridge_local_owner_account_id(
                key=bridge_session_key,
                incoming_turn_state=incoming_turn_state_header,
                previous_response_id=request_state.previous_response_id,
                api_key=api_key,
                request_model=effective_payload.model,
            )
            request_state.http_bridge_local_owner_lookup_completed_at = time.monotonic()
        if request_state.previous_response_id is not None and request_state.preferred_account_id is None:
            request_state.http_bridge_previous_owner_resolve_started_at = time.monotonic()
            request_state.preferred_account_id = await self._resolve_websocket_previous_response_owner(
                previous_response_id=request_state.previous_response_id,
                api_key=api_key,
                session_id=request_state.session_id,
                surface="http_bridge",
            )
            request_state.http_bridge_previous_owner_resolve_completed_at = time.monotonic()
        if proxy_injected_previous_response_id:
            request_state.proxy_injected_previous_response_id = True
            request_state.fresh_upstream_request_text = fresh_upstream_request_text or text_data
            # Durable-anchor injection actually runs when the incoming
            # payload is *not* a full resend (see the
            # ``not _http_bridge_payload_looks_like_full_resend(payload)``
            # guard above), so the captured unanchored text is typically
            # just a short follow-up. Replaying it as a fresh turn would
            # drop the conversational context the anchor was pointing at.
            # Only the trim branch below (which verifies the stored prefix
            # fingerprint) is allowed to flip this flag to ``True``.
            request_state.fresh_upstream_request_is_retry_safe = False
        request_state.http_bridge_get_or_create_started_at = time.monotonic()
        session_or_forward_task: asyncio.Task[_HTTPBridgeSession | _HTTPBridgeOwnerForward] | None = None
        try:
            session_or_forward_task = asyncio.create_task(
                self._get_or_create_http_bridge_session(
                    bridge_session_key,
                    headers=dict(headers),
                    affinity=affinity,
                    api_key=api_key,
                    request_model=effective_payload.model,
                    idle_ttl_seconds=_effective_http_bridge_idle_ttl_seconds(
                        affinity=affinity,
                        idle_ttl_seconds=idle_ttl_seconds,
                        codex_idle_ttl_seconds=codex_idle_ttl_seconds,
                        prompt_cache_idle_ttl_seconds=prompt_cache_idle_ttl_seconds,
                    ),
                    max_sessions=max_sessions,
                    previous_response_id=request_state.previous_response_id,
                    gateway_safe_mode=runtime_config.gateway_safe_mode,
                    allow_forward_to_owner=True,
                    forwarded_request=forwarded_request,
                    forwarded_affinity_kind=forwarded_affinity_kind,
                    forwarded_affinity_key=forwarded_affinity_key,
                    durable_lookup=durable_lookup,
                    durable_account_supports_request_model=durable_account_binding.supports_request_model,
                    request_stage=request_state.request_stage,
                    preferred_account_id=request_state.preferred_account_id,
                    trace_request_id=request_state.request_id,
                )
            )
            keepalive_sent = False
            keepalive_count = 0
            while True:
                try:
                    session_or_forward = await asyncio.wait_for(
                        asyncio.shield(session_or_forward_task),
                        timeout=_http_bridge_keepalive_wait_timeout_seconds(
                            yielded_any=False,
                            keepalive_sent=keepalive_sent,
                            startup_grace_seconds=self._http_bridge_startup_keepalive_grace_seconds(),
                            heartbeat_seconds=self._http_bridge_recovery_heartbeat_seconds(),
                        ),
                    )
                    break
                except TimeoutError:
                    keepalive_count += 1
                    if keepalive_count > _http_bridge_keepalive_max_count(
                        self._http_bridge_runtime_settings(),
                        heartbeat_seconds=self._http_bridge_recovery_heartbeat_seconds(),
                    ):
                        raise ProxyResponseError(
                            504,
                            openai_error(
                                "stream_idle_timeout",
                                "HTTP bridge session creation did not complete within the keepalive window",
                            ),
                        )
                    keepalive_sent = True
                    yield _http_bridge_keepalive_frame(
                        request_state,
                        request_id=request_state.request_id,
                        reason="waiting for HTTP bridge session capacity",
                        retry_after_seconds=self._http_bridge_recovery_heartbeat_seconds(),
                        started_at=request_state.http_bridge_get_or_create_started_at,
                    )
        finally:
            if session_or_forward_task is not None and not session_or_forward_task.done():
                await _await_cancelled_task(session_or_forward_task, label="HTTP bridge session creation")
            request_state.http_bridge_get_or_create_completed_at = time.monotonic()
        if isinstance(session_or_forward, _HTTPBridgeOwnerForward):
            forwarded_any = False
            try:
                async for line in self._forward_http_bridge_request_to_owner(
                    owner_forward=session_or_forward,
                    payload=effective_payload,
                    headers=headers,
                    api_key_reservation=api_key_reservation,
                    codex_session_affinity=codex_session_affinity,
                    downstream_turn_state=downstream_turn_state,
                    request_started_at=request_state.started_at,
                    proxy_api_authorization=proxy_api_authorization,
                ):
                    forwarded_any = True
                    yield line
                return
            except ProxyResponseError as exc:
                if forwarded_any:
                    raise
                should_attempt_previous_response_recovery = (
                    effective_payload.previous_response_id is not None
                    and _http_bridge_should_attempt_local_previous_response_recovery(exc)
                )
                should_attempt_bootstrap_rebind = _http_bridge_should_attempt_local_bootstrap_rebind(
                    exc,
                    key=bridge_session_key,
                    headers=headers,
                    previous_response_id=effective_payload.previous_response_id,
                )
                if not should_attempt_previous_response_recovery and not should_attempt_bootstrap_rebind:
                    raise
                if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                    bridge_durable_recover_total.labels(
                        path="owner_forward_fail"
                        if should_attempt_previous_response_recovery
                        else "owner_forward_bootstrap"
                    ).inc()
                _log_http_bridge_event(
                    "previous_response_recover_local"
                    if should_attempt_previous_response_recovery
                    else "bootstrap_rebind_local",
                    bridge_session_key,
                    account_id=None,
                    model=effective_payload.model,
                    detail=(
                        "outcome=local_rebind_after_forward_failure"
                        if should_attempt_previous_response_recovery
                        else "outcome=local_bootstrap_after_forward_failure"
                    ),
                    cache_key_family=bridge_session_key.affinity_kind,
                    model_class=_extract_model_class(effective_payload.model) if effective_payload.model else None,
                    owner_check_applied=True,
                )
                session = await self._get_or_create_http_bridge_session(
                    bridge_session_key,
                    headers=dict(headers),
                    affinity=affinity,
                    api_key=api_key,
                    request_model=effective_payload.model,
                    idle_ttl_seconds=_effective_http_bridge_idle_ttl_seconds(
                        affinity=affinity,
                        idle_ttl_seconds=idle_ttl_seconds,
                        codex_idle_ttl_seconds=codex_idle_ttl_seconds,
                        prompt_cache_idle_ttl_seconds=prompt_cache_idle_ttl_seconds,
                    ),
                    max_sessions=max_sessions,
                    previous_response_id=request_state.previous_response_id,
                    gateway_safe_mode=runtime_config.gateway_safe_mode,
                    allow_forward_to_owner=False,
                    forwarded_request=False,
                    allow_previous_response_recovery_rebind=should_attempt_previous_response_recovery,
                    allow_bootstrap_owner_rebind=should_attempt_bootstrap_rebind,
                    durable_lookup=durable_lookup,
                    durable_account_supports_request_model=durable_account_binding.supports_request_model,
                    request_stage="reattach",
                    preferred_account_id=request_state.preferred_account_id,
                )
                _record_bridge_reattach(
                    path="owner_forward_fail"
                    if should_attempt_previous_response_recovery
                    else "owner_forward_bootstrap",
                    outcome="success",
                )
                retry_request_state: _WebSocketRequestState | None = None
                try:
                    retry_api_key_reservation = api_key_reservation
                    retry_reservation_reacquired = False
                    if api_key is not None and api_key_reservation is not None:
                        retry_api_key_reservation = await self._reserve_websocket_api_key_usage(
                            api_key,
                            request_model=effective_payload.model,
                            request_service_tier=_normalize_service_tier_value(
                                dict(effective_payload.to_payload()).get("service_tier"),
                            ),
                        )
                        retry_reservation_reacquired = True

                    retry_request_state, retry_text_data = self._prepare_http_bridge_request(
                        effective_payload,
                        headers,
                        api_key=api_key,
                        api_key_reservation=retry_api_key_reservation,
                        request_id=request_id,
                    )
                    if downstream_turn_state is not None:
                        retry_request_state.session_id = _normalize_session_id(downstream_turn_state)
                    retry_request_state.transport = _REQUEST_TRANSPORT_HTTP
                    retry_request_state.request_stage = "reattach"
                    retry_request_state.preferred_account_id = request_state.preferred_account_id
                    _set_request_budget(
                        retry_request_state,
                        self._http_bridge_runtime_settings().proxy_reconnect_request_budget_seconds,
                    )

                    await self._submit_http_bridge_request(
                        session,
                        request_state=retry_request_state,
                        text_data=retry_text_data,
                        queue_limit=queue_limit,
                        release_submit_lease=True,
                    )
                    if downstream_turn_state is not None:
                        await self._register_http_bridge_turn_state(session, downstream_turn_state)
                    event_queue = retry_request_state.event_queue
                    assert event_queue is not None
                    while True:
                        event_block = await event_queue.get()
                        if event_block is None:
                            break
                        downstream_event_at = time.monotonic()
                        if retry_request_state.http_bridge_downstream_first_event_at is None:
                            retry_request_state.http_bridge_downstream_first_event_at = downstream_event_at
                        block_payload = parse_sse_data_json(event_block)
                        block_event_type = _event_type_from_payload(None, block_payload)
                        if (
                            retry_request_state.latency_first_token_ms is None
                            and block_event_type in _TEXT_DELTA_EVENT_TYPES
                        ):
                            retry_request_state.latency_first_token_ms = int(
                                (downstream_event_at - retry_request_state.started_at) * 1000
                            )
                            retry_request_state.http_bridge_downstream_first_text_at = downstream_event_at
                            _log_http_bridge_latency_breakdown(
                                session,
                                retry_request_state,
                                event_type=block_event_type,
                            )
                        yield event_block
                except BaseException:
                    if retry_reservation_reacquired and retry_api_key_reservation is not None:
                        await self._release_websocket_reservation(retry_api_key_reservation)
                    raise
                finally:
                    if retry_request_state is not None:
                        with anyio.CancelScope(shield=True):
                            await self._detach_http_bridge_request(session, request_state=retry_request_state)
                            session.last_used_at = time.monotonic()
                return
        session = session_or_forward
        if (
            durable_full_resend_anchor_count is not None
            and durable_full_resend_anchor_fingerprint is not None
            and durable_lookup is not None
            and durable_lookup.latest_response_id is not None
        ):
            session.last_completed_response_id = durable_lookup.latest_response_id
            session.last_completed_input_count = durable_full_resend_anchor_count
            session.last_completed_input_prefix_fingerprint = durable_full_resend_anchor_fingerprint
        # --- Session-level previous_response_id injection ---
        # If the client didn't send previous_response_id and the durable
        # lookup didn't inject one, but this bridge session is carrying
        # Codex-style conversational continuity and has already completed a
        # request on this logical conversation, inject the session's last
        # completed response ID so the trim branch below can strip the
        # already-stored prefix.
        #
        # Correctness guards:
        # - Soft affinity reuse (for example prompt cache / sticky-thread
        #   sharing) must stay self-contained, so only true Codex
        #   continuity sessions opt in.
        # - Injecting an anchor when the incoming payload is a full-resend
        #   whose prefix cannot be safely trimmed (non-list input, prefix
        #   mismatch, or shorter-than-stored history) would send both the
        #   full history *and* the anchor upstream, which duplicates
        #   context and distorts output/cost. Gate injection so it only
        #   fires when the trim branch below would actually succeed.
        incoming_input_preview = effective_payload.input
        stored_count_preview = session.last_completed_input_count
        stored_fingerprint_preview = session.last_completed_input_prefix_fingerprint
        session_anchor_trimmable = _input_prefix_matches_stored_context(
            incoming_input_preview,
            stored_count=stored_count_preview,
            stored_fingerprint=stored_fingerprint_preview,
        )
        if (
            session.codex_session
            and not proxy_injected_previous_response_id
            and effective_payload.previous_response_id is None
            and session.last_completed_response_id is not None
            and session_anchor_trimmable
        ):
            fresh_upstream_request_text = text_data
            effective_payload = effective_payload.model_copy(
                update={"previous_response_id": session.last_completed_response_id}
            )
            proxy_injected_previous_response_id = True
            request_state, text_data = self._prepare_http_bridge_request(
                effective_payload,
                headers,
                api_key=api_key,
                api_key_reservation=api_key_reservation,
                request_id=request_id,
            )
            request_state.transport = _REQUEST_TRANSPORT_HTTP
            request_state.request_stage = _http_bridge_request_stage(
                headers=headers,
                payload=effective_payload,
                durable_lookup=durable_lookup,
            )
            request_state.preferred_account_id = (
                durable_account_binding.account_id if durable_account_binding.supports_request_model else None
            )
            request_state.proxy_injected_previous_response_id = True
            request_state.fresh_upstream_request_text = fresh_upstream_request_text
            # Session-level anchor injection may be attached to a payload
            # that relied on the anchor for context (for example a
            # single-item follow-up turn whose prior history is only
            # represented by ``previous_response_id``). Replaying without
            # the anchor would silently turn it into a fresh turn and drop
            # conversational context, so opt this path out of fresh-upstream
            # fresh-turn replay.
            request_state.fresh_upstream_request_is_retry_safe = False
            logger.info(
                "session_anchor_injected request_id=%s response_id=%s",
                request_id,
                session.last_completed_response_id,
            )
        # Trim already-stored prefix when previous_response_id anchors context.
        has_previous_response_id = (
            proxy_injected_previous_response_id or effective_payload.previous_response_id is not None
        )
        incoming_input = effective_payload.input
        stored_count = session.last_completed_input_count
        stored_fingerprint = session.last_completed_input_prefix_fingerprint
        if (
            has_previous_response_id
            and stored_count > 0
            and stored_fingerprint is not None
            and isinstance(incoming_input, list)
            and len(incoming_input) > stored_count
        ):
            incoming_input_list = cast(list[JsonValue], incoming_input)
            incoming_prefix_fingerprint = _fingerprint_input_items(incoming_input_list[:stored_count])
            if incoming_prefix_fingerprint == stored_fingerprint:
                original_count = len(incoming_input_list)
                trimmed_input = incoming_input_list[stored_count:]
                trimmed_payload = effective_payload.model_copy(update={"input": trimmed_input})
                previous_preferred_account_id = request_state.preferred_account_id
                request_state, text_data = self._prepare_http_bridge_request(
                    trimmed_payload,
                    headers,
                    api_key=api_key,
                    api_key_reservation=api_key_reservation,
                    request_id=request_id,
                )
                if downstream_turn_state is not None:
                    request_state.session_id = _normalize_session_id(downstream_turn_state)
                request_state.transport = _REQUEST_TRANSPORT_HTTP
                request_state.request_stage = _http_bridge_request_stage(
                    headers=headers,
                    payload=trimmed_payload,
                    durable_lookup=durable_lookup,
                )
                request_state.preferred_account_id = previous_preferred_account_id
                request_state.input_item_count = original_count
                request_state.input_full_fingerprint = _fingerprint_input_items(incoming_input_list)
                if proxy_injected_previous_response_id:
                    request_state.proxy_injected_previous_response_id = True
                    request_state.fresh_upstream_request_text = fresh_upstream_request_text
                    # The trim branch only fires when the untrimmed payload
                    # is a true full resend whose prefix exactly matches the
                    # already-stored context, so the unanchored request text
                    # is a safe fresh-turn replay target regardless of
                    # whether the anchor came from the durable or
                    # session-level injection path.
                    request_state.fresh_upstream_request_is_retry_safe = True
                logger.info(
                    "store_context_input_trimmed request_id=%s original_items=%s trimmed_to=%s previous_response_id=%s",
                    request_id,
                    original_count,
                    len(trimmed_input),
                    effective_payload.previous_response_id,
                )
            else:
                logger.warning(
                    "store_context_input_trim_skipped_prefix_mismatch request_id=%s incoming_items=%s "
                    "stored_items=%s previous_response_id=%s",
                    request_id,
                    len(incoming_input_list),
                    stored_count,
                    effective_payload.previous_response_id,
                )
        session_events: AsyncGenerator[str, None] = self._stream_http_bridge_session_events(
            session,
            request_state=request_state,
            text_data=text_data,
            queue_limit=queue_limit,
            propagate_http_errors=propagate_http_errors,
            downstream_turn_state=downstream_turn_state,
            submit_lease_held=True,
        )
        try:
            async for event_block in session_events:
                yield event_block
        except ProxyResponseError as exc:
            is_context_overflow = _http_bridge_is_context_overflow_error(exc)
            should_rollover_after_context_overflow = _http_bridge_should_rollover_after_context_overflow(
                exc,
                key=bridge_session_key,
            )
            should_attempt_previous_response_recovery = (
                effective_payload.previous_response_id is not None
                and _http_bridge_should_attempt_local_previous_response_recovery(exc)
            )
            should_attempt_context_overflow_fresh_turn_recovery = (
                is_context_overflow
                and effective_payload.previous_response_id is not None
                and bridge_session_key.strength != "hard"
            )
            if (
                not should_attempt_previous_response_recovery
                and not should_rollover_after_context_overflow
                and not should_attempt_context_overflow_fresh_turn_recovery
            ):
                if is_context_overflow:
                    _log_http_bridge_event(
                        "context_overflow_no_rollover",
                        bridge_session_key,
                        account_id=None,
                        model=effective_payload.model,
                        detail="outcome=preserve_hard_affinity_session",
                        cache_key_family=bridge_session_key.affinity_kind,
                        model_class=_extract_model_class(effective_payload.model) if effective_payload.model else None,
                        owner_check_applied=True,
                    )
                raise

            if should_attempt_context_overflow_fresh_turn_recovery:
                if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                    bridge_durable_recover_total.labels(path="context_overflow_fresh_turn").inc()
                _log_http_bridge_event(
                    "context_overflow_fresh_turn_recover",
                    bridge_session_key,
                    account_id=None,
                    model=effective_payload.model,
                    detail="outcome=retry_without_previous_response_id",
                    cache_key_family=bridge_session_key.affinity_kind,
                    model_class=_extract_model_class(effective_payload.model) if effective_payload.model else None,
                    owner_check_applied=True,
                )
                await self._reset_http_bridge_session_after_local_terminal_error(
                    session,
                    error_code="stream_incomplete",
                    error_message="Upstream websocket closed before response.completed",
                )
                recovery_path = "context_overflow_fresh_turn"
                retry_payload = _http_bridge_payload_without_previous_response_id(effective_payload)
                retry_previous_response_id = None
                retry_request_stage = "context_overflow_recover"
                retry_preferred_account_id = None
                allow_previous_response_recovery_rebind = False
            elif should_rollover_after_context_overflow:
                _log_http_bridge_event(
                    "context_overflow_rollover",
                    bridge_session_key,
                    account_id=None,
                    model=effective_payload.model,
                    detail="outcome=close_session_after_context_length_exceeded",
                    cache_key_family=bridge_session_key.affinity_kind,
                    model_class=_extract_model_class(effective_payload.model) if effective_payload.model else None,
                    owner_check_applied=True,
                )
                await self._reset_http_bridge_session_after_local_terminal_error(
                    session,
                    error_code="stream_incomplete",
                    error_message="Upstream websocket closed before response.completed",
                )
                raise
            else:
                if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                    bridge_durable_recover_total.labels(path="local_previous_response_error").inc()
                _log_http_bridge_event(
                    "previous_response_recover_local",
                    bridge_session_key,
                    account_id=None,
                    model=effective_payload.model,
                    detail="outcome=local_rebind_after_local_error",
                    cache_key_family=bridge_session_key.affinity_kind,
                    model_class=_extract_model_class(effective_payload.model) if effective_payload.model else None,
                    owner_check_applied=True,
                )
                await self._reset_http_bridge_session_after_local_terminal_error(
                    session,
                    error_code="stream_incomplete",
                    error_message="Upstream websocket closed before response.completed",
                )
                recovery_path = "local_previous_response_error"
                retry_payload = effective_payload
                retry_previous_response_id = request_state.previous_response_id
                retry_request_stage = "reattach"
                retry_preferred_account_id = request_state.preferred_account_id
                allow_previous_response_recovery_rebind = True

            session = await self._get_or_create_http_bridge_session(
                bridge_session_key,
                headers=dict(headers),
                affinity=affinity,
                api_key=api_key,
                request_model=retry_payload.model,
                idle_ttl_seconds=_effective_http_bridge_idle_ttl_seconds(
                    affinity=affinity,
                    idle_ttl_seconds=idle_ttl_seconds,
                    codex_idle_ttl_seconds=codex_idle_ttl_seconds,
                    prompt_cache_idle_ttl_seconds=prompt_cache_idle_ttl_seconds,
                ),
                max_sessions=max_sessions,
                previous_response_id=retry_previous_response_id,
                gateway_safe_mode=runtime_config.gateway_safe_mode,
                allow_forward_to_owner=False,
                forwarded_request=False,
                allow_previous_response_recovery_rebind=allow_previous_response_recovery_rebind,
                durable_lookup=durable_lookup,
                durable_account_supports_request_model=durable_account_binding.supports_request_model,
                request_stage=retry_request_stage,
                preferred_account_id=retry_preferred_account_id,
            )
            _record_bridge_reattach(path=recovery_path, outcome="success")

            try:
                retry_api_key_reservation = api_key_reservation
                retry_reservation_reacquired = False
                if api_key is not None and api_key_reservation is not None:
                    retry_api_key_reservation = await self._reserve_websocket_api_key_usage(
                        api_key,
                        request_model=retry_payload.model,
                        request_service_tier=_normalize_service_tier_value(
                            dict(retry_payload.to_payload()).get("service_tier"),
                        ),
                    )
                    retry_reservation_reacquired = True

                retry_request_state, retry_text_data = self._prepare_http_bridge_request(
                    retry_payload,
                    headers,
                    api_key=api_key,
                    api_key_reservation=retry_api_key_reservation,
                    request_id=request_id,
                )
                if downstream_turn_state is not None:
                    retry_request_state.session_id = _normalize_session_id(downstream_turn_state)
                retry_request_state.transport = _REQUEST_TRANSPORT_HTTP
                retry_request_state.request_stage = retry_request_stage
                retry_request_state.preferred_account_id = retry_preferred_account_id

                retry_events: AsyncGenerator[str, None] = self._stream_http_bridge_session_events(
                    session,
                    request_state=retry_request_state,
                    text_data=retry_text_data,
                    queue_limit=queue_limit,
                    propagate_http_errors=propagate_http_errors,
                    downstream_turn_state=downstream_turn_state,
                    submit_lease_held=False,
                )
                try:
                    async for event_block in retry_events:
                        yield event_block
                finally:
                    try:
                        await retry_events.aclose()
                    except Exception:
                        pass
            except BaseException:
                if retry_reservation_reacquired and retry_api_key_reservation is not None:
                    await self._release_websocket_reservation(retry_api_key_reservation)
                raise
        finally:
            try:
                await session_events.aclose()
            except Exception:
                pass

    async def _reset_http_bridge_session_after_local_terminal_error(
        self: _HTTPBridgeStreamService,
        session: "_HTTPBridgeSession",
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        async with self._http_bridge_lock:
            if self._http_bridge_sessions.get(session.key) is session:
                self._http_bridge_sessions.pop(session.key, None)
        async with session.pending_lock:
            session.queued_request_count = 0
        await self._fail_pending_websocket_requests(
            account_id_value=session.account.id,
            pending_requests=session.pending_requests,
            pending_lock=session.pending_lock,
            error_code=error_code,
            error_message=error_message,
            api_key=None,
            response_create_gate=session.response_create_gate,
        )
        await self._close_http_bridge_session(session)

    async def _stream_http_bridge_session_events(
        self: _HTTPBridgeStreamService,
        session: "_HTTPBridgeSession",
        *,
        request_state: _WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ) -> AsyncGenerator[str, None]:
        _set_request_budget(
            request_state,
            _http_bridge_request_budget_seconds(session, request_state, self._http_bridge_runtime_settings()),
        )
        await self._submit_http_bridge_request(
            session,
            request_state=request_state,
            text_data=text_data,
            queue_limit=queue_limit,
            release_submit_lease=submit_lease_held,
        )
        if downstream_turn_state is not None:
            await self._register_http_bridge_turn_state(session, downstream_turn_state)

        try:
            event_queue = request_state.event_queue
            assert event_queue is not None
            yielded_any = False
            keepalive_sent = False
            keepalive_count = 0
            while True:
                try:
                    event_block = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=_http_bridge_keepalive_wait_timeout_seconds(
                            yielded_any=yielded_any,
                            keepalive_sent=keepalive_sent,
                            startup_grace_seconds=self._http_bridge_startup_keepalive_grace_seconds(),
                            heartbeat_seconds=self._http_bridge_recovery_heartbeat_seconds(),
                        ),
                    )
                except TimeoutError:
                    keepalive_count += 1
                    if keepalive_count > _http_bridge_keepalive_max_count(
                        self._http_bridge_runtime_settings(),
                        heartbeat_seconds=self._http_bridge_recovery_heartbeat_seconds(),
                    ):
                        logger.info(
                            "HTTP bridge stream idle timeout request_id=%s keepalive_count=%s",
                            request_state.request_id,
                            keepalive_count,
                        )
                        yield format_sse_event(
                            response_failed_event(
                                "stream_idle_timeout",
                                "Upstream did not respond within the keepalive window",
                                response_id=request_state.response_id,
                            )
                        )
                        break
                    keepalive_sent = True
                    yielded_any = True
                    yield _http_bridge_keepalive_frame(
                        request_state,
                        request_id=request_state.request_id,
                        reason=request_state.account_capacity_wait_reason,
                        retry_after_seconds=request_state.account_capacity_wait_retry_after_seconds,
                        started_at=request_state.account_capacity_wait_started_at,
                    )
                    continue
                if event_block is None:
                    break
                keepalive_count = 0
                downstream_event_at = time.monotonic()
                if request_state.http_bridge_downstream_first_event_at is None:
                    request_state.http_bridge_downstream_first_event_at = downstream_event_at
                block_payload = parse_sse_data_json(event_block)
                block_event_type = _event_type_from_payload(None, block_payload)
                if request_state.latency_first_token_ms is None and block_event_type in _TEXT_DELTA_EVENT_TYPES:
                    request_state.latency_first_token_ms = int((downstream_event_at - request_state.started_at) * 1000)
                    request_state.http_bridge_downstream_first_text_at = downstream_event_at
                    _log_http_bridge_latency_breakdown(session, request_state, event_type=block_event_type)
                if (
                    not yielded_any
                    and propagate_http_errors
                    and block_event_type == "response.failed"
                    and request_state.error_http_status_override is not None
                    and request_state.error_http_status_override >= 400
                ):
                    raise ProxyResponseError(
                        request_state.error_http_status_override,
                        _openai_error_envelope_from_response_failed_payload(block_payload),
                    )
                yield event_block
                yielded_any = True
        finally:
            with anyio.CancelScope(shield=True):
                await self._detach_http_bridge_request(session, request_state=request_state)
                session.last_used_at = time.monotonic()
