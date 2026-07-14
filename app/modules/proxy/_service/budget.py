from __future__ import annotations

import time
from typing import NoReturn

from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.errors import ResponseFailedEvent, openai_error, response_failed_event
from app.modules.proxy._service.support import _HTTPBridgeSession, _WebSocketRequestState
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error


def _remaining_budget_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _ensure_request_budget_remaining(
    request_state: _WebSocketRequestState,
    default_budget_seconds: float,
) -> float:
    remaining = _remaining_budget_seconds(_request_deadline_at(request_state, default_budget_seconds))
    if remaining <= 0:
        _raise_proxy_budget_exhausted()
    return remaining


def _request_budget_seconds(request_state: _WebSocketRequestState, default_budget_seconds: float) -> float:
    configured = request_state.request_budget_seconds
    if configured is not None and configured > 0:
        return configured
    return default_budget_seconds


def _request_deadline_at(request_state: _WebSocketRequestState, default_budget_seconds: float) -> float:
    if request_state.request_deadline_at is not None:
        return request_state.request_deadline_at
    return request_state.started_at + _request_budget_seconds(request_state, default_budget_seconds)


def _set_request_budget(
    request_state: _WebSocketRequestState,
    budget_seconds: float,
    *,
    restart_from_now: bool = False,
) -> None:
    request_state.request_budget_seconds = budget_seconds
    deadline_start = time.monotonic() if restart_from_now else request_state.started_at
    request_state.request_deadline_at = deadline_start + budget_seconds


def _inherit_request_budget(
    source_request_state: _WebSocketRequestState,
    target_request_state: _WebSocketRequestState,
) -> None:
    target_request_state.started_at = source_request_state.started_at
    target_request_state.request_budget_seconds = source_request_state.request_budget_seconds
    target_request_state.request_deadline_at = source_request_state.request_deadline_at


def _http_bridge_request_budget_seconds(
    session: _HTTPBridgeSession,
    request_state: _WebSocketRequestState,
    settings: Settings,
) -> float:
    del session, request_state
    return settings.http_responses_session_bridge_request_budget_seconds


def _websocket_connect_deadline(request_state: _WebSocketRequestState, budget_seconds: float) -> float:
    if request_state.request_deadline_at is not None:
        return request_state.request_deadline_at
    started_at = request_state.started_at if request_state.started_at > 0 else time.monotonic()
    return started_at + budget_seconds


def _proxy_request_timeout_event(request_id: str) -> ResponseFailedEvent:
    return response_failed_event(
        "upstream_request_timeout",
        "Proxy request budget exhausted",
        response_id=request_id,
    )


def _raise_proxy_budget_exhausted() -> NoReturn:
    raise ProxyResponseError(
        502,
        openai_error("upstream_unavailable", "Proxy request budget exhausted"),
    )


def _raise_proxy_unavailable(message: str) -> NoReturn:
    raise ProxyResponseError(
        502,
        openai_error("upstream_unavailable", message),
    )


def _is_proxy_budget_exhausted_error(exc: ProxyResponseError) -> bool:
    error = _parse_openai_error(exc.payload)
    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
    error_message = error.message if error else None
    return error_code == "upstream_unavailable" and error_message == "Proxy request budget exhausted"
