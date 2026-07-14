from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import AsyncIterator, Literal, Mapping, cast

import aiohttp
import anyio

from app.core.auth.refresh import RefreshError as RefreshError
from app.core.balancer import DEFAULT_ROUTING_STRATEGY, PERMANENT_FAILURE_CODES, RoutingStrategy
from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.codex_search import search_codex as core_search_codex
from app.core.clients.proxy import (
    ProxyResponseError,
    filter_inbound_headers,
    pop_stream_timeout_overrides,
    push_stream_timeout_overrides,
)
from app.core.clients.proxy import compact_responses as core_compact_responses
from app.core.clients.proxy import stream_responses as core_stream_responses
from app.core.clients.proxy import transcribe_audio as core_transcribe_audio
from app.core.clients.proxy_websocket import (
    UpstreamResponsesWebSocket,
    connect_responses_websocket,
)
from app.core.config.settings import Settings, get_settings
from app.core.config.settings_cache import get_settings_cache
from app.core.crypto import TokenEncryptor
from app.core.errors import (
    OpenAIErrorEnvelope,
    openai_error,
    response_failed_event,
)
from app.core.exceptions import ProxyAuthError
from app.core.metrics.prometheus import (
    PROMETHEUS_AVAILABLE,
    bridge_forward_latency_seconds,
    bridge_owner_forward_total,
    continuity_fail_closed_total,
    continuity_owner_resolution_total,
)
from app.core.openai.codex_search import CodexSearchRequest, CodexSearchResponse
from app.core.openai.model_registry import ModelRegistry, get_model_registry
from app.core.openai.models import CompactResponsePayload, OpenAIEvent
from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest
from app.core.openai.response_create import (
    _RESPONSE_CREATE_IMAGE_OMISSION_NOTICE as _RESPONSE_CREATE_IMAGE_OMISSION_NOTICE,
)
from app.core.openai.response_create import (
    _RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE as _RESPONSE_CREATE_TOOL_OUTPUT_OMISSION_NOTICE,
)
from app.core.openai.response_create import _is_inline_image_reference as _is_inline_image_reference
from app.core.openai.response_create import _json_size_bytes as _json_size_bytes
from app.core.openai.response_create import (
    _json_value_contains_input_image_part as _json_value_contains_input_image_part,
)
from app.core.openai.response_create import (
    _response_create_inline_image_notice_item as _response_create_inline_image_notice_item,
)
from app.core.openai.response_create import (
    _response_create_inline_image_notice_part as _response_create_inline_image_notice_part,
)
from app.core.openai.response_create import (
    _response_create_recent_suffix_start as _response_create_recent_suffix_start,
)
from app.core.openai.response_create import (
    _response_create_too_large_error_envelope as _response_create_too_large_error_envelope,
)
from app.core.openai.response_create import (
    _responses_request_contains_input_image as _responses_request_contains_input_image,
)
from app.core.openai.response_create import (
    _responses_request_uses_image_generation as _responses_request_uses_image_generation,
)
from app.core.openai.response_create import _safe_dump_slug as _safe_dump_slug
from app.core.openai.response_create import (
    _should_dump_oversized_response_create as _should_dump_oversized_response_create,
)
from app.core.openai.response_create import (
    _should_slim_historical_tool_output as _should_slim_historical_tool_output,
)
from app.core.openai.response_create import (
    _slim_historical_response_content as _slim_historical_response_content,
)
from app.core.openai.response_create import (
    _slim_historical_response_content_part as _slim_historical_response_content_part,
)
from app.core.openai.response_create import (
    _slim_historical_response_input_item as _slim_historical_response_input_item,
)
from app.core.openai.response_create import (
    _slim_response_create_payload_for_upstream as _slim_response_create_payload_for_upstream,
)
from app.core.openai.response_create import (
    _summarize_response_create_input as _summarize_response_create_input,
)
from app.core.openai.response_create import (
    _summarize_response_create_payload as _summarize_response_create_payload,
)
from app.core.types import JsonValue
from app.core.utils.request_id import get_request_id
from app.core.utils.sse import format_sse_event
from app.core.utils.sse import parse_sse_data_json as parse_sse_data_json
from app.core.utils.time import utcnow as utcnow
from app.db.models import (
    ACCOUNT_PROVIDER_API_KEY as ACCOUNT_PROVIDER_API_KEY,
)
from app.db.models import (
    Account,
    AccountStatus,
    DashboardSettings,
    StickySessionKind,
)
from app.db.models import HttpBridgeSessionState as HttpBridgeSessionState
from app.db.session import SessionLocal
from app.modules.accounts.auth_manager import AuthManager as AuthManager
from app.modules.api_keys.service import (
    ApiKeyData,
    ApiKeyInvalidError,
    ApiKeysService,
    ApiKeyUsageReservationData,
)
from app.modules.proxy._service.account_freshness import _AccountFreshnessMixin
from app.modules.proxy._service.affinity import (
    _derive_prompt_cache_key as _derive_prompt_cache_key,
)
from app.modules.proxy._service.affinity import _extract_first_user_input as _extract_first_user_input
from app.modules.proxy._service.affinity import (
    _extract_model_class,
)
from app.modules.proxy._service.affinity import (
    _owner_lookup_session_id_from_headers as _owner_lookup_session_id_from_headers,
)
from app.modules.proxy._service.affinity import (
    _preferred_http_bridge_reconnect_turn_state as _preferred_http_bridge_reconnect_turn_state,
)
from app.modules.proxy._service.affinity import (
    _response_create_client_metadata as _response_create_client_metadata,
)
from app.modules.proxy._service.affinity import (
    _sticky_key_for_compact_request as _sticky_key_for_compact_request_impl,
)
from app.modules.proxy._service.affinity import (
    _sticky_key_for_responses_request as _sticky_key_for_responses_request_impl,
)
from app.modules.proxy._service.affinity import (
    _sticky_key_from_session_header as _sticky_key_from_session_header,
)
from app.modules.proxy._service.affinity import (
    build_downstream_turn_state_accept_headers as build_downstream_turn_state_accept_headers,
)
from app.modules.proxy._service.affinity import (
    build_downstream_turn_state_response_headers as build_downstream_turn_state_response_headers,
)
from app.modules.proxy._service.affinity import (
    ensure_downstream_turn_state as ensure_downstream_turn_state,
)
from app.modules.proxy._service.affinity import (
    ensure_http_downstream_turn_state as ensure_http_downstream_turn_state,
)
from app.modules.proxy._service.api_key_usage import _ApiKeyUsageRuntimeMixin
from app.modules.proxy._service.budget import (
    _raise_proxy_budget_exhausted,
    _remaining_budget_seconds,
)
from app.modules.proxy._service.compact import _CompactRuntimeMixin
from app.modules.proxy._service.concurrency import _ConcurrencyRuntimeMixin
from app.modules.proxy._service.continuity import (
    _WEBSOCKET_PREVIOUS_RESPONSE_ACCOUNT_CACHE_LIMIT as _WEBSOCKET_PREVIOUS_RESPONSE_ACCOUNT_CACHE_LIMIT,
)
from app.modules.proxy._service.continuity import _ContinuityRuntimeMixin
from app.modules.proxy._service.http_bridge.capacity import _HTTPBridgeCapacityMixin
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_busy_parallel_key as _http_bridge_busy_parallel_key,
)
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_previous_response_alias_key,
    _http_bridge_turn_state_alias_key,
)
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_soft_shard_key as _http_bridge_soft_shard_key,
)
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_soft_sharding_allowed as _http_bridge_soft_sharding_allowed,
)
from app.modules.proxy._service.http_bridge.keys import (
    _make_http_bridge_session_key as _make_http_bridge_session_key,
)
from app.modules.proxy._service.http_bridge.lifecycle import _HTTPBridgeLifecycleMixin
from app.modules.proxy._service.http_bridge.owner_resolution import _HTTPBridgeOwnerResolutionMixin
from app.modules.proxy._service.http_bridge.ownership import (
    _active_http_bridge_instance_ring,
    _durable_bridge_lookup_active_owner,
    _http_bridge_owner_instance,
    _http_bridge_should_wait_for_registration,
    _normalized_http_bridge_instance_ring,
)
from app.modules.proxy._service.http_bridge.ownership import (
    _http_bridge_can_recover_during_drain as _http_bridge_can_recover_during_drain,
)
from app.modules.proxy._service.http_bridge.ownership import (
    _http_bridge_owner_check_required as _http_bridge_owner_check_required,
)
from app.modules.proxy._service.http_bridge.ownership import (
    _http_bridge_requires_cluster_registration as _http_bridge_requires_cluster_registration,
)
from app.modules.proxy._service.http_bridge.policy import (
    _http_bridge_client_kind as _http_bridge_client_kind,
)
from app.modules.proxy._service.http_bridge.policy import (
    _http_bridge_session_allows_api_key,
)
from app.modules.proxy._service.http_bridge.policy import (
    _http_bridge_session_reusable_for_request as _policy_http_bridge_session_reusable_for_request,
)
from app.modules.proxy._service.http_bridge.policy import (
    _http_bridge_soft_prompt_cache_busy_parallel_allowed as _http_bridge_soft_prompt_cache_busy_parallel_allowed,
)
from app.modules.proxy._service.http_bridge.request_submit import (
    _build_http_bridge_prewarm_text as _build_http_bridge_prewarm_text,
)
from app.modules.proxy._service.http_bridge.request_submit import (
    _HTTPBridgeRequestSubmitMixin,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeAccountRuntimeSnapshot as HTTPBridgeAccountRuntimeSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRequestStatusSnapshot as HTTPBridgeRequestStatusSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeConfigSnapshot as HTTPBridgeRuntimeConfigSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeGroupSnapshot as HTTPBridgeRuntimeGroupSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeHealthHistoryBucket as HTTPBridgeRuntimeHealthHistoryBucket,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeHealthSnapshot as HTTPBridgeRuntimeHealthSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeSessionSnapshot as HTTPBridgeRuntimeSessionSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeShardFamilySnapshot as HTTPBridgeRuntimeShardFamilySnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeRuntimeSnapshot as HTTPBridgeRuntimeSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    UpstreamEgressRuntimeSnapshot as UpstreamEgressRuntimeSnapshot,
)
from app.modules.proxy._service.http_bridge.runtime import (
    _http_bridge_runtime_config as _http_bridge_runtime_config,
)
from app.modules.proxy._service.http_bridge.runtime import (
    _HTTPBridgePressureCapacityHint as _HTTPBridgePressureCapacityHint,
)
from app.modules.proxy._service.http_bridge.runtime_collection import _HTTPBridgeRuntimeCollectionMixin
from app.modules.proxy._service.http_bridge.session_acquire import _HTTPBridgeSessionAcquireMixin
from app.modules.proxy._service.http_bridge.session_create import _HTTPBridgeSessionCreateMixin
from app.modules.proxy._service.http_bridge.stream import (
    _await_http_bridge_startup_before_deadline,
    _http_bridge_post_accept_failure_frame,
    _http_bridge_startup_failure_frame,
    _HTTPBridgeStreamMixin,
    _HTTPBridgeStreamService,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _effective_http_bridge_idle_ttl_seconds as _effective_http_bridge_idle_ttl_seconds,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _fingerprint_input_items as _fingerprint_input_items,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_is_context_overflow_error as _http_bridge_is_context_overflow_error,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_payload_looks_like_full_resend as _http_bridge_payload_looks_like_full_resend,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_payload_without_previous_response_id as _http_bridge_payload_without_previous_response_id,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_request_stage as _http_bridge_request_stage,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_should_attempt_local_bootstrap_rebind as _http_bridge_should_attempt_local_bootstrap_rebind,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_should_attempt_local_previous_response_recovery as _stream_should_recover_previous_response,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _http_bridge_should_rollover_after_context_overflow as _http_bridge_should_rollover_after_context_overflow,
)
from app.modules.proxy._service.http_bridge.stream_policy import (
    _input_prefix_matches_stored_context as _input_prefix_matches_stored_context,
)
from app.modules.proxy._service.http_bridge.upstream_events import _HTTPBridgeUpstreamEventsMixin
from app.modules.proxy._service.observability import (
    _log_http_bridge_event,
)
from app.modules.proxy._service.observability import (
    _maybe_log_proxy_request_payload as _maybe_log_proxy_request_payload_impl,
)
from app.modules.proxy._service.observability import (
    _maybe_log_proxy_request_shape as _maybe_log_proxy_request_shape_impl,
)
from app.modules.proxy._service.observability import (
    _maybe_log_proxy_service_tier_trace as _maybe_log_proxy_service_tier_trace_impl,
)
from app.modules.proxy._service.observability import (
    _monotonic_age_ms as _monotonic_age_ms,
)
from app.modules.proxy._service.observability import (
    _record_continuity_fail_closed as _record_continuity_fail_closed_impl,
)
from app.modules.proxy._service.observability import (
    _record_continuity_owner_resolution as _record_continuity_owner_resolution_impl,
)
from app.modules.proxy._service.rate_limits import _RateLimitRuntimeMixin
from app.modules.proxy._service.request_logging import _RequestLoggingMixin
from app.modules.proxy._service.response_create import (
    _enforce_response_create_size_limit as _enforce_response_create_size_limit_impl,
)
from app.modules.proxy._service.response_create import (
    _maybe_dump_oversized_response_create_request as _maybe_dump_oversized_response_create_request_impl,
)
from app.modules.proxy._service.response_create import (
    _write_response_create_dump as _write_response_create_dump_impl,
)
from app.modules.proxy._service.response_create_runtime import _ResponseCreateRuntimeMixin
from app.modules.proxy._service.search import _SearchRuntimeMixin
from app.modules.proxy._service.service_tier import (
    _effective_service_tier as _effective_service_tier,
)
from app.modules.proxy._service.service_tier import (
    _http_bridge_text_with_account_service_tier as _http_bridge_text_with_account_service_tier,
)
from app.modules.proxy._service.service_tier import (
    _payload_with_account_service_tier as _payload_with_account_service_tier,
)
from app.modules.proxy._service.service_tier import (
    _service_tier_from_compact_payload as _service_tier_from_compact_payload,
)
from app.modules.proxy._service.service_tier import (
    _service_tier_from_response as _service_tier_from_response,
)
from app.modules.proxy._service.streaming import _StreamingMixin
from app.modules.proxy._service.support import (
    _ACCOUNT_SELECTION_RECOVERABLE_WAIT_CODE,
    _ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS,
    _REQUEST_TRANSPORT_HTTP,
    _AffinityPolicy,
    _await_cancelled_task,
    _await_operation_before_hard_timeout,
    _call_with_supported_optional_kwargs,
    _header_value_case_insensitive,
    _headers_with_authorization,
    _HTTPBridgeOwnerForward,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _is_account_neutral_error_code,
    _schedule_tracked_background_task,
    _supported_optional_kwargs,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy._service.support import (
    _DownstreamWebSocketActivity as _DownstreamWebSocketActivity,
)
from app.modules.proxy._service.support import _DurableAccountBinding as _DurableAccountBinding
from app.modules.proxy._service.support import (
    _event_type_from_payload as _event_type_from_payload,
)
from app.modules.proxy._service.support import (
    _release_websocket_response_create_gate as _release_websocket_response_create_gate,
)
from app.modules.proxy._service.support import (
    _should_retry_http_bridge_on_different_account as _should_retry_http_bridge_on_different_account,
)
from app.modules.proxy._service.transcription import _TranscriptionRuntimeMixin
from app.modules.proxy._service.upstream_account import (
    _websocket_disabled_for_account as _websocket_disabled_for_account,
)
from app.modules.proxy._service.upstream_websocket import _UpstreamWebSocketRuntimeMixin
from app.modules.proxy._service.websocket.connection import _WebSocketConnectionMixin
from app.modules.proxy._service.websocket.events import (
    _maybe_rewrite_websocket_previous_response_not_found_event as _maybe_rewrite_previous_response_impl,
)
from app.modules.proxy._service.websocket.events import (
    _message_mentions_previous_response_id as _message_mentions_previous_response_id,
)
from app.modules.proxy._service.websocket.events import (
    _rewrite_previous_response_stream_error as _rewrite_previous_response_stream_error_impl,
)
from app.modules.proxy._service.websocket.events import (
    _rewrite_websocket_previous_response_owner_unavailable_event as _rewrite_previous_response_owner_impl,
)
from app.modules.proxy._service.websocket.events import (
    _sanitize_websocket_connect_failure as _sanitize_websocket_connect_failure_impl,
)
from app.modules.proxy._service.websocket.orchestration import _WebSocketOrchestrationMixin
from app.modules.proxy._service.websocket.relay import (
    _websocket_receive_timeout_for_pending_requests as _websocket_receive_timeout_for_pending_requests,
)
from app.modules.proxy._service.websocket.relay import _WebSocketRelayMixin
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLimiter
from app.modules.proxy.durable_bridge_coordinator import (
    DurableBridgeLookup,
    DurableBridgeSessionCoordinator,
)
from app.modules.proxy.helpers import (
    _normalize_error_code,
    _parse_openai_error,
    _upstream_error_from_openai,
    classify_upstream_failure,
)
from app.modules.proxy.http_bridge_forwarding import (
    HTTP_BRIDGE_OWNER_ACCEPTED_EVENT_TYPE,
    HTTPBridgeForwardContext,
    HTTPBridgeOwnerClient,
    OwnerForwardRelayFailure,
    is_owner_forward_accepted_event,
)
from app.modules.proxy.load_balancer import AccountSelection, LoadBalancer
from app.modules.proxy.repo_bundle import ProxyRepoFactory
from app.modules.proxy.ring_membership import (
    RingMembershipService,
)
from app.modules.proxy.work_admission import WorkAdmissionController

_http_bridge_should_attempt_local_previous_response_recovery = _stream_should_recover_previous_response

logger = logging.getLogger(__name__)


def _sticky_key_for_responses_request(
    payload: ResponsesRequest,
    headers: Mapping[str, str],
    *,
    codex_session_affinity: bool,
    openai_cache_affinity: bool,
    openai_cache_affinity_max_age_seconds: int,
    sticky_threads_enabled: bool,
    api_key: ApiKeyData | None = None,
) -> _AffinityPolicy:
    return _sticky_key_for_responses_request_impl(
        payload,
        headers,
        codex_session_affinity=codex_session_affinity,
        openai_cache_affinity=openai_cache_affinity,
        openai_cache_affinity_max_age_seconds=openai_cache_affinity_max_age_seconds,
        sticky_threads_enabled=sticky_threads_enabled,
        api_key=api_key,
        settings=get_settings(),
    )


def _sticky_key_for_compact_request(
    payload: ResponsesCompactRequest,
    headers: Mapping[str, str],
    *,
    codex_session_affinity: bool,
    openai_cache_affinity: bool,
    openai_cache_affinity_max_age_seconds: int,
    sticky_threads_enabled: bool,
    api_key: ApiKeyData | None = None,
) -> _AffinityPolicy:
    return _sticky_key_for_compact_request_impl(
        payload,
        headers,
        codex_session_affinity=codex_session_affinity,
        openai_cache_affinity=openai_cache_affinity,
        openai_cache_affinity_max_age_seconds=openai_cache_affinity_max_age_seconds,
        sticky_threads_enabled=sticky_threads_enabled,
        api_key=api_key,
        settings=get_settings(),
    )


def _maybe_log_proxy_request_shape(
    kind: str,
    payload: ResponsesRequest | ResponsesCompactRequest,
    headers: Mapping[str, str],
    *,
    sticky_kind: str | None = None,
    sticky_key_source: str | None = None,
    prompt_cache_key_set: bool | None = None,
) -> None:
    _maybe_log_proxy_request_shape_impl(
        kind,
        payload,
        headers,
        settings=get_settings(),
        sticky_kind=sticky_kind,
        sticky_key_source=sticky_key_source,
        prompt_cache_key_set=prompt_cache_key_set,
    )


def _maybe_log_proxy_request_payload(
    kind: str,
    payload: ResponsesRequest | ResponsesCompactRequest,
    headers: Mapping[str, str],
) -> None:
    _maybe_log_proxy_request_payload_impl(kind, payload, headers, settings=get_settings())


def _maybe_log_proxy_service_tier_trace(
    kind: str,
    *,
    requested_service_tier: str | None,
    actual_service_tier: str | None,
) -> None:
    _maybe_log_proxy_service_tier_trace_impl(
        kind,
        requested_service_tier=requested_service_tier,
        actual_service_tier=actual_service_tier,
        settings=get_settings(),
    )


def _record_continuity_owner_resolution(
    *,
    surface: str,
    source: str,
    outcome: str,
    previous_response_id: str | None,
    session_id: str | None,
) -> None:
    _record_continuity_owner_resolution_impl(
        surface=surface,
        source=source,
        outcome=outcome,
        previous_response_id=previous_response_id,
        session_id=session_id,
        prometheus_available=PROMETHEUS_AVAILABLE,
        metric=continuity_owner_resolution_total,
    )


def _record_continuity_fail_closed(
    *,
    surface: str,
    reason: str,
    previous_response_id: str | None,
    session_id: str | None = None,
    upstream_error_code: str | None = None,
) -> None:
    _record_continuity_fail_closed_impl(
        surface=surface,
        reason=reason,
        previous_response_id=previous_response_id,
        session_id=session_id,
        upstream_error_code=upstream_error_code,
        prometheus_available=PROMETHEUS_AVAILABLE,
        metric=continuity_fail_closed_total,
    )


def _maybe_rewrite_websocket_previous_response_not_found_event(
    *,
    request_state: _WebSocketRequestState,
    event: OpenAIEvent | None,
    payload: dict[str, JsonValue] | None,
    event_type: str | None,
    upstream_control: _WebSocketUpstreamControl,
    original_text: str,
) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]:
    return _maybe_rewrite_previous_response_impl(
        request_state=request_state,
        event=event,
        payload=payload,
        event_type=event_type,
        upstream_control=upstream_control,
        original_text=original_text,
        record_continuity_fail_closed=_record_continuity_fail_closed,
    )


def _rewrite_websocket_previous_response_owner_unavailable_event(
    *,
    request_state: _WebSocketRequestState,
) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]:
    return _rewrite_previous_response_owner_impl(
        request_state=request_state,
        record_continuity_fail_closed=_record_continuity_fail_closed,
    )


def _sanitize_websocket_connect_failure(
    *,
    request_state: _WebSocketRequestState,
    status_code: int,
    payload: OpenAIErrorEnvelope,
    error_code: str,
    error_message: str,
) -> tuple[int, OpenAIErrorEnvelope, str, str]:
    return _sanitize_websocket_connect_failure_impl(
        request_state=request_state,
        status_code=status_code,
        payload=payload,
        error_code=error_code,
        error_message=error_message,
        record_continuity_fail_closed=_record_continuity_fail_closed,
    )


def _rewrite_previous_response_stream_error(
    *,
    previous_response_id: str | None,
    preferred_account_id: str | None,
    error_code: str | None,
    error_type: str | None,
    error_message: str | None,
    error_param: str | None,
) -> tuple[str, str, str | None] | None:
    return _rewrite_previous_response_stream_error_impl(
        previous_response_id=previous_response_id,
        preferred_account_id=preferred_account_id,
        error_code=error_code,
        error_type=error_type,
        error_message=error_message,
        error_param=error_param,
        record_continuity_fail_closed=_record_continuity_fail_closed,
    )


# Stay below the common 16 MiB websocket message ceiling so we can slim or fail
# early before upstream closes the session with 1009.
_UPSTREAM_RESPONSE_CREATE_WARN_BYTES = 12 * 1024 * 1024
_UPSTREAM_RESPONSE_CREATE_MAX_BYTES = 15 * 1024 * 1024
_OVERSIZED_RESPONSE_CREATE_DUMP_DIR = Path("/var/lib/codex-lb/debug/response-create-dumps")
_OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS = 10

_HTTP_BRIDGE_STARTUP_KEEPALIVE_GRACE_SECONDS = 0.5


_HTTP_BRIDGE_RETRYABLE_CONNECT_FORBIDDEN_CODES = frozenset(
    {"forbidden", "insufficient_permissions", "permission_error"}
)


def _http_bridge_session_reusable_for_request(
    *,
    session: _HTTPBridgeSession,
    key: _HTTPBridgeSessionKey,
    incoming_turn_state: str | None,
    previous_response_id: str | None,
    request_model: str | None,
    required_upstream_wire_api: str | None = None,
) -> bool:
    return _policy_http_bridge_session_reusable_for_request(
        session=session,
        key=key,
        incoming_turn_state=incoming_turn_state,
        previous_response_id=previous_response_id,
        request_model=request_model,
        required_upstream_wire_api=required_upstream_wire_api,
        model_registry=get_model_registry(),
    )


class ProxyService(
    _ApiKeyUsageRuntimeMixin,
    _AccountFreshnessMixin,
    _CompactRuntimeMixin,
    _SearchRuntimeMixin,
    _ResponseCreateRuntimeMixin,
    _UpstreamWebSocketRuntimeMixin,
    _RequestLoggingMixin,
    _RateLimitRuntimeMixin,
    _TranscriptionRuntimeMixin,
    _ConcurrencyRuntimeMixin,
    _ContinuityRuntimeMixin,
    _HTTPBridgeRuntimeCollectionMixin,
    _WebSocketOrchestrationMixin,
    _WebSocketRelayMixin,
    _WebSocketConnectionMixin,
    _StreamingMixin,
    _HTTPBridgeStreamMixin,
    _HTTPBridgeOwnerResolutionMixin,
    _HTTPBridgeSessionAcquireMixin,
    _HTTPBridgeSessionCreateMixin,
    _HTTPBridgeCapacityMixin,
    _HTTPBridgeLifecycleMixin,
    _HTTPBridgeUpstreamEventsMixin,
    _HTTPBridgeRequestSubmitMixin,
):
    def __init__(self, repo_factory: ProxyRepoFactory) -> None:
        self._repo_factory = repo_factory
        self._encryptor = TokenEncryptor()
        self._load_balancer = LoadBalancer(repo_factory)
        self._ring_membership = RingMembershipService(SessionLocal)
        self._durable_bridge = DurableBridgeSessionCoordinator(SessionLocal)
        self._proxy_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._http_bridge_owner_client = HTTPBridgeOwnerClient(self._proxy_cleanup_tasks)
        self._http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession] = {}
        self._http_bridge_inflight_sessions: dict[_HTTPBridgeSessionKey, asyncio.Future[_HTTPBridgeSession]] = {}
        self._http_bridge_turn_state_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey] = {}
        self._http_bridge_previous_response_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey] = {}
        self._websocket_previous_response_account_index: dict[tuple[str, str | None, str | None], str] = {}
        self._http_bridge_lock = anyio.Lock()
        self._http_bridge_background_close_tasks: set[asyncio.Task[None]] = set()
        self._search_background_tasks: set[asyncio.Task[None]] = set()
        self._account_model_concurrency = AccountModelConcurrencyLimiter()
        self._http_bridge_account_model_sessions = AccountModelConcurrencyLimiter()
        self._work_admission: WorkAdmissionController | None = None

    def _get_work_admission(self) -> WorkAdmissionController:
        if self._work_admission is None:
            settings = get_settings()
            self._work_admission = WorkAdmissionController(
                token_refresh_limit=settings.proxy_token_refresh_limit,
                websocket_connect_limit=settings.proxy_upstream_websocket_connect_limit,
                response_create_limit=settings.proxy_response_create_limit,
                compact_response_create_limit=settings.proxy_compact_response_create_limit,
                admission_wait_timeout_seconds=getattr(
                    settings,
                    "proxy_admission_wait_timeout_seconds",
                    10.0,
                ),
            )
        return self._work_admission

    @staticmethod
    def _http_bridge_codex_prewarm_enabled() -> bool:
        return bool(getattr(get_settings(), "http_responses_session_bridge_codex_prewarm_enabled", False))

    @staticmethod
    def _proxy_runtime_settings() -> Settings:
        return get_settings()

    @staticmethod
    async def _proxy_dashboard_settings() -> DashboardSettings:
        return await get_settings_cache().get()

    _http_bridge_runtime_settings = _proxy_runtime_settings
    _http_bridge_dashboard_settings = _proxy_dashboard_settings

    @staticmethod
    def _continuity_model_registry() -> ModelRegistry:
        return get_model_registry()

    @staticmethod
    def _record_continuity_owner_resolution_compatible(
        *,
        surface: str,
        source: str,
        outcome: str,
        previous_response_id: str | None,
        session_id: str | None,
    ) -> None:
        _record_continuity_owner_resolution(
            surface=surface,
            source=source,
            outcome=outcome,
            previous_response_id=previous_response_id,
            session_id=session_id,
        )

    @staticmethod
    def _record_continuity_fail_closed_compatible(
        *,
        surface: str,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None = None,
        upstream_error_code: str | None = None,
    ) -> None:
        _record_continuity_fail_closed(
            surface=surface,
            reason=reason,
            previous_response_id=previous_response_id,
            session_id=session_id,
            upstream_error_code=upstream_error_code,
        )

    @staticmethod
    async def _core_compact_responses_compatible(
        payload: ResponsesCompactRequest,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
    ) -> CompactResponsePayload:
        return cast(
            CompactResponsePayload,
            await _call_with_supported_optional_kwargs(
                core_compact_responses,
                payload,
                headers,
                access_token,
                account_id,
                optional_kwargs={"base_url": base_url, "wire_api": wire_api},
            ),
        )

    @staticmethod
    async def _core_search_codex_compatible(
        payload: CodexSearchRequest,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
        timeout_seconds: float,
    ) -> CodexSearchResponse:
        return await core_search_codex(
            payload,
            headers,
            access_token,
            account_id,
            base_url=base_url,
            wire_api=wire_api,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    async def _connect_responses_websocket_compatible(
        headers: dict[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
        cleanup_tasks: set[asyncio.Task[None]],
    ) -> UpstreamResponsesWebSocket:
        return cast(
            UpstreamResponsesWebSocket,
            await _call_with_supported_optional_kwargs(
                connect_responses_websocket,
                headers,
                access_token,
                account_id,
                optional_kwargs={
                    "cleanup_tasks": cleanup_tasks,
                    "wire_api": wire_api,
                },
                base_url=base_url,
            ),
        )

    def _core_stream_responses_compatible(
        self,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
        raise_for_status: bool,
        upstream_stream_transport_override: str | None = None,
    ) -> AsyncIterator[str]:
        optional_kwargs: dict[str, object] = {
            "wire_api": wire_api,
            "cleanup_tasks": self._proxy_cleanup_tasks,
        }
        if upstream_stream_transport_override is not None:
            optional_kwargs["upstream_stream_transport_override"] = upstream_stream_transport_override
        return core_stream_responses(
            payload,
            headers,
            access_token,
            account_id,
            **_supported_optional_kwargs(
                core_stream_responses,
                optional_kwargs,
                {"base_url": base_url, "raise_for_status": raise_for_status},
            ),
        )

    async def _core_transcribe_audio_compatible(
        self,
        audio_bytes: bytes,
        *,
        filename: str,
        content_type: str | None,
        prompt: str | None,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        base_url: str | None,
    ) -> dict[str, JsonValue]:
        return await core_transcribe_audio(
            audio_bytes,
            filename=filename,
            content_type=content_type,
            prompt=prompt,
            headers=headers,
            access_token=access_token,
            account_id=account_id,
            base_url=base_url,
        )

    @staticmethod
    def _remaining_budget_seconds_compatible(deadline: float) -> float:
        return _remaining_budget_seconds(deadline)

    @staticmethod
    def _push_stream_attempt_timeout_overrides_compatible(
        timeout_seconds: float,
    ) -> tuple[float | None, float | None, float | None]:
        return push_stream_timeout_overrides(
            connect_timeout_seconds=timeout_seconds,
            idle_timeout_seconds=timeout_seconds,
            total_timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _pop_stream_timeout_overrides_compatible(
        tokens: tuple[float | None, float | None, float | None],
    ) -> None:
        pop_stream_timeout_overrides(tokens)

    @staticmethod
    def _rewrite_previous_response_stream_error_compatible(
        *,
        previous_response_id: str | None,
        preferred_account_id: str | None,
        error_code: str | None,
        error_type: str | None,
        error_message: str | None,
        error_param: str | None,
    ) -> tuple[str, str, str | None] | None:
        return _rewrite_previous_response_stream_error(
            previous_response_id=previous_response_id,
            preferred_account_id=preferred_account_id,
            error_code=error_code,
            error_type=error_type,
            error_message=error_message,
            error_param=error_param,
        )

    @staticmethod
    def _maybe_rewrite_websocket_previous_response_not_found_event_compatible(
        *,
        request_state: _WebSocketRequestState,
        event: OpenAIEvent | None,
        payload: dict[str, JsonValue] | None,
        event_type: str | None,
        upstream_control: _WebSocketUpstreamControl,
        original_text: str,
    ) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]:
        return _maybe_rewrite_websocket_previous_response_not_found_event(
            request_state=request_state,
            event=event,
            payload=payload,
            event_type=event_type,
            upstream_control=upstream_control,
            original_text=original_text,
        )

    @staticmethod
    def _rewrite_websocket_previous_response_owner_unavailable_event_compatible(
        *,
        request_state: _WebSocketRequestState,
    ) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]:
        return _rewrite_websocket_previous_response_owner_unavailable_event(request_state=request_state)

    @staticmethod
    def _sanitize_websocket_connect_failure_compatible(
        *,
        request_state: _WebSocketRequestState,
        status_code: int,
        payload: OpenAIErrorEnvelope,
        error_code: str,
        error_message: str,
    ) -> tuple[int, OpenAIErrorEnvelope, str, str]:
        return _sanitize_websocket_connect_failure(
            request_state=request_state,
            status_code=status_code,
            payload=payload,
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _maybe_dump_oversized_response_create_request_compatible(
        request_state: _WebSocketRequestState,
        *,
        account_id_value: str | None,
        error_code: str,
        error_message: str | None,
    ) -> None:
        _maybe_dump_oversized_response_create_request(
            request_state,
            account_id_value=account_id_value,
            error_code=error_code,
            error_message=error_message,
        )

    @staticmethod
    def _response_create_max_bytes_compatible() -> int:
        return _UPSTREAM_RESPONSE_CREATE_MAX_BYTES

    @staticmethod
    def _enforce_response_create_size_limit_compatible(
        request_state: _WebSocketRequestState,
    ) -> None:
        _enforce_response_create_size_limit(request_state)

    @staticmethod
    def _http_bridge_startup_keepalive_grace_seconds() -> float:
        return _HTTP_BRIDGE_STARTUP_KEEPALIVE_GRACE_SECONDS

    @staticmethod
    def _http_bridge_recovery_heartbeat_seconds() -> float:
        return _ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS

    @staticmethod
    async def _cancel_http_bridge_upstream_reader(
        task: asyncio.Task[None],
        *,
        timeout_seconds: float,
    ) -> bool:
        return await _await_cancelled_task(
            task,
            timeout_seconds=timeout_seconds,
            label="http bridge upstream reader",
        )

    def _mark_http_bridge_account_permanent_failure(
        self,
        account: Account,
        error_code: str,
    ) -> None:
        async def persist() -> None:
            await self._load_balancer.mark_permanent_failure(account, error_code)

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            persist(),
            name=f"permanent-failure-persist-{account.id}-{time.monotonic_ns()}",
            label=f"permanent failure persistence account_id={account.id} code={error_code}",
        )

    async def _http_bridge_should_wait_for_registration_compatible(
        self,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
    ) -> bool:
        return await _http_bridge_should_wait_for_registration(self, key, settings)

    async def _http_bridge_owner_instance_compatible(
        self,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
    ) -> str | None:
        return await _http_bridge_owner_instance(key, settings, self._ring_membership)

    async def _active_http_bridge_instance_ring_compatible(
        self,
        settings: Settings,
    ) -> tuple[str, tuple[str, ...]]:
        return await _active_http_bridge_instance_ring(settings, self._ring_membership)

    @staticmethod
    def _http_bridge_session_reusable_for_request_compatible(
        *,
        session: _HTTPBridgeSession,
        key: _HTTPBridgeSessionKey,
        incoming_turn_state: str | None,
        previous_response_id: str | None,
        request_model: str | None,
        required_upstream_wire_api: str | None = None,
    ) -> bool:
        return _http_bridge_session_reusable_for_request(
            session=session,
            key=key,
            incoming_turn_state=incoming_turn_state,
            previous_response_id=previous_response_id,
            request_model=request_model,
            required_upstream_wire_api=required_upstream_wire_api,
        )

    @staticmethod
    def _record_http_bridge_continuity_fail_closed(
        *,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None,
        upstream_error_code: str | None = None,
    ) -> None:
        _record_continuity_fail_closed(
            surface="http_bridge",
            reason=reason,
            previous_response_id=previous_response_id,
            session_id=session_id,
            upstream_error_code=upstream_error_code,
        )

    async def _http_bridge_routable_budget_safe_account_ids(
        self,
        *,
        model: str | None,
        account_ids: Collection[str] | None,
        allowed_groups: Collection[str] | None,
        preferred_group_priorities: dict[str, int] | None,
        budget_threshold_pct: float,
        routing_strategy: RoutingStrategy,
        ignore_five_hour_limit: bool = False,
    ) -> set[str]:
        return await self._load_balancer.routable_budget_safe_account_ids(
            model=model,
            account_ids=account_ids,
            allowed_groups=allowed_groups,
            preferred_group_priorities=preferred_group_priorities,
            budget_threshold_pct=budget_threshold_pct,
            routing_strategy=routing_strategy,
            ignore_five_hour_limit=ignore_five_hour_limit,
        )

    @staticmethod
    def _is_retryable_http_bridge_connect_forbidden(exc: ProxyResponseError) -> bool:
        if exc.status_code != 403:
            return False
        error = _parse_openai_error(exc.payload)
        code = _normalize_error_code(error.code if error else None, error.type if error else None)
        return code in _HTTP_BRIDGE_RETRYABLE_CONNECT_FORBIDDEN_CODES

    @staticmethod
    def _http_bridge_connect_rejected_error() -> ProxyResponseError:
        return ProxyResponseError(
            502,
            openai_error(
                "upstream_unavailable",
                "HTTP responses session bridge upstream connection was rejected; retry later.",
                error_type="server_error",
            ),
        )

    def stream_responses(
        self,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool = False,
        propagate_http_errors: bool = False,
        openai_cache_affinity: bool = False,
        api_key: ApiKeyData | None = None,
        api_key_reservation: ApiKeyUsageReservationData | None = None,
        suppress_text_done_events: bool = False,
        request_transport: str = _REQUEST_TRANSPORT_HTTP,
        request_started_at: float | None = None,
        request_deadline_at: float | None = None,
    ) -> AsyncIterator[str]:
        _maybe_log_proxy_request_payload("stream", payload, headers)
        filtered = filter_inbound_headers(headers)
        return self._stream_with_retry(
            payload,
            filtered,
            codex_session_affinity=codex_session_affinity,
            propagate_http_errors=propagate_http_errors,
            openai_cache_affinity=openai_cache_affinity,
            api_key=api_key,
            api_key_reservation=api_key_reservation,
            suppress_text_done_events=suppress_text_done_events,
            request_transport=request_transport,
            request_started_at=request_started_at,
            request_deadline_at=request_deadline_at,
        )

    def stream_http_responses(
        self,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool = False,
        propagate_http_errors: bool = False,
        openai_cache_affinity: bool = False,
        api_key: ApiKeyData | None = None,
        api_key_reservation: ApiKeyUsageReservationData | None = None,
        suppress_text_done_events: bool = False,
        downstream_turn_state: str | None = None,
        forwarded_request: bool = False,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        forwarded_request_deadline_unix_ms: int | None = None,
        request_started_at: float | None = None,
        request_deadline_at: float | None = None,
    ) -> AsyncIterator[str]:
        _maybe_log_proxy_request_payload("stream_http", payload, headers)
        proxy_api_authorization = _header_value_case_insensitive(headers, "authorization")
        filtered = filter_inbound_headers(headers)
        return self._stream_http_bridge_or_retry(
            payload,
            filtered,
            codex_session_affinity=codex_session_affinity,
            propagate_http_errors=propagate_http_errors,
            openai_cache_affinity=openai_cache_affinity,
            api_key=api_key,
            api_key_reservation=api_key_reservation,
            suppress_text_done_events=suppress_text_done_events,
            downstream_turn_state=downstream_turn_state,
            forwarded_request=forwarded_request,
            proxy_api_authorization=proxy_api_authorization,
            forwarded_affinity_kind=forwarded_affinity_kind,
            forwarded_affinity_key=forwarded_affinity_key,
            forwarded_request_deadline_unix_ms=forwarded_request_deadline_unix_ms,
            request_started_at=request_started_at,
            request_deadline_at=request_deadline_at,
        )

    async def _stream_http_bridge_or_retry(
        self,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool,
        propagate_http_errors: bool,
        openai_cache_affinity: bool,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        suppress_text_done_events: bool,
        downstream_turn_state: str | None = None,
        forwarded_request: bool = False,
        proxy_api_authorization: str | None = None,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        forwarded_request_deadline_unix_ms: int | None = None,
        request_started_at: float | None = None,
        request_deadline_at: float | None = None,
    ) -> AsyncIterator[str]:
        runtime_settings = self._http_bridge_runtime_settings()
        if (request_started_at is None) != (request_deadline_at is None):
            raise RuntimeError("HTTP bridge request timing must be provided as a complete pair")
        request_started_at = request_started_at if request_started_at is not None else time.monotonic()
        request_deadline_at = (
            request_deadline_at
            if request_deadline_at is not None
            else request_started_at + runtime_settings.http_responses_session_bridge_request_budget_seconds
        )
        forwarded_request_acknowledged = False
        startup_reservation_owned = api_key_reservation is not None

        def transfer_startup_reservation_ownership() -> None:
            nonlocal startup_reservation_owned
            startup_reservation_owned = False

        def release_owned_startup_reservation(reason: str) -> None:
            nonlocal startup_reservation_owned
            if not startup_reservation_owned:
                return
            startup_reservation_owned = False
            self._schedule_websocket_reservation_release(
                api_key_reservation,
                reason=reason,
            )

        if forwarded_request:
            if forwarded_request_deadline_unix_ms is None:
                raise ProxyResponseError(
                    400,
                    openai_error(
                        "bridge_forward_invalid",
                        "Internal bridge forward request deadline is required",
                        error_type="invalid_request_error",
                    ),
                )
            forwarded_remaining = (forwarded_request_deadline_unix_ms / 1000.0) - time.time()
            if forwarded_remaining <= 0:
                _raise_proxy_budget_exhausted()
            request_deadline_at = min(request_deadline_at, request_started_at + forwarded_remaining)
        try:
            if forwarded_request:
                yield format_sse_event(cast(dict[str, JsonValue], {"type": HTTP_BRIDGE_OWNER_ACCEPTED_EVENT_TYPE}))
                forwarded_request_acknowledged = True
            dashboard_settings = await _await_http_bridge_startup_before_deadline(
                self._http_bridge_dashboard_settings,
                request_deadline_at=request_deadline_at,
                cleanup_tasks=self._proxy_cleanup_tasks,
            )
            runtime_config = _http_bridge_runtime_config(dashboard_settings, runtime_settings)
            input_image_request = _responses_request_contains_input_image(payload)
            image_generation_request = _responses_request_uses_image_generation(payload)
            if runtime_config.enabled and (input_image_request or image_generation_request):
                logger.info(
                    "stream_responses bypassing http bridge for image-capable request "
                    "input_image=%s image_generation=%s",
                    input_image_request,
                    image_generation_request,
                )
                runtime_config = dataclasses.replace(runtime_config, enabled=False)
            if forwarded_request and not runtime_config.enabled:
                raise ProxyResponseError(
                    503,
                    openai_error(
                        "bridge_owner_unreachable",
                        "HTTP bridge owner cannot accept forwarding while the bridge is disabled",
                        error_type="server_error",
                    ),
                )
            if not runtime_config.enabled:
                transfer_startup_reservation_ownership()
                if not forwarded_request:
                    request_deadline_at = (
                        request_started_at + runtime_settings.http_responses_stream_request_budget_seconds
                    )
                async for line in self._stream_with_retry(
                    payload,
                    headers,
                    codex_session_affinity=codex_session_affinity,
                    propagate_http_errors=propagate_http_errors,
                    openai_cache_affinity=openai_cache_affinity,
                    api_key=api_key,
                    api_key_reservation=api_key_reservation,
                    suppress_text_done_events=suppress_text_done_events,
                    request_transport=_REQUEST_TRANSPORT_HTTP,
                    request_started_at=request_started_at,
                    request_deadline_at=request_deadline_at,
                ):
                    yield line
                return

            async for line in _HTTPBridgeStreamMixin._stream_via_http_bridge(
                cast(_HTTPBridgeStreamService, self),
                payload,
                headers,
                codex_session_affinity=codex_session_affinity,
                propagate_http_errors=propagate_http_errors,
                openai_cache_affinity=openai_cache_affinity,
                api_key=api_key,
                api_key_reservation=api_key_reservation,
                suppress_text_done_events=suppress_text_done_events,
                idle_ttl_seconds=runtime_config.idle_ttl_seconds,
                codex_idle_ttl_seconds=runtime_config.codex_idle_ttl_seconds,
                max_sessions=runtime_config.max_sessions,
                queue_limit=runtime_config.queue_limit,
                prompt_cache_idle_ttl_seconds=runtime_config.prompt_cache_idle_ttl_seconds,
                downstream_turn_state=downstream_turn_state,
                forwarded_request=forwarded_request,
                proxy_api_authorization=proxy_api_authorization,
                forwarded_affinity_kind=forwarded_affinity_kind,
                forwarded_affinity_key=forwarded_affinity_key,
                forwarded_request_deadline_unix_ms=forwarded_request_deadline_unix_ms,
                request_started_at=request_started_at,
                request_deadline_at=request_deadline_at,
                dashboard_settings=dashboard_settings,
                runtime_settings=runtime_settings,
                forwarded_request_acknowledged=forwarded_request_acknowledged,
                on_reservation_handoff=transfer_startup_reservation_ownership,
            ):
                yield line
        except ProxyResponseError as exc:
            release_owned_startup_reservation("http-bridge-startup-proxy-failure")
            if not forwarded_request_acknowledged:
                raise
            yield _http_bridge_post_accept_failure_frame(exc)
        except Exception:
            release_owned_startup_reservation("http-bridge-startup-unexpected-failure")
            if not forwarded_request_acknowledged:
                raise
            logger.exception("HTTP bridge owner stream failed after acceptance")
            yield _http_bridge_startup_failure_frame(
                ProxyResponseError(
                    502,
                    openai_error(
                        "upstream_error",
                        "HTTP bridge owner stream failed after acceptance",
                        error_type="server_error",
                    ),
                )
            )
        except BaseException:
            release_owned_startup_reservation("http-bridge-startup-interrupted")
            raise

    async def _http_bridge_has_live_local_session(
        self,
        *,
        key: "_HTTPBridgeSessionKey",
        incoming_turn_state: str | None,
        api_key: ApiKeyData | None,
    ) -> bool:
        api_key_id = api_key.id if api_key is not None else None
        async with self._http_bridge_lock:
            candidate_keys = [key]
            if incoming_turn_state is not None:
                alias_key = self._http_bridge_turn_state_index.get(
                    _http_bridge_turn_state_alias_key(incoming_turn_state, api_key_id)
                )
                if alias_key is not None and alias_key not in candidate_keys:
                    candidate_keys.append(alias_key)
            for candidate_key in candidate_keys:
                session = self._http_bridge_sessions.get(candidate_key)
                if session is None or session.closed or session.account.status != AccountStatus.ACTIVE:
                    continue
                if not _http_bridge_session_allows_api_key(session, api_key):
                    continue
                return True
        return False

    async def _http_bridge_local_owner_account_id(
        self,
        *,
        key: "_HTTPBridgeSessionKey",
        incoming_turn_state: str | None,
        previous_response_id: str,
        api_key: ApiKeyData | None,
        request_model: str | None,
    ) -> str | None:
        api_key_id = api_key.id if api_key is not None else None
        candidate_keys: list[_HTTPBridgeSessionKey] = [key]
        async with self._http_bridge_lock:
            if incoming_turn_state is not None:
                alias_key = self._http_bridge_turn_state_index.get(
                    _http_bridge_turn_state_alias_key(incoming_turn_state, api_key_id)
                )
                if alias_key is not None and alias_key not in candidate_keys:
                    candidate_keys.append(alias_key)
            previous_alias_key = _http_bridge_previous_response_alias_key(previous_response_id, api_key_id)
            previous_key = self._http_bridge_previous_response_index.get(previous_alias_key)
            if previous_key is not None and previous_key not in candidate_keys:
                candidate_keys.append(previous_key)
            for candidate_key in candidate_keys:
                session = self._http_bridge_sessions.get(candidate_key)
                if session is None or session.closed or session.account.status != AccountStatus.ACTIVE:
                    continue
                if not _http_bridge_session_allows_api_key(session, api_key):
                    continue
                if not _http_bridge_session_reusable_for_request(
                    session=session,
                    key=candidate_key,
                    incoming_turn_state=incoming_turn_state,
                    previous_response_id=previous_response_id,
                    request_model=request_model,
                ):
                    continue
                _record_continuity_owner_resolution(
                    surface="http_bridge",
                    source="local_bridge_session",
                    outcome="hit",
                    previous_response_id=previous_response_id,
                    session_id=incoming_turn_state,
                )
                return session.account.id
        _record_continuity_owner_resolution(
            surface="http_bridge",
            source="local_bridge_session",
            outcome="miss",
            previous_response_id=previous_response_id,
            session_id=incoming_turn_state,
        )
        return None

    async def _http_bridge_can_forward_to_active_owner(
        self,
        durable_lookup: DurableBridgeLookup,
    ) -> bool:
        owner_instance = _durable_bridge_lookup_active_owner(durable_lookup)
        if owner_instance is None:
            return False
        if owner_instance == get_settings().http_responses_session_bridge_instance_id:
            return False
        if self._ring_membership is None:
            return False
        try:
            owner_endpoint = await self._ring_membership.resolve_endpoint(owner_instance)
        except Exception:
            logger.debug("Failed to resolve HTTP bridge owner endpoint during anchor injection decision", exc_info=True)
            return False
        return owner_endpoint is not None

    async def _forward_http_bridge_request_to_owner(
        self,
        *,
        owner_forward: _HTTPBridgeOwnerForward,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        api_key_reservation: ApiKeyUsageReservationData | None,
        codex_session_affinity: bool,
        downstream_turn_state: str | None,
        request_started_at: float,
        request_deadline_at: float,
        proxy_api_authorization: str | None,
        on_owner_attempt_started: Callable[[], None] | None = None,
        on_owner_accepted: Callable[[], None] | None = None,
    ) -> AsyncIterator[str]:
        current_instance, _ = _normalized_http_bridge_instance_ring(get_settings())
        forwarded_turn_state = _header_value_case_insensitive(headers, "x-codex-turn-state") or downstream_turn_state
        remaining_request_seconds = max(0.0, request_deadline_at - time.monotonic())
        request_deadline_unix_ms = int((time.time() + remaining_request_seconds) * 1000)
        forward_context = HTTPBridgeForwardContext(
            origin_instance=current_instance,
            target_instance=owner_forward.owner_instance,
            reservation=api_key_reservation,
            codex_session_affinity=codex_session_affinity,
            downstream_turn_state=forwarded_turn_state,
            original_affinity_kind=owner_forward.key.affinity_kind,
            original_affinity_key=owner_forward.key.affinity_key,
            request_deadline_unix_ms=request_deadline_unix_ms,
        )
        forward_headers = _headers_with_authorization(headers, proxy_api_authorization)
        start = time.monotonic()
        _log_http_bridge_event(
            "owner_forward_start",
            owner_forward.key,
            account_id=None,
            model=payload.model,
            detail=(
                f"owner_instance={owner_forward.owner_instance}, current_instance={current_instance}, "
                f"owner_endpoint={owner_forward.owner_endpoint}"
            ),
            cache_key_family=owner_forward.key.affinity_kind,
            model_class=_extract_model_class(payload.model) if payload.model else None,
            owner_check_applied=True,
        )

        forwarded_any = False
        try:
            if on_owner_attempt_started is not None:
                on_owner_attempt_started()
            async for event_block in self._http_bridge_owner_client.stream_responses(
                owner_endpoint=owner_forward.owner_endpoint,
                payload=payload,
                headers=forward_headers,
                context=forward_context,
                request_started_at=request_started_at,
                request_deadline_at=request_deadline_at,
            ):
                if is_owner_forward_accepted_event(event_block):
                    forwarded_any = True
                    if on_owner_accepted is not None:
                        on_owner_accepted()
                    continue
                forwarded_any = True
                yield event_block
        except OwnerForwardRelayFailure as exc:
            if PROMETHEUS_AVAILABLE and bridge_owner_forward_total is not None:
                bridge_owner_forward_total.labels(outcome="fail").inc()
            _log_http_bridge_event(
                "owner_forward_fail",
                owner_forward.key,
                account_id=None,
                model=payload.model,
                detail=(
                    f"owner_instance={owner_forward.owner_instance}, current_instance={current_instance}, "
                    "error=relay_failure"
                ),
                cache_key_family=owner_forward.key.affinity_kind,
                model_class=_extract_model_class(payload.model) if payload.model else None,
                owner_check_applied=True,
            )
            if not forwarded_any:
                self._schedule_unclaimed_websocket_reservation_release(
                    api_key_reservation,
                    reason="owner-forward-relay-failure-before-ack",
                )
            # A timeout before the acceptance control event is an ambiguous
            # handoff. Replaying locally could duplicate response.create; the
            # conditional release above only wins if the owner never claimed it.
            yield exc.event_block
            return
        except ProxyResponseError:
            if not forwarded_any:
                self._schedule_unclaimed_websocket_reservation_release(
                    api_key_reservation,
                    reason="owner-forward-proxy-failure-before-ack",
                )
            if PROMETHEUS_AVAILABLE and bridge_owner_forward_total is not None:
                bridge_owner_forward_total.labels(outcome="fail").inc()
            _log_http_bridge_event(
                "owner_forward_fail",
                owner_forward.key,
                account_id=None,
                model=payload.model,
                detail=f"owner_instance={owner_forward.owner_instance}, current_instance={current_instance}",
                cache_key_family=owner_forward.key.affinity_kind,
                model_class=_extract_model_class(payload.model) if payload.model else None,
                owner_check_applied=True,
            )
            raise
        except aiohttp.ClientConnectorError as exc:
            self._schedule_unclaimed_websocket_reservation_release(
                api_key_reservation,
                reason="owner-forward-connect-failure-before-ack",
            )
            if PROMETHEUS_AVAILABLE and bridge_owner_forward_total is not None:
                bridge_owner_forward_total.labels(outcome="fail").inc()
            _log_http_bridge_event(
                "owner_forward_fail",
                owner_forward.key,
                account_id=None,
                model=payload.model,
                detail=(
                    f"owner_instance={owner_forward.owner_instance}, current_instance={current_instance}, "
                    f"error=connect_failed:{exc}"
                ),
                cache_key_family=owner_forward.key.affinity_kind,
                model_class=_extract_model_class(payload.model) if payload.model else None,
                owner_check_applied=True,
            )
            raise ProxyResponseError(
                503,
                openai_error(
                    "bridge_owner_unreachable",
                    "HTTP bridge owner connection failed before reservation handoff",
                    error_type="server_error",
                ),
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if not forwarded_any:
                self._schedule_unclaimed_websocket_reservation_release(
                    api_key_reservation,
                    reason="owner-forward-stream-failure-before-ack",
                )
            if PROMETHEUS_AVAILABLE and bridge_owner_forward_total is not None:
                bridge_owner_forward_total.labels(outcome="fail").inc()
            _log_http_bridge_event(
                "owner_forward_fail",
                owner_forward.key,
                account_id=None,
                model=payload.model,
                detail=(
                    f"owner_instance={owner_forward.owner_instance}, current_instance={current_instance}, error={exc}"
                ),
                cache_key_family=owner_forward.key.affinity_kind,
                model_class=_extract_model_class(payload.model) if payload.model else None,
                owner_check_applied=True,
            )
            yield format_sse_event(
                response_failed_event(
                    "stream_incomplete",
                    "HTTP bridge owner stream disconnected after request acceptance"
                    if forwarded_any
                    else "HTTP bridge owner acceptance is unknown; local replay was suppressed",
                )
            )
            return
        except BaseException:
            if not forwarded_any:
                self._schedule_unclaimed_websocket_reservation_release(
                    api_key_reservation,
                    reason="owner-forward-interrupted-before-ack",
                )
            raise
        else:
            if PROMETHEUS_AVAILABLE and bridge_owner_forward_total is not None:
                bridge_owner_forward_total.labels(outcome="success").inc()
            _log_http_bridge_event(
                "owner_forward_success",
                owner_forward.key,
                account_id=None,
                model=payload.model,
                detail=f"owner_instance={owner_forward.owner_instance}, current_instance={current_instance}",
                cache_key_family=owner_forward.key.affinity_kind,
                model_class=_extract_model_class(payload.model) if payload.model else None,
                owner_check_applied=True,
            )
        finally:
            if PROMETHEUS_AVAILABLE and bridge_forward_latency_seconds is not None:
                bridge_forward_latency_seconds.observe(max(time.monotonic() - start, 0.0))

    async def _select_account_with_budget_compatible(
        self,
        deadline: float,
        **kwargs: object,
    ) -> AccountSelection:
        return cast(
            AccountSelection,
            await _call_with_supported_optional_kwargs(
                self._select_account_with_budget,
                deadline,
                optional_kwargs=kwargs,
            ),
        )

    async def _create_http_bridge_session_compatible(
        self,
        key: "_HTTPBridgeSessionKey",
        **kwargs: object,
    ) -> "_HTTPBridgeSession":
        return cast(
            _HTTPBridgeSession,
            await _call_with_supported_optional_kwargs(
                self._create_http_bridge_session,
                key,
                optional_kwargs=kwargs,
            ),
        )

    async def _refresh_websocket_api_key_policy(self, api_key: ApiKeyData | None) -> ApiKeyData | None:
        if api_key is None:
            return None

        with anyio.CancelScope(shield=True):
            async with self._repo_factory() as repos:
                service = ApiKeysService(repos.api_keys)
                try:
                    return await service.get_key_by_id(api_key.id)
                except ApiKeyInvalidError as exc:
                    raise ProxyAuthError(str(exc)) from exc

    async def _handle_websocket_connect_error(self, account: Account, exc: ProxyResponseError) -> ClassifiedFailure:
        error = _parse_openai_error(exc.payload)
        error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
        error_payload = _upstream_error_from_openai(error)
        classified = classify_upstream_failure(
            error_code=error_code,
            error=error_payload,
            http_status=exc.status_code,
            phase="connect",
        )

        async def persist() -> None:
            await self._persist_classified_upstream_error(
                account,
                error_payload,
                error_code,
                classified=classified,
            )

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            persist(),
            name=f"websocket-connect-error-persist-{account.id}-{time.monotonic_ns()}",
            label=f"websocket connect error persistence account_id={account.id} code={error_code}",
        )
        return classified

    async def _select_account_with_budget(
        self,
        deadline: float,
        *,
        request_id: str,
        kind: str,
        request_stage: str = "first_turn",
        api_key: ApiKeyData | None = None,
        sticky_key: str | None = None,
        sticky_kind: StickySessionKind | None = None,
        reallocate_sticky: bool = False,
        sticky_max_age_seconds: int | None = None,
        prefer_earlier_reset_accounts: bool = False,
        routing_strategy: RoutingStrategy = DEFAULT_ROUTING_STRATEGY,
        model: str | None = None,
        additional_limit_name: str | None = None,
        exclude_account_ids: Collection[str] | None = None,
        preferred_account_id: str | None = None,
        held_http_bridge_session_account_id: str | None = None,
        required_upstream_wire_api: str | None = None,
    ) -> AccountSelection:
        remaining_budget = max(0.0, deadline - time.monotonic())
        if remaining_budget <= 0:
            _raise_proxy_budget_exhausted()
        try:
            return await _await_operation_before_hard_timeout(
                self._select_account_with_budget_inner(
                    deadline,
                    request_id=request_id,
                    kind=kind,
                    request_stage=request_stage,
                    api_key=api_key,
                    sticky_key=sticky_key,
                    sticky_kind=sticky_kind,
                    reallocate_sticky=reallocate_sticky,
                    sticky_max_age_seconds=sticky_max_age_seconds,
                    prefer_earlier_reset_accounts=prefer_earlier_reset_accounts,
                    routing_strategy=routing_strategy,
                    model=model,
                    additional_limit_name=additional_limit_name,
                    exclude_account_ids=exclude_account_ids,
                    preferred_account_id=preferred_account_id,
                    held_http_bridge_session_account_id=held_http_bridge_session_account_id,
                    required_upstream_wire_api=required_upstream_wire_api,
                ),
                timeout_seconds=remaining_budget,
                tasks=self._proxy_cleanup_tasks,
                label=f"{kind} account selection request_id={request_id}",
            )
        except TimeoutError:
            logger.warning("%s account selection exceeded hard request budget request_id=%s", kind.title(), request_id)
            _raise_proxy_budget_exhausted()

    async def _select_account_with_budget_inner(
        self,
        deadline: float,
        *,
        request_id: str,
        kind: str,
        request_stage: str = "first_turn",
        api_key: ApiKeyData | None = None,
        sticky_key: str | None = None,
        sticky_kind: StickySessionKind | None = None,
        reallocate_sticky: bool = False,
        sticky_max_age_seconds: int | None = None,
        prefer_earlier_reset_accounts: bool = False,
        routing_strategy: RoutingStrategy = DEFAULT_ROUTING_STRATEGY,
        model: str | None = None,
        additional_limit_name: str | None = None,
        exclude_account_ids: Collection[str] | None = None,
        preferred_account_id: str | None = None,
        held_http_bridge_session_account_id: str | None = None,
        required_upstream_wire_api: str | None = None,
    ) -> AccountSelection:
        remaining_budget = _remaining_budget_seconds(deadline)
        if remaining_budget <= 0:
            logger.warning(
                "%s request budget exhausted before account selection request_id=%s", kind.title(), request_id
            )
            _raise_proxy_budget_exhausted()
        scoped_account_ids = (
            set(api_key.assigned_account_ids)
            if api_key is not None and api_key.account_assignment_scope_enabled
            else None
        )
        allowed_groups = set(api_key.allowed_groups) if api_key is not None and api_key.allowed_groups else None
        preferred_group_priorities = (
            {preference.group: preference.priority for preference in api_key.preferred_groups}
            if api_key is not None and api_key.preferred_groups
            else None
        )
        explicit_excluded_account_ids = set(exclude_account_ids or ())
        selection_excluded_account_ids = set(explicit_excluded_account_ids)
        concurrency_excluded_account_ids = self._full_account_model_concurrency_account_ids(model)
        selection_excluded_account_ids.update(concurrency_excluded_account_ids)
        bridge_session_excluded_account_ids = (
            self._full_http_bridge_session_account_ids(model) if kind == "http_bridge" else set()
        )
        bridge_connect_excluded_account_ids = (
            self._full_http_bridge_connect_account_ids(model) if kind == "http_bridge" else set()
        )
        if held_http_bridge_session_account_id is not None:
            bridge_session_excluded_account_ids.discard(held_http_bridge_session_account_id)
        selection_excluded_account_ids.update(bridge_connect_excluded_account_ids)
        local_budget_excluded_account_ids = (
            concurrency_excluded_account_ids | bridge_session_excluded_account_ids | bridge_connect_excluded_account_ids
        )
        try:
            with anyio.fail_after(remaining_budget):
                settings = await get_settings_cache().get()
                ignore_five_hour_limit = bool(getattr(settings, "ignore_five_hour_limit", False))
                if (
                    preferred_account_id is not None
                    and preferred_account_id not in selection_excluded_account_ids
                    and (scoped_account_ids is None or preferred_account_id in scoped_account_ids)
                ):
                    preferred_selection = await self._load_balancer.select_account(
                        sticky_key=sticky_key,
                        sticky_kind=sticky_kind,
                        reallocate_sticky=reallocate_sticky,
                        sticky_max_age_seconds=sticky_max_age_seconds,
                        prefer_earlier_reset_accounts=prefer_earlier_reset_accounts,
                        routing_strategy=routing_strategy,
                        model=model,
                        additional_limit_name=additional_limit_name,
                        account_ids={preferred_account_id},
                        allowed_groups=allowed_groups,
                        preferred_group_priorities=preferred_group_priorities,
                        budget_threshold_pct=settings.sticky_reallocation_budget_threshold_pct,
                        ignore_five_hour_limit=ignore_five_hour_limit,
                        required_upstream_wire_api=required_upstream_wire_api,
                    )
                    if preferred_selection.account is not None:
                        logger.info(
                            "Selected preferred account request_id=%s kind=%s request_stage=%s account_id=%s",
                            request_id,
                            kind,
                            request_stage,
                            preferred_account_id,
                        )
                        return preferred_selection
                selection = await self._load_balancer.select_account(
                    sticky_key=sticky_key,
                    sticky_kind=sticky_kind,
                    reallocate_sticky=reallocate_sticky,
                    sticky_max_age_seconds=sticky_max_age_seconds,
                    prefer_earlier_reset_accounts=prefer_earlier_reset_accounts,
                    routing_strategy=routing_strategy,
                    model=model,
                    additional_limit_name=additional_limit_name,
                    account_ids=scoped_account_ids,
                    exclude_account_ids=selection_excluded_account_ids,
                    allowed_groups=allowed_groups,
                    preferred_group_priorities=preferred_group_priorities,
                    budget_threshold_pct=settings.sticky_reallocation_budget_threshold_pct,
                    ignore_five_hour_limit=ignore_five_hour_limit,
                    required_upstream_wire_api=required_upstream_wire_api,
                )
                if selection.account is not None and selection.account.id in selection_excluded_account_ids:
                    return AccountSelection(
                        account=None,
                        error_message="All eligible accounts are at the local account/model concurrency budget",
                        error_code="proxy_overloaded",
                    )
                if selection.account is None and local_budget_excluded_account_ids:
                    fallback_selection = await self._load_balancer.select_account(
                        sticky_key=sticky_key,
                        sticky_kind=sticky_kind,
                        reallocate_sticky=reallocate_sticky,
                        sticky_max_age_seconds=sticky_max_age_seconds,
                        prefer_earlier_reset_accounts=prefer_earlier_reset_accounts,
                        routing_strategy=routing_strategy,
                        model=model,
                        additional_limit_name=additional_limit_name,
                        account_ids=scoped_account_ids,
                        exclude_account_ids=explicit_excluded_account_ids,
                        allowed_groups=allowed_groups,
                        preferred_group_priorities=preferred_group_priorities,
                        budget_threshold_pct=settings.sticky_reallocation_budget_threshold_pct,
                        ignore_five_hour_limit=ignore_five_hour_limit,
                        required_upstream_wire_api=required_upstream_wire_api,
                    )
                    if fallback_selection.account is None:
                        if fallback_selection.retry_after_seconds is not None and selection.retry_after_seconds is None:
                            return AccountSelection(
                                account=None,
                                error_message=fallback_selection.error_message,
                                error_code=(fallback_selection.error_code or _ACCOUNT_SELECTION_RECOVERABLE_WAIT_CODE),
                                retry_after_seconds=fallback_selection.retry_after_seconds,
                            )
                        return selection
                    return AccountSelection(
                        account=None,
                        error_message=("All eligible accounts are at the local account/model concurrency budget"),
                        error_code="proxy_overloaded",
                    )
                if selection.account is None and selection.retry_after_seconds is not None:
                    return AccountSelection(
                        account=None,
                        error_message=selection.error_message,
                        error_code=selection.error_code or _ACCOUNT_SELECTION_RECOVERABLE_WAIT_CODE,
                        retry_after_seconds=selection.retry_after_seconds,
                    )
                return selection
        except TimeoutError:
            logger.warning("%s account selection exceeded request budget request_id=%s", kind.title(), request_id)
            _raise_proxy_budget_exhausted()

    async def _handle_proxy_error(self, account: Account, exc: ProxyResponseError) -> None:
        error = _parse_openai_error(exc.payload)
        code = _normalize_error_code(
            error.code if error else None,
            error.type if error else None,
        )
        if _is_account_neutral_error_code(code):
            return
        await self._handle_stream_error(
            account,
            _upstream_error_from_openai(error),
            code,
            http_status=exc.status_code,
        )

    async def _handle_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        http_status: int | None = None,
    ) -> ClassifiedFailure:
        return await self._handle_upstream_error(
            account,
            error,
            code,
            http_status=http_status,
            phase="first_event",
        )

    def _classify_and_schedule_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        *,
        http_status: int | None = None,
        additional_error_count: int = 0,
    ) -> ClassifiedFailure:
        classified = classify_upstream_failure(
            error_code=code,
            error=error,
            http_status=http_status,
            phase="first_event",
        )

        async def persist() -> None:
            await self._persist_classified_upstream_error(
                account,
                error,
                code,
                classified=classified,
            )
            if additional_error_count > 0:
                await self._load_balancer.record_errors(account, additional_error_count)

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            persist(),
            name=f"stream-error-persist-{account.id}-{time.monotonic_ns()}",
            label=f"stream error persistence account_id={account.id} code={code}",
        )
        return classified

    async def _handle_upstream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        *,
        http_status: int | None,
        phase: Literal["connect", "first_event", "mid_stream"],
    ) -> ClassifiedFailure:
        classified = classify_upstream_failure(
            error_code=code,
            error=error,
            http_status=http_status,
            phase=phase,
        )
        await self._persist_classified_upstream_error(
            account,
            error,
            code,
            classified=classified,
        )
        return classified

    async def _persist_classified_upstream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        *,
        classified: ClassifiedFailure,
    ) -> None:
        if _is_account_neutral_error_code(code):
            return
        if classified["failure_class"] == "rate_limit":
            await self._load_balancer.mark_rate_limit(account, error)
        elif classified["failure_class"] == "quota":
            await self._load_balancer.mark_quota_exceeded(account, error)
        elif code in PERMANENT_FAILURE_CODES:
            await self._load_balancer.mark_permanent_failure(account, code)
        else:
            await self._load_balancer.record_error(account)
            logger.info(
                "Recorded transient account error account_id=%s request_id=%s code=%s",
                account.id,
                get_request_id(),
                code,
            )


def _enforce_response_create_size_limit(request_state: _WebSocketRequestState) -> None:
    _enforce_response_create_size_limit_impl(
        request_state,
        warn_bytes=_UPSTREAM_RESPONSE_CREATE_WARN_BYTES,
        max_bytes=_UPSTREAM_RESPONSE_CREATE_MAX_BYTES,
        dump_dir=_OVERSIZED_RESPONSE_CREATE_DUMP_DIR,
        largest_items_limit=_OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS,
    )


def _maybe_dump_oversized_response_create_request(
    request_state: _WebSocketRequestState,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
) -> None:
    _maybe_dump_oversized_response_create_request_impl(
        request_state,
        account_id_value=account_id_value,
        error_code=error_code,
        error_message=error_message,
        dump_dir=_OVERSIZED_RESPONSE_CREATE_DUMP_DIR,
        largest_items_limit=_OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS,
    )


def _write_response_create_dump(
    request_state: _WebSocketRequestState,
    *,
    account_id_value: str | None,
    error_code: str,
    error_message: str | None,
    log_prefix: str,
) -> bool:
    return _write_response_create_dump_impl(
        request_state,
        account_id_value=account_id_value,
        error_code=error_code,
        error_message=error_message,
        log_prefix=log_prefix,
        dump_dir=_OVERSIZED_RESPONSE_CREATE_DUMP_DIR,
        largest_items_limit=_OVERSIZED_RESPONSE_CREATE_LARGEST_ITEMS,
    )
