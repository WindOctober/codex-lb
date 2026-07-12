from __future__ import annotations

from collections import deque

from app.modules.proxy._service.support import _WebSocketRequestState
from app.modules.proxy._service.websocket.events import (
    _http_error_status_from_payload,
    _match_websocket_request_state_for_anonymous_event,
    _matching_websocket_request_states_for_previous_response_error,
    _pop_terminal_websocket_request_state,
)


def _request_state(
    request_id: str,
    *,
    response_id: str | None = None,
    previous_response_id: str | None = None,
    awaiting_response_created: bool = False,
) -> _WebSocketRequestState:
    return _WebSocketRequestState(
        request_id=request_id,
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        response_id=response_id,
        previous_response_id=previous_response_id,
        awaiting_response_created=awaiting_response_created,
    )


def test_http_error_status_from_payload_requires_integer_status() -> None:
    assert _http_error_status_from_payload({"status": 429}) == 429
    assert _http_error_status_from_payload({"status": "429"}) is None
    assert _http_error_status_from_payload(None) is None


def test_previous_response_match_prefers_explicit_hint() -> None:
    first = _request_state("first", previous_response_id="resp_first")
    second = _request_state("second", previous_response_id="resp_second")

    matches = _matching_websocket_request_states_for_previous_response_error(
        deque([first, second]),
        previous_response_id_hint="resp_second",
    )

    assert matches == [second]


def test_anonymous_event_only_matches_unambiguous_pending_request() -> None:
    assigned = _request_state("assigned", response_id="resp_assigned")
    unresolved = _request_state("unresolved")

    match = _match_websocket_request_state_for_anonymous_event(
        deque([assigned, unresolved]),
        prefer_previous_response_not_found=False,
    )

    assert match is unresolved


def test_pop_terminal_state_removes_exact_response_owner() -> None:
    first = _request_state("first", response_id="resp_first")
    second = _request_state("second", response_id="resp_second")
    pending = deque([first, second])

    popped = _pop_terminal_websocket_request_state(
        pending,
        response_id="resp_second",
        fallback_request_state=None,
    )

    assert popped is second
    assert list(pending) == [first]
