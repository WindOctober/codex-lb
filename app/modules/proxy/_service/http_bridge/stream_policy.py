from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import cast

from app.core.clients.proxy import ProxyResponseError
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.db.models import StickySessionKind
from app.modules.proxy._service.affinity import (
    _sticky_key_from_session_header,
    _sticky_key_from_turn_state_header,
)
from app.modules.proxy._service.support import _AffinityPolicy, _HTTPBridgeSessionKey
from app.modules.proxy._service.websocket.events import (
    _is_previous_response_not_found_error,
)
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeLookup
from app.modules.proxy.helpers import _normalize_error_code


def _fingerprint_input_items(items: Sequence[JsonValue]) -> str:
    canonical = json.dumps(list(items), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _input_prefix_matches_stored_context(
    input_value: JsonValue,
    *,
    stored_count: int,
    stored_fingerprint: str | None,
) -> bool:
    if stored_count <= 0 or stored_fingerprint is None or not isinstance(input_value, list):
        return False
    if len(input_value) <= stored_count:
        return False
    prefix = cast(list[JsonValue], input_value)[:stored_count]
    return _fingerprint_input_items(prefix) == stored_fingerprint


def _http_bridge_payload_looks_like_full_resend(payload: ResponsesRequest) -> bool:
    input_value = payload.input
    if isinstance(input_value, str):
        return len(input_value) >= 4096
    if not isinstance(input_value, Sequence) or isinstance(input_value, (str, bytes, bytearray)):
        return False
    if len(input_value) > 1:
        return True
    if len(input_value) != 1:
        return False
    try:
        return len(json.dumps(input_value[0], ensure_ascii=True, separators=(",", ":"))) >= 4096
    except TypeError:
        return False


def _http_bridge_request_stage(
    *,
    headers: Mapping[str, str],
    payload: ResponsesRequest,
    durable_lookup: DurableBridgeLookup | None,
) -> str:
    del durable_lookup
    if (
        payload.previous_response_id is not None
        or _sticky_key_from_turn_state_header(headers) is not None
        or _sticky_key_from_session_header(headers) is not None
    ):
        return "follow_up"
    return "first_turn"


def _effective_http_bridge_idle_ttl_seconds(
    *,
    affinity: _AffinityPolicy,
    idle_ttl_seconds: float,
    codex_idle_ttl_seconds: float,
    prompt_cache_idle_ttl_seconds: float | None = None,
) -> float:
    if affinity.kind == StickySessionKind.CODEX_SESSION:
        return max(idle_ttl_seconds, codex_idle_ttl_seconds)
    if affinity.kind == StickySessionKind.PROMPT_CACHE and prompt_cache_idle_ttl_seconds is not None:
        return prompt_cache_idle_ttl_seconds
    return idle_ttl_seconds


def _http_bridge_payload_without_previous_response_id(payload: ResponsesRequest) -> ResponsesRequest:
    if payload.previous_response_id is None:
        return payload
    return payload.model_copy(update={"previous_response_id": None})


def _http_bridge_should_attempt_local_previous_response_recovery(exc: ProxyResponseError) -> bool:
    payload = exc.payload
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if code in {
        "bridge_owner_unreachable",
        "previous_response_not_found",
        "bridge_instance_mismatch",
    }:
        return True
    param_value = error.get("param")
    param = param_value.strip() if isinstance(param_value, str) and param_value.strip() else None
    message_value = error.get("message")
    message = message_value.strip() if isinstance(message_value, str) and message_value.strip() else None
    if code == "upstream_unavailable" and message == "Previous response owner account is unavailable; retry later.":
        return True
    return _is_previous_response_not_found_error(code=code, param=param, message=message)


def _http_bridge_is_context_overflow_error(exc: ProxyResponseError) -> bool:
    payload = exc.payload
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code_value = error.get("code")
    code = code_value.strip() if isinstance(code_value, str) and code_value.strip() else None
    type_value = error.get("type")
    error_type = type_value.strip() if isinstance(type_value, str) and type_value.strip() else None
    return _normalize_error_code(code, error_type) == "context_length_exceeded"


def _http_bridge_should_rollover_after_context_overflow(
    exc: ProxyResponseError,
    *,
    key: _HTTPBridgeSessionKey | None = None,
) -> bool:
    if not _http_bridge_is_context_overflow_error(exc):
        return False
    return key is None or key.strength != "hard"


def _http_bridge_should_attempt_local_bootstrap_rebind(
    exc: ProxyResponseError,
    *,
    key: _HTTPBridgeSessionKey,
    headers: Mapping[str, str],
    previous_response_id: str | None,
) -> bool:
    if key.affinity_kind != "session_header" or previous_response_id is not None:
        return False
    if _sticky_key_from_turn_state_header(headers) is not None:
        return False
    payload = exc.payload
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return error.get("code") in {
        "bridge_owner_unreachable",
        "bridge_instance_mismatch",
    }
