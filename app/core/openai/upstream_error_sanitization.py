from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from app.core.errors import OpenAIErrorEnvelope
from app.core.types import JsonValue

_PUBLIC_UPSTREAM_ERROR_CODES = frozenset(
    {
        "account_deactivated",
        "account_deleted",
        "account_suspended",
        "insufficient_permissions",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_request_error",
        "forbidden",
        "not_found",
        "not_implemented",
        "permission_error",
        "previous_response_not_found",
        "proxy_overloaded",
        "proxy_unavailable",
        "quota_exceeded",
        "rate_limit_exceeded",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
        "server_error",
        "server_is_overloaded",
        "stream_incomplete",
        "unsupported_transport",
        "upstream_connect_timeout",
        "upstream_capability_unavailable",
        "upstream_error",
        "upstream_unavailable",
        "usage_limit_reached",
        "usage_not_included",
    }
)
_PUBLIC_UPSTREAM_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "rate_limit_error",
        "server_error",
    }
)
_PUBLIC_UPSTREAM_ERROR_PARAMS = frozenset({"previous_response_id"})
_PUBLIC_RATE_LIMIT_PLAN_TYPES = frozenset(
    {
        "business",
        "edu",
        "education",
        "enterprise",
        "free",
        "go",
        "plus",
        "pro",
        "team",
    }
)


def sanitize_upstream_websocket_error_detail(
    error: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    raw_code = error.get("code")
    raw_type = error.get("type")
    raw_message = error.get("message")
    raw_param = error.get("param")
    code_candidate = raw_code.strip().lower() if isinstance(raw_code, str) else ""
    type_candidate = raw_type.strip().lower() if isinstance(raw_type, str) else ""
    param_candidate = raw_param.strip().lower() if isinstance(raw_param, str) else ""
    message_candidate = " ".join(raw_message.lower().split()) if isinstance(raw_message, str) else ""
    if (
        code_candidate == "invalid_request_error"
        and param_candidate == "previous_response_id"
        and "previous response" in message_candidate
        and "not found" in message_candidate
    ):
        normalized_code = "previous_response_not_found"
    else:
        normalized_code = code_candidate if code_candidate in _PUBLIC_UPSTREAM_ERROR_CODES else "upstream_error"

    if type_candidate in _PUBLIC_UPSTREAM_ERROR_TYPES:
        normalized_type = type_candidate
    elif normalized_code in {"invalid_request_error", "previous_response_not_found"}:
        normalized_type = "invalid_request_error"
    elif normalized_code in {
        "insufficient_quota",
        "quota_exceeded",
        "rate_limit_exceeded",
        "usage_limit_reached",
        "usage_not_included",
    }:
        normalized_type = "rate_limit_error"
    else:
        normalized_type = "server_error"

    if normalized_code in {
        "insufficient_quota",
        "quota_exceeded",
        "rate_limit_exceeded",
        "usage_limit_reached",
        "usage_not_included",
    }:
        normalized_message = "Upstream request was rate limited"
    elif normalized_code == "invalid_api_key":
        normalized_message = "Upstream authentication failed"
    elif normalized_code in {"forbidden", "insufficient_permissions", "permission_error"}:
        normalized_message = "Upstream request was forbidden"
    elif normalized_code == "not_found":
        normalized_message = "Upstream resource was not found"
    elif normalized_code in {"invalid_request_error", "previous_response_not_found"}:
        normalized_message = "Upstream rejected the request"
    elif normalized_code in {
        "account_deactivated",
        "account_deleted",
        "account_suspended",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }:
        normalized_message = "Upstream account is unavailable"
    elif normalized_code in {"not_implemented", "unsupported_transport"}:
        normalized_message = "Upstream transport is not supported"
    else:
        normalized_message = "Upstream request failed"

    sanitized: dict[str, JsonValue] = {
        "code": normalized_code,
        "message": normalized_message,
        "type": normalized_type,
    }
    if param_candidate in _PUBLIC_UPSTREAM_ERROR_PARAMS:
        sanitized["param"] = param_candidate
    raw_plan_type = error.get("plan_type")
    plan_type_candidate = raw_plan_type.strip().lower() if isinstance(raw_plan_type, str) else ""
    if plan_type_candidate in _PUBLIC_RATE_LIMIT_PLAN_TYPES:
        sanitized["plan_type"] = plan_type_candidate
    for metadata_key in ("resets_at", "resets_in_seconds"):
        metadata_value = error.get(metadata_key)
        if isinstance(metadata_value, int | float) and not isinstance(metadata_value, bool):
            sanitized[metadata_key] = metadata_value
    return sanitized


def _generic_upstream_error_detail() -> dict[str, JsonValue]:
    return {
        "code": "upstream_error",
        "message": "Upstream request failed",
        "type": "server_error",
    }


def sanitize_upstream_openai_error_envelope(
    payload: Mapping[str, object],
) -> OpenAIErrorEnvelope:
    raw_error = payload.get("error")
    error_detail = (
        sanitize_upstream_websocket_error_detail(cast(Mapping[str, JsonValue], raw_error))
        if isinstance(raw_error, Mapping)
        else _generic_upstream_error_detail()
    )
    return cast(OpenAIErrorEnvelope, {"error": error_detail})


def sanitize_upstream_websocket_event_payload(
    payload: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    sanitized_payload = dict(payload)
    event_type = payload.get("type")
    if event_type == "error":
        raw_error = payload.get("error")
        sanitized_payload["error"] = (
            sanitize_upstream_websocket_error_detail(raw_error)
            if isinstance(raw_error, dict)
            else _generic_upstream_error_detail()
        )
        return sanitized_payload
    if event_type != "response.failed":
        return sanitized_payload
    raw_response = payload.get("response")
    if not isinstance(raw_response, dict):
        sanitized_payload["response"] = {
            "status": "failed",
            "error": _generic_upstream_error_detail(),
        }
        return sanitized_payload
    raw_error = raw_response.get("error")
    if isinstance(raw_error, dict):
        sanitized_response = dict(raw_response)
        sanitized_response["error"] = sanitize_upstream_websocket_error_detail(raw_error)
    else:
        sanitized_response: dict[str, JsonValue] = {"status": "failed"}
        response_id = raw_response.get("id")
        if isinstance(response_id, str):
            sanitized_response["id"] = response_id
        sanitized_response["error"] = _generic_upstream_error_detail()
    sanitized_payload["response"] = sanitized_response
    return sanitized_payload


def sanitize_upstream_websocket_event_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        return json.dumps(
            {"type": "error", "error": _generic_upstream_error_detail()},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    payload = cast(dict[str, JsonValue], parsed)
    sanitized = sanitize_upstream_websocket_event_payload(payload)
    if sanitized == payload:
        return text
    return json.dumps(sanitized, ensure_ascii=True, separators=(",", ":"))
