from __future__ import annotations

from dataclasses import fields

from app.modules.proxy import service as proxy_service
from app.modules.proxy._service import support


def test_proxy_service_reexports_shared_support_state() -> None:
    names = (
        "_AffinityPolicy",
        "_WebSocketRequestState",
        "_HTTPBridgeSessionKey",
        "_HTTPBridgeOwnerForward",
        "_HTTPBridgeSession",
        "_WebSocketUpstreamControl",
        "_DownstreamWebSocketActivity",
    )

    for name in names:
        assert getattr(proxy_service, name) is getattr(support, name)


def test_http_bridge_session_key_derives_strength_from_affinity_kind() -> None:
    assert support._HTTPBridgeSessionKey("turn_state_header", "turn", None).strength == "hard"
    assert support._HTTPBridgeSessionKey("session_header", "session", None).strength == "hard"
    assert support._HTTPBridgeSessionKey("prompt_cache", "cache", None).strength == "soft"
    assert support._HTTPBridgeSessionKey("request", "request", None, strength="hard").strength == "hard"


def test_websocket_request_state_preserves_required_field_order_and_defaults() -> None:
    field_names = tuple(field.name for field in fields(support._WebSocketRequestState))
    assert field_names[:6] == (
        "request_id",
        "model",
        "service_tier",
        "reasoning_effort",
        "api_key_reservation",
        "started_at",
    )
    assert field_names[-4:] == (
        "http_bridge_upstream_first_text_at",
        "http_bridge_downstream_first_event_at",
        "http_bridge_downstream_first_text_at",
        "http_bridge_latency_breakdown_logged",
    )

    state = support._WebSocketRequestState(
        request_id="request",
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
    )
    assert state.transport == "websocket"
    assert state.request_stage == "first_turn"
    assert state.fresh_upstream_request_is_retry_safe is False
    assert state.affinity_policy == support._AffinityPolicy()


def test_stream_settlement_error_payload_preserves_explicit_error() -> None:
    explicit_error = {"message": "capacity", "code": "rate_limit_exceeded"}
    settlement = support._StreamSettlement(error=explicit_error)
    assert support._stream_settlement_error_payload(settlement) is explicit_error
    assert support._stream_settlement_error_payload(support._StreamSettlement(error_message="fallback")) == {
        "message": "fallback"
    }
