from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Protocol

from app.core.config.settings import Settings
from app.core.metrics.prometheus import (
    PROMETHEUS_AVAILABLE,
    bridge_drain_recovery_allowed_total,
    bridge_first_turn_timeout_total,
    bridge_reattach_total,
    bridge_same_account_takeover_total,
    continuity_fail_closed_total,
    continuity_owner_resolution_total,
)
from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest
from app.core.types import JsonValue
from app.core.utils.request_id import get_request_id
from app.modules.proxy._service.affinity import (
    _extract_model_class,
    _prompt_cache_key_from_request_model,
    _sticky_key_from_session_header,
)
from app.modules.proxy._service.http_bridge.keys import _http_bridge_key_strength
from app.modules.proxy._service.support import _HTTPBridgeSession, _HTTPBridgeSessionKey, _WebSocketRequestState

logger = logging.getLogger("app.modules.proxy.service")

_TEXT_DELTA_EVENT_TYPES = frozenset({"response.output_text.delta", "response.refusal.delta"})


class _MetricSample(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...


class _LabeledCounter(Protocol):
    def labels(self, **labels: str) -> _MetricSample: ...


def _hash_identifier(value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def _truncate_identifier(value: str, *, max_length: int = 96) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[:48]}...{value[-16:]}"


def _tools_hash(payload: ResponsesRequest | ResponsesCompactRequest) -> str | None:
    payload_tools = payload.to_payload().get("tools")
    if not isinstance(payload_tools, list) or not payload_tools:
        return None
    serialized = json.dumps(payload_tools, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _hash_identifier(serialized)


def _interesting_header_keys(headers: Mapping[str, str]) -> list[str]:
    allowlist = {
        "user-agent",
        "x-request-id",
        "request-id",
        "session_id",
        "x-openai-client-id",
        "x-openai-client-version",
        "x-openai-client-arch",
        "x-openai-client-os",
        "x-openai-client-user-agent",
        "x-codex-session-id",
        "x-codex-conversation-id",
    }
    return sorted({key.lower() for key in headers if key.lower() in allowlist})


def _summarize_input(items: JsonValue) -> str:
    if items is None:
        return "0"
    if isinstance(items, str):
        return "str"
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
        if not items:
            return "0"
        type_counts: dict[str, int] = {}
        for item in items:
            type_name = type(item).__name__
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
        summary = ",".join(f"{key}={type_counts[key]}" for key in sorted(type_counts))
        return f"{len(items)}({summary})"
    return type(items).__name__


def _maybe_log_proxy_request_shape(
    kind: str,
    payload: ResponsesRequest | ResponsesCompactRequest,
    headers: Mapping[str, str],
    *,
    settings: Settings,
    sticky_kind: str | None = None,
    sticky_key_source: str | None = None,
    prompt_cache_key_set: bool | None = None,
) -> None:
    if not settings.log_proxy_request_shape:
        return

    prompt_cache_key = _prompt_cache_key_from_request_model(payload)
    prompt_cache_key_hash = _hash_identifier(prompt_cache_key) if isinstance(prompt_cache_key, str) else None
    prompt_cache_key_raw = (
        _truncate_identifier(prompt_cache_key)
        if settings.log_proxy_request_shape_raw_cache_key and isinstance(prompt_cache_key, str)
        else None
    )
    extra_keys = sorted(payload.model_extra) if payload.model_extra else []
    fields_set = sorted(payload.model_fields_set)
    session_header_present = _sticky_key_from_session_header(headers) is not None

    logger.warning(
        "proxy_request_shape request_id=%s kind=%s model=%s stream=%s input=%s "
        "prompt_cache_key=%s prompt_cache_key_raw=%s fields=%s extra=%s headers=%s "
        "sticky_kind=%s sticky_key_source=%s prompt_cache_key_set=%s"
        " session_header_present=%s tools_hash=%s model_class=%s",
        get_request_id(),
        kind,
        payload.model,
        getattr(payload, "stream", None),
        _summarize_input(payload.input),
        prompt_cache_key_hash,
        prompt_cache_key_raw,
        fields_set,
        extra_keys,
        _interesting_header_keys(headers),
        sticky_kind,
        sticky_key_source,
        prompt_cache_key_set,
        session_header_present,
        _tools_hash(payload),
        _extract_model_class(payload.model),
    )


def _maybe_log_proxy_request_payload(
    kind: str,
    payload: ResponsesRequest | ResponsesCompactRequest,
    headers: Mapping[str, str],
    *,
    settings: Settings,
) -> None:
    if not settings.log_proxy_request_payload:
        return
    payload_dict = payload.model_dump(mode="json", exclude_none=True)
    extra = payload.model_extra or {}
    if extra:
        payload_dict = {**payload_dict, "_extra": extra}
    logger.warning(
        "proxy_request_payload request_id=%s kind=%s payload=%s headers=%s",
        get_request_id(),
        kind,
        json.dumps(payload_dict, ensure_ascii=True, separators=(",", ":")),
        _interesting_header_keys(headers),
    )


def _maybe_log_proxy_service_tier_trace(
    kind: str,
    *,
    requested_service_tier: str | None,
    actual_service_tier: str | None,
    settings: Settings,
) -> None:
    if not settings.log_proxy_service_tier_trace:
        return
    logger.warning(
        "proxy_service_tier_trace request_id=%s kind=%s requested_service_tier=%s actual_service_tier=%s",
        get_request_id(),
        kind,
        requested_service_tier,
        actual_service_tier,
    )


def _monotonic_age_ms(now: float, started_at: float | None) -> int | None:
    if started_at is None:
        return None
    return max(0, int((now - started_at) * 1000))


def _log_http_bridge_event(
    event: str,
    key: _HTTPBridgeSessionKey,
    *,
    account_id: str | None,
    model: str | None,
    pending_count: int | None = None,
    detail: str | None = None,
    cache_key_family: str | None = None,
    model_class: str | None = None,
    owner_check_applied: bool | None = None,
) -> None:
    level = logging.INFO
    if event in {
        "queue_full",
        "submit_on_closed",
        "send_failure",
        "retry_fresh_upstream",
        "retry_precreated",
        "reconnect",
        "terminal_error",
        "capacity_exhausted_active_sessions",
        "owner_mismatch",
        "owner_forward_fail",
        "prompt_cache_locality_miss",
        "reallocation_orphan",
        "context_overflow_rollover",
        "busy_recreate_deferred",
        "busy_recreate_parallel",
        "pressure_evict_parallel_prompt_cache",
        "evict_upstream_disconnected",
    }:
        level = logging.WARNING
    logger.log(
        level,
        "http_bridge_event event=%s bridge_kind=%s bridge_key=%s account_id=%s"
        " model=%s pending=%s detail=%s cache_key_family=%s model_class=%s"
        " key_strength=%s owner_check_applied=%s",
        event,
        key.affinity_kind,
        _hash_identifier(key.affinity_key),
        account_id,
        model,
        pending_count,
        detail,
        cache_key_family,
        model_class,
        _http_bridge_key_strength(key),
        owner_check_applied,
    )


def _record_same_account_takeover(*, preferred_account_id: str | None, selected_account_id: str | None) -> None:
    if not PROMETHEUS_AVAILABLE or bridge_same_account_takeover_total is None or preferred_account_id is None:
        return
    if selected_account_id is None:
        bridge_same_account_takeover_total.labels(outcome="fail").inc()
    elif selected_account_id == preferred_account_id:
        bridge_same_account_takeover_total.labels(outcome="success").inc()
    else:
        bridge_same_account_takeover_total.labels(outcome="fallback").inc()


def _record_bridge_first_turn_timeout() -> None:
    if PROMETHEUS_AVAILABLE and bridge_first_turn_timeout_total is not None:
        bridge_first_turn_timeout_total.inc()


def _record_bridge_drain_recovery_allowed() -> None:
    if PROMETHEUS_AVAILABLE and bridge_drain_recovery_allowed_total is not None:
        bridge_drain_recovery_allowed_total.inc()


def _log_http_bridge_get_or_create_breakdown(
    *,
    trace_request_id: str | None,
    outcome: str,
    key: _HTTPBridgeSessionKey,
    account_id: str | None,
    model: str | None,
    request_stage: str,
    total_ms: int | None,
    registration_check_ms: int,
    registration_gate_ms: int,
    main_lock_wait_ms: int,
    main_lock_body_ms: int,
    stale_close_ms: int,
    capacity_wait_ms: int,
    inflight_wait_ms: int,
    create_session_ms: int,
    loops: int,
    preferred_account_id: str | None,
    require_preferred_account: bool,
) -> None:
    if trace_request_id is None:
        return
    logger.warning(
        "http_bridge_get_or_create_breakdown request_id=%s outcome=%s account_id=%s model=%s"
        " request_stage=%s total_ms=%s registration_check_ms=%s registration_gate_ms=%s"
        " main_lock_wait_ms=%s main_lock_body_ms=%s stale_close_ms=%s capacity_wait_ms=%s"
        " inflight_wait_ms=%s create_session_ms=%s loops=%s bridge_kind=%s bridge_key=%s"
        " key_strength=%s preferred_account_id=%s require_preferred_account=%s",
        trace_request_id,
        outcome,
        account_id,
        model,
        request_stage,
        total_ms,
        registration_check_ms,
        registration_gate_ms,
        main_lock_wait_ms,
        main_lock_body_ms,
        stale_close_ms,
        capacity_wait_ms,
        inflight_wait_ms,
        create_session_ms,
        loops,
        key.affinity_kind,
        _hash_identifier(key.affinity_key),
        _http_bridge_key_strength(key),
        preferred_account_id,
        require_preferred_account,
    )


def _record_bridge_reattach(*, path: str, outcome: str) -> None:
    if PROMETHEUS_AVAILABLE and bridge_reattach_total is not None:
        bridge_reattach_total.labels(path=path, outcome=outcome).inc()


def _hash_identifier_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return _hash_identifier(stripped)


def _record_continuity_owner_resolution(
    *,
    surface: str,
    source: str,
    outcome: str,
    previous_response_id: str | None,
    session_id: str | None,
    prometheus_available: bool | None = None,
    metric: _LabeledCounter | None = None,
) -> None:
    metrics_enabled = PROMETHEUS_AVAILABLE if prometheus_available is None else prometheus_available
    counter = continuity_owner_resolution_total if metric is None else metric
    if metrics_enabled and counter is not None:
        counter.labels(
            surface=surface,
            source=source,
            outcome=outcome,
        ).inc()
    if outcome == "miss" or (outcome == "hit" and source == "request_cache"):
        return
    logger.log(
        logging.WARNING if outcome == "fail_closed" else logging.INFO,
        "continuity_owner_resolution surface=%s source=%s outcome=%s previous_response_id=%s session_id=%s",
        surface,
        source,
        outcome,
        _hash_identifier_or_none(previous_response_id),
        _hash_identifier_or_none(session_id),
    )


def _record_continuity_fail_closed(
    *,
    surface: str,
    reason: str,
    previous_response_id: str | None,
    session_id: str | None = None,
    upstream_error_code: str | None = None,
    prometheus_available: bool | None = None,
    metric: _LabeledCounter | None = None,
) -> None:
    metrics_enabled = PROMETHEUS_AVAILABLE if prometheus_available is None else prometheus_available
    counter = continuity_fail_closed_total if metric is None else metric
    if metrics_enabled and counter is not None:
        counter.labels(
            surface=surface,
            reason=reason,
        ).inc()
    logger.warning(
        "continuity_fail_closed surface=%s reason=%s previous_response_id=%s session_id=%s upstream_error_code=%s",
        surface,
        reason,
        _hash_identifier_or_none(previous_response_id),
        _hash_identifier_or_none(session_id),
        upstream_error_code,
    )


def _elapsed_ms(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start) * 1000)


def _record_http_bridge_upstream_event(
    request_state: _WebSocketRequestState,
    event_type: str | None,
) -> None:
    now = time.monotonic()
    if request_state.http_bridge_upstream_first_event_at is None:
        request_state.http_bridge_upstream_first_event_at = now
        request_state.http_bridge_upstream_first_event_type = event_type
    if event_type in _TEXT_DELTA_EVENT_TYPES and request_state.http_bridge_upstream_first_text_at is None:
        request_state.http_bridge_upstream_first_text_at = now


def _log_http_bridge_latency_breakdown(
    session: _HTTPBridgeSession,
    request_state: _WebSocketRequestState,
    *,
    event_type: str | None,
) -> None:
    if request_state.http_bridge_latency_breakdown_logged:
        return
    first_text_at = (
        request_state.http_bridge_downstream_first_text_at or request_state.http_bridge_upstream_first_text_at
    )
    if first_text_at is None:
        return
    request_state.http_bridge_latency_breakdown_logged = True
    gate_wait_started_at = request_state.http_bridge_gate_wait_started_at
    gate_wait_reference_at = request_state.http_bridge_gate_acquired_at or gate_wait_started_at
    logger.warning(
        "http_bridge_latency_breakdown request_id=%s response_id=%s session_id=%s account_id=%s"
        " model=%s event_type=%s first_upstream_event_type=%s total_to_first_text_ms=%s"
        " submit_start_delay_ms=%s local_owner_lookup_ms=%s previous_owner_resolve_ms=%s"
        " get_or_create_session_ms=%s response_create_gate_wait_ms=%s response_create_admission_wait_ms=%s"
        " send_after_admission_ms=%s upstream_first_event_ms=%s upstream_first_text_ms=%s"
        " downstream_first_event_ms=%s downstream_first_text_ms=%s upstream_to_downstream_text_ms=%s"
        " cache_key_family=%s bridge_key=%s key_strength=%s pending_count=%s",
        request_state.request_id,
        request_state.response_id,
        request_state.session_id,
        session.account.id,
        session.request_model,
        event_type,
        request_state.http_bridge_upstream_first_event_type,
        _elapsed_ms(request_state.started_at, first_text_at),
        _elapsed_ms(request_state.started_at, request_state.http_bridge_submit_started_at),
        _elapsed_ms(
            request_state.http_bridge_local_owner_lookup_started_at,
            request_state.http_bridge_local_owner_lookup_completed_at,
        ),
        _elapsed_ms(
            request_state.http_bridge_previous_owner_resolve_started_at,
            request_state.http_bridge_previous_owner_resolve_completed_at,
        ),
        _elapsed_ms(
            request_state.http_bridge_get_or_create_started_at,
            request_state.http_bridge_get_or_create_completed_at,
        ),
        _elapsed_ms(gate_wait_started_at, request_state.http_bridge_gate_acquired_at),
        _elapsed_ms(gate_wait_reference_at, request_state.http_bridge_admission_acquired_at),
        _elapsed_ms(request_state.http_bridge_admission_acquired_at, request_state.http_bridge_send_completed_at),
        _elapsed_ms(request_state.started_at, request_state.http_bridge_upstream_first_event_at),
        _elapsed_ms(request_state.started_at, request_state.http_bridge_upstream_first_text_at),
        _elapsed_ms(request_state.started_at, request_state.http_bridge_downstream_first_event_at),
        _elapsed_ms(request_state.started_at, request_state.http_bridge_downstream_first_text_at),
        _elapsed_ms(
            request_state.http_bridge_upstream_first_text_at,
            request_state.http_bridge_downstream_first_text_at,
        ),
        session.key.affinity_kind,
        _hash_identifier(session.key.affinity_key),
        _http_bridge_key_strength(session.key),
        len(session.pending_requests),
    )
