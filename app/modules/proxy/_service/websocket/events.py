from __future__ import annotations

import json
import re
from collections import deque
from typing import Protocol, cast

import anyio

from app.core.balancer import failover_decision
from app.core.balancer.types import UpstreamError
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.core.errors import OpenAIErrorDetail, OpenAIErrorEnvelope, openai_error, response_failed_event
from app.core.exceptions import AppError
from app.core.openai.models import OpenAIEvent
from app.core.openai.parsing import parse_sse_event
from app.core.types import JsonValue
from app.core.utils.sse import format_sse_event, parse_sse_data_json
from app.modules.proxy._service.affinity import _normalize_session_id as _normalize_session_id
from app.modules.proxy._service.observability import _record_continuity_fail_closed
from app.modules.proxy._service.support import (
    _ACCOUNT_RECOVERY_RETRY_CODES,
    _event_type_from_payload,
    _WebSocketRequestState,
    _WebSocketUpstreamControl,
)
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error, classify_upstream_failure

_WEBSOCKET_TRANSPARENT_REPLAY_ERROR_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "usage_limit_reached",
        "insufficient_quota",
        "usage_not_included",
        "quota_exceeded",
    }
)


class _ContinuityFailClosedRecorder(Protocol):
    def __call__(
        self,
        *,
        surface: str,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None = None,
        upstream_error_code: str | None = None,
    ) -> None: ...


def _wrapped_websocket_error_event(
    status_code: int,
    payload: OpenAIErrorEnvelope,
) -> dict[str, JsonValue]:
    error_payload = cast(JsonValue, dict(payload["error"]))
    return cast(
        dict[str, JsonValue],
        {
            "type": "error",
            "status": status_code,
            "error": error_payload,
        },
    )


def _serialize_websocket_error_event(payload: dict[str, JsonValue]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _parse_websocket_payload(text: str) -> dict[str, JsonValue] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _is_websocket_response_create(payload: dict[str, JsonValue]) -> bool:
    return payload.get("type") == "response.create"


def _app_error_to_websocket_event(exc: AppError) -> dict[str, JsonValue]:
    return _wrapped_websocket_error_event(
        exc.status_code,
        openai_error(exc.code, exc.message, error_type=getattr(exc, "error_type", "server_error")),
    )


def _http_error_status_from_payload(payload: dict[str, JsonValue] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, int):
        return status
    return None


def _message_mentions_previous_response_id(message: str | None, previous_response_id: str | None) -> bool:
    if message is None or previous_response_id is None:
        return False
    normalized_message = " ".join(message.split())
    normalized_previous_response_id = previous_response_id.strip()
    if not normalized_previous_response_id:
        return False
    identifier_pattern = re.escape(normalized_previous_response_id)
    return (
        re.search(
            rf"(?<![A-Za-z0-9_-]){identifier_pattern}(?![A-Za-z0-9_-])",
            normalized_message,
        )
        is not None
    )


def _find_websocket_request_state_by_response_id(
    pending_requests: deque[_WebSocketRequestState],
    response_id: str,
) -> _WebSocketRequestState | None:
    for request_state in pending_requests:
        if request_state.response_id == response_id:
            return request_state
    return None


def _match_websocket_request_state_for_anonymous_event(
    pending_requests: deque[_WebSocketRequestState],
    *,
    prefer_previous_response_not_found: bool,
    previous_response_id_hint: str | None = None,
    error_message: str | None = None,
) -> _WebSocketRequestState | None:
    if prefer_previous_response_not_found:
        return _match_websocket_request_state_for_previous_response_error(
            pending_requests,
            previous_response_id_hint=previous_response_id_hint,
            error_message=error_message,
        )

    if len(pending_requests) == 1:
        return pending_requests[0]

    unresolved_requests = [request_state for request_state in pending_requests if request_state.response_id is None]
    if len(unresolved_requests) == 1:
        return unresolved_requests[0]
    return None


def _match_websocket_request_state_for_precreated_terminal_event(
    pending_requests: deque[_WebSocketRequestState],
) -> _WebSocketRequestState | None:
    unresolved_requests = [
        request_state
        for request_state in pending_requests
        if request_state.response_id is None and request_state.awaiting_response_created
    ]
    if len(unresolved_requests) == 1:
        return unresolved_requests[0]
    return None


def _match_websocket_request_state_for_previous_response_error(
    pending_requests: deque[_WebSocketRequestState],
    *,
    previous_response_id_hint: str | None = None,
    error_message: str | None = None,
) -> _WebSocketRequestState | None:
    matching_requests = _matching_websocket_request_states_for_previous_response_error(
        pending_requests,
        previous_response_id_hint=previous_response_id_hint,
        error_message=error_message,
    )
    if len(matching_requests) == 1:
        return matching_requests[0]
    return None


def _matching_websocket_request_states_for_previous_response_error(
    pending_requests: deque[_WebSocketRequestState],
    *,
    previous_response_id_hint: str | None = None,
    error_message: str | None = None,
) -> list[_WebSocketRequestState]:
    followup_requests = [
        request_state for request_state in pending_requests if request_state.previous_response_id is not None
    ]
    if not followup_requests:
        return []
    if previous_response_id_hint is not None:
        matching_requests = [
            request_state
            for request_state in followup_requests
            if request_state.previous_response_id == previous_response_id_hint
        ]
        if matching_requests:
            return matching_requests
    if error_message is not None:
        matching_requests = [
            request_state
            for request_state in followup_requests
            if _message_mentions_previous_response_id(error_message, request_state.previous_response_id)
        ]
        if matching_requests:
            return matching_requests
    unresolved_followups = [request_state for request_state in followup_requests if request_state.response_id is None]
    if len(unresolved_followups) == 1:
        return unresolved_followups
    if len(unresolved_followups) > 1:
        unique_previous_response_ids = {
            request_state.previous_response_id
            for request_state in unresolved_followups
            if request_state.previous_response_id
        }
        if len(unique_previous_response_ids) == 1:
            return unresolved_followups
    return []


def _pop_terminal_websocket_request_state(
    pending_requests: deque[_WebSocketRequestState],
    *,
    response_id: str | None,
    fallback_request_state: _WebSocketRequestState | None,
    prefer_previous_response_not_found: bool = False,
    previous_response_id_hint: str | None = None,
    error_message: str | None = None,
    allow_precreated_terminal_fallback: bool = False,
) -> _WebSocketRequestState | None:
    if response_id is not None:
        request_state = _find_websocket_request_state_by_response_id(pending_requests, response_id)
        if request_state is not None:
            pending_requests.remove(request_state)
            return request_state
    if fallback_request_state is not None and fallback_request_state in pending_requests:
        pending_requests.remove(fallback_request_state)
        return fallback_request_state
    if response_id is not None and allow_precreated_terminal_fallback:
        request_state = _match_websocket_request_state_for_precreated_terminal_event(pending_requests)
        if request_state is not None and request_state in pending_requests:
            pending_requests.remove(request_state)
            return request_state
    if response_id is not None and prefer_previous_response_not_found:
        request_state = _match_websocket_request_state_for_previous_response_error(
            pending_requests,
            previous_response_id_hint=previous_response_id_hint,
            error_message=error_message,
        )
        if request_state is not None and request_state in pending_requests:
            pending_requests.remove(request_state)
            return request_state
    if response_id is None:
        request_state = _match_websocket_request_state_for_anonymous_event(
            pending_requests,
            prefer_previous_response_not_found=prefer_previous_response_not_found,
            previous_response_id_hint=previous_response_id_hint,
            error_message=error_message,
        )
        if request_state is not None and request_state in pending_requests:
            pending_requests.remove(request_state)
            return request_state
    return None


def _normalize_http_bridge_error_event(
    *,
    event: OpenAIEvent | None,
    payload: dict[str, JsonValue] | None,
    request_state: _WebSocketRequestState | None,
) -> tuple[str, dict[str, JsonValue] | None, OpenAIEvent | None, str]:
    error_code_value: str | None = None
    error_type_value: str | None = None
    error_message_value: str | None = None
    error_param_value: str | None = None
    rate_limit_metadata: OpenAIErrorDetail = {}

    if event is not None and event.error is not None:
        error_code_value = event.error.code
        error_type_value = event.error.type
        error_message_value = event.error.message
        error_param_value = event.error.param
    elif isinstance(payload, dict):
        payload_error = payload.get("error")
        if isinstance(payload_error, dict):
            code_value = payload_error.get("code")
            if isinstance(code_value, str):
                stripped = code_value.strip()
                if stripped:
                    error_code_value = stripped
            type_value = payload_error.get("type")
            if isinstance(type_value, str):
                stripped = type_value.strip()
                if stripped:
                    error_type_value = stripped
            message_value = payload_error.get("message")
            if isinstance(message_value, str):
                stripped = message_value.strip()
                if stripped:
                    error_message_value = stripped
            param_value = payload_error.get("param")
            if isinstance(param_value, str):
                stripped = param_value.strip()
                if stripped:
                    error_param_value = stripped

    if isinstance(payload, dict):
        raw_error = payload.get("error")
        if isinstance(raw_error, dict):
            plan_type = raw_error.get("plan_type")
            if isinstance(plan_type, str):
                rate_limit_metadata["plan_type"] = plan_type
            resets_at = raw_error.get("resets_at")
            if isinstance(resets_at, int | float):
                rate_limit_metadata["resets_at"] = resets_at
            resets_in = raw_error.get("resets_in_seconds")
            if isinstance(resets_in, int | float):
                rate_limit_metadata["resets_in_seconds"] = resets_in

    normalized_error_code = _normalize_error_code(error_code_value, error_type_value) or "upstream_error"
    normalized_error_type = error_type_value or "server_error"
    normalized_error_message = error_message_value or "Upstream error"

    normalized_response_id = None
    if request_state is not None:
        normalized_response_id = request_state.response_id or request_state.request_id

    normalized_event = response_failed_event(
        normalized_error_code,
        normalized_error_message,
        error_type=normalized_error_type,
        response_id=normalized_response_id,
        error_param=error_param_value,
    )
    if rate_limit_metadata:
        normalized_event["response"]["error"].update(rate_limit_metadata)
    normalized_event_block = format_sse_event(normalized_event)
    normalized_payload = parse_sse_data_json(normalized_event_block)
    parsed_event = parse_sse_event(normalized_event_block)
    return normalized_event_block, normalized_payload, parsed_event, "response.failed"


def _websocket_response_id(event: OpenAIEvent | None, payload: dict[str, JsonValue] | None) -> str | None:
    if event is not None and event.response is not None and event.response.id:
        return event.response.id
    if not isinstance(payload, dict):
        return None
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    response_id = response.get("id")
    if not isinstance(response_id, str):
        return None
    stripped = response_id.strip()
    return stripped or None


def _websocket_event_error_code(event_type: str | None, payload: dict[str, JsonValue] | None) -> str | None:
    error = _websocket_event_error_payload(event_type, payload)
    if not isinstance(error, dict):
        return None
    code_value = error.get("code")
    if not isinstance(code_value, str):
        return None
    stripped = code_value.strip()
    return stripped or None


def _websocket_event_error_type(event_type: str | None, payload: dict[str, JsonValue] | None) -> str | None:
    error = _websocket_event_error_payload(event_type, payload)
    if not isinstance(error, dict):
        return None
    type_value = error.get("type")
    if not isinstance(type_value, str):
        return None
    stripped = type_value.strip()
    return stripped or None


def _websocket_event_error_param(event_type: str | None, payload: dict[str, JsonValue] | None) -> str | None:
    error = _websocket_event_error_payload(event_type, payload)
    if not isinstance(error, dict):
        return None
    param_value = error.get("param")
    if not isinstance(param_value, str):
        return None
    stripped = param_value.strip()
    return stripped or None


def _websocket_event_error_message(event_type: str | None, payload: dict[str, JsonValue] | None) -> str | None:
    error = _websocket_event_error_payload(event_type, payload)
    if not isinstance(error, dict):
        return None
    message_value = error.get("message")
    if not isinstance(message_value, str):
        return None
    stripped = message_value.strip()
    return stripped or None


def _is_previous_response_not_found_message(message: str | None) -> bool:
    if message is None:
        return False
    normalized = " ".join(message.lower().split())
    return "previous response" in normalized and "not found" in normalized


def _previous_response_id_from_not_found_message(message: str | None) -> str | None:
    if message is None:
        return None
    normalized = " ".join(message.split())
    match = re.search(
        r"""previous\s+response\s+with\s+id\s+['"](?P<response_id>[^'"]+)['"]\s+not\s+found""",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        return None
    response_id = match.group("response_id").strip()
    return response_id or None


def _websocket_precreated_retry_error_code(
    request_state: _WebSocketRequestState | None,
    *,
    event_type: str | None,
    payload: dict[str, JsonValue] | None,
    has_other_pending_requests: bool,
) -> str | None:
    if request_state is None:
        return None
    if has_other_pending_requests:
        return None
    if request_state.response_id is not None:
        return None
    if not request_state.awaiting_response_created:
        return None
    if not request_state.request_text:
        return None
    if request_state.replay_count >= 1:
        return None
    if event_type not in {"error", "response.failed"}:
        return None

    error_code = _normalize_error_code(
        _websocket_event_error_code(event_type, payload),
        _websocket_event_error_type(event_type, payload),
    )
    error_param = _websocket_event_error_param(event_type, payload)
    error_message = _websocket_event_error_message(event_type, payload)
    if _is_previous_response_not_found_error(
        code=error_code,
        param=error_param,
        message=error_message,
    ):
        return "stream_incomplete"
    if error_code in _WEBSOCKET_TRANSPARENT_REPLAY_ERROR_CODES:
        return error_code
    if _should_failover_first_event_failure(
        error_code=error_code,
        error={"message": error_message or "Upstream error"},
        http_status=_http_error_status_from_payload(payload),
    ):
        return error_code
    return None


def _http_bridge_precreated_failover_error_code(
    request_state: _WebSocketRequestState | None,
    *,
    event_type: str | None,
    payload: dict[str, JsonValue] | None,
    has_other_pending_requests: bool,
) -> str | None:
    if request_state is None:
        return None
    if has_other_pending_requests:
        return None
    if request_state.response_id is not None:
        return None
    if not request_state.awaiting_response_created:
        return None
    if not request_state.request_text:
        return None
    if request_state.replay_count >= 1:
        return None
    if event_type not in {"error", "response.failed"}:
        return None

    error_code = _normalize_error_code(
        _websocket_event_error_code(event_type, payload),
        _websocket_event_error_type(event_type, payload),
    )
    error_message = _websocket_event_error_message(event_type, payload)
    if not _should_failover_first_event_failure(
        error_code=error_code,
        error={"message": error_message or "Upstream error"},
        http_status=_http_error_status_from_payload(payload),
    ):
        return None
    return error_code


def _http_bridge_no_text_failover_error_code(
    request_state: _WebSocketRequestState | None,
    *,
    event_type: str | None,
    payload: dict[str, JsonValue] | None,
) -> str | None:
    if request_state is None:
        return None
    if request_state.response_id is None:
        return None
    if not request_state.request_text:
        return None
    if request_state.replay_count >= 1:
        return None
    if request_state.latency_first_token_ms is not None:
        return None
    if request_state.http_bridge_upstream_first_text_at is not None:
        return None
    if request_state.http_bridge_downstream_first_text_at is not None:
        return None
    if event_type not in {"error", "response.failed"}:
        return None

    error_code = _normalize_error_code(
        _websocket_event_error_code(event_type, payload),
        _websocket_event_error_type(event_type, payload),
    )
    error_message = _websocket_event_error_message(event_type, payload)
    if not _should_failover_first_event_failure(
        error_code=error_code,
        error={"message": error_message or "Upstream error"},
        http_status=_http_error_status_from_payload(payload),
    ):
        return None
    return error_code


def _should_failover_first_event_failure(
    *,
    error_code: str,
    error: UpstreamError,
    http_status: int | None,
) -> bool:
    classified = classify_upstream_failure(
        error_code=error_code,
        error=error,
        http_status=http_status,
        phase="first_event",
    )
    action = failover_decision(
        failure_class=classified["failure_class"],
        downstream_visible=False,
        candidates_remaining=1,
    )
    return action == "failover_next"


async def _pop_replayable_precreated_websocket_request_state(
    pending_requests: deque[_WebSocketRequestState],
    *,
    pending_lock: anyio.Lock,
) -> _WebSocketRequestState | None:
    async with pending_lock:
        if len(pending_requests) != 1:
            return None
        request_state = pending_requests[0]
        if request_state.response_id is not None:
            return None
        if not request_state.awaiting_response_created:
            return None
        if not request_state.request_text:
            return None
        if request_state.replay_count >= 1:
            return None
        pending_requests.popleft()
    request_state.replay_count += 1
    request_state.awaiting_response_created = True
    request_state.response_id = None
    return request_state


def _is_previous_response_not_found_error(
    *,
    code: str | None,
    param: str | None,
    message: str | None,
) -> bool:
    if code == "previous_response_not_found":
        return True
    if code != "invalid_request_error" or param != "previous_response_id":
        return False
    return _is_previous_response_not_found_message(message)


def _websocket_event_error_payload(
    event_type: str | None,
    payload: dict[str, JsonValue] | None,
) -> dict[str, JsonValue] | None:
    if not isinstance(payload, dict):
        return None
    if event_type == "error":
        error = payload.get("error")
    elif event_type == "response.failed":
        response = payload.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    else:
        return None
    return cast(dict[str, JsonValue], error) if isinstance(error, dict) else None


def _maybe_rewrite_websocket_previous_response_not_found_event(
    *,
    request_state: _WebSocketRequestState,
    event: OpenAIEvent | None,
    payload: dict[str, JsonValue] | None,
    event_type: str | None,
    upstream_control: _WebSocketUpstreamControl,
    original_text: str,
    record_continuity_fail_closed: _ContinuityFailClosedRecorder = _record_continuity_fail_closed,
) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]:
    if request_state.previous_response_id is None:
        return event, payload, event_type, original_text

    error_code = _websocket_event_error_code(event_type, payload)
    error_param = _websocket_event_error_param(event_type, payload)
    error_message = _websocket_event_error_message(event_type, payload)
    should_rewrite = _is_previous_response_not_found_error(
        code=error_code,
        param=error_param,
        message=error_message,
    )
    if not should_rewrite:
        return event, payload, event_type, original_text

    upstream_control.reconnect_requested = True
    record_continuity_fail_closed(
        surface="websocket_stream",
        reason="previous_response_not_found",
        previous_response_id=request_state.previous_response_id,
        session_id=request_state.session_id,
        upstream_error_code=error_code,
    )
    rewritten_event_payload = response_failed_event(
        "stream_incomplete",
        "Upstream websocket closed before response.completed",
        error_type="server_error",
        response_id=request_state.response_id or request_state.request_id,
    )
    rewritten_text = json.dumps(rewritten_event_payload, ensure_ascii=True, separators=(",", ":"))
    rewritten_event_block = format_sse_event(rewritten_event_payload)
    rewritten_payload = parse_sse_data_json(rewritten_event_block)
    rewritten_event = parse_sse_event(rewritten_event_block)
    rewritten_event_type = _event_type_from_payload(rewritten_event, rewritten_payload)
    return rewritten_event, rewritten_payload, rewritten_event_type, rewritten_text


def _rewrite_websocket_previous_response_owner_unavailable_event(
    *,
    request_state: _WebSocketRequestState,
    record_continuity_fail_closed: _ContinuityFailClosedRecorder = _record_continuity_fail_closed,
) -> tuple[OpenAIEvent | None, dict[str, JsonValue] | None, str | None, str]:
    record_continuity_fail_closed(
        surface="websocket_stream",
        reason="owner_account_unavailable",
        previous_response_id=request_state.previous_response_id,
        session_id=request_state.session_id,
    )
    rewritten_event_payload = response_failed_event(
        "upstream_unavailable",
        "Previous response owner account is unavailable; retry later.",
        error_type="server_error",
        response_id=request_state.response_id or request_state.request_id,
    )
    rewritten_text = json.dumps(rewritten_event_payload, ensure_ascii=True, separators=(",", ":"))
    rewritten_event_block = format_sse_event(rewritten_event_payload)
    rewritten_payload = parse_sse_data_json(rewritten_event_block)
    rewritten_event = parse_sse_event(rewritten_event_block)
    rewritten_event_type = _event_type_from_payload(rewritten_event, rewritten_payload)
    return rewritten_event, rewritten_payload, rewritten_event_type, rewritten_text


def _sanitize_websocket_connect_failure(
    *,
    request_state: _WebSocketRequestState,
    status_code: int,
    payload: OpenAIErrorEnvelope,
    error_code: str,
    error_message: str,
    record_continuity_fail_closed: _ContinuityFailClosedRecorder = _record_continuity_fail_closed,
) -> tuple[int, OpenAIErrorEnvelope, str, str]:
    if request_state.previous_response_id is None:
        return status_code, payload, error_code, error_message

    parsed_error = _parse_openai_error(payload)
    normalized_code = _normalize_error_code(
        parsed_error.code if parsed_error else error_code,
        parsed_error.type if parsed_error else None,
    )
    normalized_message = parsed_error.message if parsed_error and parsed_error.message else error_message
    if not _is_previous_response_not_found_error(
        code=normalized_code,
        param=parsed_error.param if parsed_error else None,
        message=normalized_message,
    ):
        return status_code, payload, error_code, error_message

    rewritten_message = "Upstream websocket closed before response.completed"
    record_continuity_fail_closed(
        surface="websocket_connect",
        reason="previous_response_not_found",
        previous_response_id=request_state.previous_response_id,
        session_id=request_state.session_id,
        upstream_error_code=normalized_code,
    )
    return (
        502,
        openai_error(
            "stream_incomplete",
            rewritten_message,
            error_type="server_error",
        ),
        "stream_incomplete",
        rewritten_message,
    )


def _rewrite_previous_response_stream_error(
    *,
    previous_response_id: str | None,
    preferred_account_id: str | None,
    error_code: str | None,
    error_type: str | None,
    error_message: str | None,
    error_param: str | None,
    record_continuity_fail_closed: _ContinuityFailClosedRecorder = _record_continuity_fail_closed,
) -> tuple[str, str, str | None] | None:
    if previous_response_id is None:
        return None
    if _is_previous_response_not_found_error(
        code=error_code,
        param=error_param,
        message=error_message,
    ):
        record_continuity_fail_closed(
            surface="http_stream",
            reason="previous_response_not_found",
            previous_response_id=previous_response_id,
            upstream_error_code=error_code,
        )
        return (
            "stream_incomplete",
            "Upstream websocket closed before response.completed",
            None,
        )
    normalized_code = _normalize_error_code(error_code, error_type)
    if preferred_account_id is not None and normalized_code in _ACCOUNT_RECOVERY_RETRY_CODES:
        record_continuity_fail_closed(
            surface="http_stream",
            reason="owner_account_unavailable",
            previous_response_id=previous_response_id,
            upstream_error_code=normalized_code,
        )
        return (
            "upstream_unavailable",
            "Previous response owner account is unavailable; retry later.",
            normalized_code,
        )
    return None


def _build_rewritten_stream_response_failed_event(
    *,
    response_id: str,
    error_code: str,
    error_message: str,
) -> tuple[str, OpenAIEvent | None, dict[str, JsonValue] | None, str | None]:
    rewritten_event_payload = response_failed_event(
        error_code,
        error_message,
        error_type="server_error",
        response_id=response_id,
    )
    rewritten_event_block = format_sse_event(rewritten_event_payload)
    rewritten_payload = parse_sse_data_json(rewritten_event_block)
    rewritten_event = parse_sse_event(rewritten_event_block)
    rewritten_event_type = _event_type_from_payload(rewritten_event, rewritten_payload)
    return rewritten_event_block, rewritten_event, rewritten_payload, rewritten_event_type


def _assign_websocket_response_id(
    pending_requests: deque[_WebSocketRequestState],
    response_id: str | None,
) -> _WebSocketRequestState | None:
    if response_id is None:
        return None
    existing = _find_websocket_request_state_by_response_id(pending_requests, response_id)
    if existing is not None:
        return existing
    for request_state in pending_requests:
        if request_state.response_id is None:
            request_state.response_id = response_id
            return request_state
    return None


def _has_other_precreated_pending_requests(
    pending_requests: deque[_WebSocketRequestState],
    current_request_state: _WebSocketRequestState,
) -> bool:
    return any(
        request_state is not current_request_state
        and request_state.response_id is None
        and request_state.awaiting_response_created
        for request_state in pending_requests
    )


def _pop_matching_websocket_request_states(
    pending_requests: deque[_WebSocketRequestState],
    matching_requests: list[_WebSocketRequestState],
) -> list[_WebSocketRequestState]:
    popped_requests: list[_WebSocketRequestState] = []
    for request_state in matching_requests:
        try:
            pending_requests.remove(request_state)
        except ValueError:
            continue
        popped_requests.append(request_state)
    return popped_requests


def _build_stream_incomplete_terminal_event_for_request(
    request_state: _WebSocketRequestState,
) -> tuple[str, str, OpenAIEvent | None, dict[str, JsonValue] | None, str | None]:
    event_block, event, payload, event_type = _build_rewritten_stream_response_failed_event(
        response_id=request_state.response_id or request_state.request_id,
        error_code="stream_incomplete",
        error_message="Upstream websocket closed before response.completed",
    )
    downstream_text = json.dumps(
        cast(
            dict[str, JsonValue],
            response_failed_event(
                "stream_incomplete",
                "Upstream websocket closed before response.completed",
                error_type="server_error",
                response_id=request_state.response_id or request_state.request_id,
            ),
        ),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return downstream_text, event_block, event, payload, event_type


def _upstream_websocket_disconnect_message(message: UpstreamWebSocketMessage) -> str:
    if message.kind == "error" and message.error:
        return f"Upstream websocket closed before response.completed: {message.error}"
    if message.close_code is not None:
        return f"Upstream websocket closed before response.completed (close_code={message.close_code})"
    return "Upstream websocket closed before response.completed"
