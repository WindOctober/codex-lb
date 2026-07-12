from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import aiohttp
import anyio
import pytest
from fastapi import WebSocket

from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket, UpstreamWebSocketMessage
from app.core.config.settings import Settings
from app.db.models import AccountStatus, HttpBridgeSessionState
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import keys as bridge_keys
from app.modules.proxy.http_bridge_forwarding import OwnerForwardRelayFailure
from app.modules.request_logs.repository import RequestLatencyHealthSnapshot, RequestLatencyHistoryBucket

pytestmark = pytest.mark.unit


def _make_app_settings(*, bridge_enabled: bool = True) -> Settings:
    return Settings(
        http_responses_session_bridge_enabled=bridge_enabled,
        http_responses_session_bridge_soft_shard_max_shards=1,
    )


def _make_api_key(
    *,
    key_id: str,
    assigned_account_ids: list[str],
    account_assignment_scope_enabled: bool | None = None,
) -> proxy_service.ApiKeyData:
    return proxy_service.ApiKeyData(
        id=key_id,
        name="bridge-key",
        key_prefix="sk-bridge",
        allowed_models=None,
        enforced_model=None,
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        last_used_at=None,
        account_assignment_scope_enabled=(
            bool(assigned_account_ids) if account_assignment_scope_enabled is None else account_assignment_scope_enabled
        ),
        assigned_account_ids=assigned_account_ids,
    )


def _make_http_bridge_session(
    *,
    key: proxy_service._HTTPBridgeSessionKey,
    account: Any,
    pending_count: int = 0,
) -> proxy_service._HTTPBridgeSession:
    pending_requests = deque(
        proxy_service._WebSocketRequestState(
            request_id=f"req-{index}",
            model="gpt-5.4",
            service_tier=None,
            reasoning_effort=None,
            api_key_reservation=None,
            started_at=time.monotonic(),
            event_queue=asyncio.Queue(),
            transport="http",
        )
        for index in range(pending_count)
    )
    return proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key=key.affinity_key),
        request_model="gpt-5.4",
        account=account,
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=pending_requests,
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=pending_count,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )


@pytest.mark.asyncio
async def test_stream_via_http_bridge_emits_keepalive_while_session_creation_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
        }
    )
    session_wait_started = asyncio.Event()

    async def slow_get_or_create(*_args: Any, **_kwargs: Any) -> proxy_service._HTTPBridgeSession:
        session_wait_started.set()
        await asyncio.sleep(10.0)
        raise AssertionError("session creation should be cancelled after keepalive assertion")

    monkeypatch.setattr(proxy_service, "_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "_HTTP_BRIDGE_STARTUP_KEEPALIVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", slow_get_or_create)

    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-keepalive-create"},
        codex_session_affinity=True,
        propagate_http_errors=False,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=None,
        suppress_text_done_events=False,
        idle_ttl_seconds=120.0,
        codex_idle_ttl_seconds=1800.0,
        max_sessions=8,
        queue_limit=4,
    )
    try:
        first = await asyncio.wait_for(anext(stream), timeout=1.0)
        payload_json = proxy_service.parse_sse_data_json(first)
        assert payload_json is not None
        assert payload_json["type"] == "codex.keepalive"
        assert payload_json["status"] == "waiting_for_account_capacity"
        assert payload_json["request_id"]
        assert await asyncio.wait_for(session_wait_started.wait(), timeout=1.0) is True
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_http_bridge_session_events_emits_keepalive_while_waiting_for_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-keepalive-events", None)
    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-keepalive-events",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )

    monkeypatch.setattr(proxy_service, "_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "_HTTP_BRIDGE_STARTUP_KEEPALIVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    stream = service._stream_http_bridge_session_events(
        session,
        request_state=request_state,
        text_data='{"type":"response.create"}',
        queue_limit=4,
        propagate_http_errors=False,
        downstream_turn_state=None,
    )
    try:
        first = await asyncio.wait_for(anext(stream), timeout=1.0)
        payload_json = proxy_service.parse_sse_data_json(first)
        assert payload_json is not None
        assert payload_json["type"] == "codex.keepalive"
        assert payload_json["status"] == "waiting_for_account_capacity"
        assert payload_json["request_id"] == "req-keepalive-events"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_http_bridge_session_events_keeps_initial_http_error_stream_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-keepalive-before-created", None)
    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-keepalive", status=AccountStatus.ACTIVE)),
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-keepalive-before-created",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="xhigh",
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )

    monkeypatch.setattr(proxy_service, "_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "_HTTP_BRIDGE_STARTUP_KEEPALIVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    stream = service._stream_http_bridge_session_events(
        session,
        request_state=request_state,
        text_data='{"type":"response.create"}',
        queue_limit=4,
        propagate_http_errors=True,
        downstream_turn_state=None,
    )
    try:
        first = await asyncio.wait_for(anext(stream), timeout=1.0)
        keepalive = proxy_service.parse_sse_data_json(first)
        assert keepalive is not None
        assert keepalive["type"] == "codex.keepalive"
        assert request_state.response_id is None

        failed_block = (
            'data: {"type":"response.failed","response":{"id":"resp-late-failure",'
            '"status":"failed","error":{"code":"rate_limit_exceeded","message":"slow down"}}}\n\n'
        )
        request_state.error_http_status_override = 429
        assert request_state.event_queue is not None
        await request_state.event_queue.put(failed_block)

        assert await asyncio.wait_for(anext(stream), timeout=1.0) == failed_block
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_http_bridge_session_events_preserves_http_status_before_first_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-immediate-http-error", None)
    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-immediate-error", status=AccountStatus.ACTIVE)),
    )
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-immediate-http-error",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="xhigh",
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=event_queue,
        transport="http",
    )
    request_state.error_http_status_override = 429
    await event_queue.put(
        'data: {"type":"response.failed","response":{"id":"resp-immediate-failure",'
        '"status":"failed","error":{"code":"rate_limit_exceeded","message":"slow down"}}}\n\n'
    )

    monkeypatch.setattr(proxy_service, "_ACCOUNT_SELECTION_RECOVERY_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "_HTTP_BRIDGE_STARTUP_KEEPALIVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    stream = service._stream_http_bridge_session_events(
        session,
        request_state=request_state,
        text_data='{"type":"response.create"}',
        queue_limit=4,
        propagate_http_errors=True,
        downstream_turn_state=None,
    )
    try:
        with pytest.raises(ProxyResponseError) as exc_info:
            await asyncio.wait_for(anext(stream), timeout=1.0)
        assert exc_info.value.status_code == 429
        assert exc_info.value.payload["error"]["code"] == "rate_limit_exceeded"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_reuses_live_local_session_without_ring_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache_key", "bridge-key", None)
    existing = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4-mini",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace()),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = existing
    monkeypatch.setattr(
        service,
        "_prune_http_bridge_sessions_locked",
        AsyncMock(),
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )

    async def _unexpected_owner_lookup(*args: object, **kwargs: object) -> str:
        raise AssertionError("live local session reuse must not hit the ring")

    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", _unexpected_owner_lookup)
    monkeypatch.setattr(proxy_service, "_active_http_bridge_instance_ring", _unexpected_owner_lookup)

    reused = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert reused is existing
    assert reused.request_model == "gpt-5.4"
    assert reused.last_used_at > 1.0


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_shards_busy_prompt_cache_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    base_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    busy_session = proxy_service._HTTPBridgeSession(
        key=base_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-key",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.5",
        account=cast(Any, SimpleNamespace(id="acc-busy", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([cast(Any, object())]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[base_key] = busy_session
    captured: dict[str, proxy_service._HTTPBridgeSessionKey] = {}

    async def fake_create_http_bridge_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        del kwargs
        captured["key"] = create_key
        return proxy_service._HTTPBridgeSession(
            key=create_key,
            headers={},
            affinity=proxy_service._AffinityPolicy(
                key="cache-key",
                kind=proxy_service.StickySessionKind.PROMPT_CACHE,
            ),
            request_model="gpt-5.5",
            account=cast(Any, SimpleNamespace(id="acc-shard", status=AccountStatus.ACTIVE)),
            upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
            upstream_control=proxy_service._WebSocketUpstreamControl(),
            pending_requests=deque(),
            pending_lock=anyio.Lock(),
            response_create_gate=None,
            queued_request_count=0,
            last_used_at=2.0,
            idle_ttl_seconds=120.0,
        )

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())

    async def fake_owner_instance(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "codex-lb"

    async def fake_active_ring(*args: object, **kwargs: object) -> tuple[str, tuple[str, ...]]:
        del args, kwargs
        return ("codex-lb", ("codex-lb",))

    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", fake_owner_instance)
    monkeypatch.setattr(proxy_service, "_active_http_bridge_instance_ring", fake_active_ring)
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            http_responses_session_bridge_soft_shard_pending_limit=1,
            http_responses_session_bridge_soft_shard_max_shards=4,
        ),
    )

    created = await service._get_or_create_http_bridge_session(
        base_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-key",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    shard_key = captured["key"]
    assert shard_key != base_key
    assert bridge_keys._http_bridge_soft_shard_index(shard_key) == 1
    assert shard_key.affinity_kind == "prompt_cache"
    assert created is service._http_bridge_sessions[shard_key]
    assert service._http_bridge_sessions[base_key] is busy_session


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_shards_promoted_prompt_cache_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    base_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    busy_session = proxy_service._HTTPBridgeSession(
        key=base_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="http_turn_123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.5",
        account=cast(Any, SimpleNamespace(id="acc-codex", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([cast(Any, object())]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=900.0,
        codex_session=True,
    )
    service._http_bridge_sessions[base_key] = busy_session
    captured: dict[str, proxy_service._HTTPBridgeSessionKey] = {}

    async def fake_create_http_bridge_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        del kwargs
        captured["key"] = create_key
        return proxy_service._HTTPBridgeSession(
            key=create_key,
            headers={},
            affinity=proxy_service._AffinityPolicy(
                key="cache-key",
                kind=proxy_service.StickySessionKind.PROMPT_CACHE,
            ),
            request_model="gpt-5.5",
            account=cast(Any, SimpleNamespace(id="acc-shard", status=AccountStatus.ACTIVE)),
            upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
            upstream_control=proxy_service._WebSocketUpstreamControl(),
            pending_requests=deque(),
            pending_lock=anyio.Lock(),
            response_create_gate=None,
            queued_request_count=0,
            last_used_at=2.0,
            idle_ttl_seconds=120.0,
        )

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="codex-lb"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("codex-lb", ("codex-lb",))),
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            http_responses_session_bridge_soft_shard_pending_limit=1,
            http_responses_session_bridge_soft_shard_max_shards=4,
        ),
    )

    created = await service._get_or_create_http_bridge_session(
        base_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-key",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    shard_key = captured["key"]
    assert shard_key != base_key
    assert bridge_keys._http_bridge_soft_shard_index(shard_key) == 1
    assert created is service._http_bridge_sessions[shard_key]
    assert service._http_bridge_sessions[base_key] is busy_session
    assert busy_session.closed is False


@pytest.mark.asyncio
async def test_http_bridge_runtime_snapshot_groups_pending_codex_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_at = datetime(2026, 1, 1, 12, 0, 0)

    class _RequestLogs:
        async def bridge_latency_health_snapshot(self) -> RequestLatencyHealthSnapshot:
            return RequestLatencyHealthSnapshot(
                anchor_at=anchor_at,
                latency_first_token_p50_ms=2_233,
                latency_first_token_p95_ms=5_200,
                latency_first_token_p99_ms=8_800,
                success_rate_percent=97.0,
                success_count=9_779,
                request_count=10_081,
                history=[
                    RequestLatencyHistoryBucket(
                        bucket_start=anchor_at,
                        latency_first_token_p50_ms=2_233,
                        latency_first_token_p95_ms=5_200,
                        success_count=10,
                        error_count=0,
                        status="ok",
                    )
                ],
            )

    class _RepoContext:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(request_logs=_RequestLogs())

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    service._repo_factory = cast(Any, lambda: _RepoContext())
    base_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    shard_key = proxy_service._http_bridge_soft_shard_key(base_key, 1)
    pending_state = proxy_service._WebSocketRequestState(
        request_id="req-pending",
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    service._http_bridge_sessions[base_key] = proxy_service._HTTPBridgeSession(
        key=base_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="http_turn_123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.5",
        account=cast(
            Any,
            SimpleNamespace(
                id="acc-codex",
                email="codex@example.com",
                status=AccountStatus.ACTIVE,
            ),
        ),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([pending_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=time.monotonic() - 5.0,
        idle_ttl_seconds=900.0,
        codex_session=True,
        previous_response_ids={"resp_1"},
        downstream_turn_state_aliases={"http_turn_123"},
        last_completed_response_id="resp_1",
    )
    service._http_bridge_sessions[shard_key] = proxy_service._HTTPBridgeSession(
        key=shard_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-key",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.5",
        account=cast(
            Any,
            SimpleNamespace(
                id="acc-shard",
                email="shard@example.com",
                status=AccountStatus.ACTIVE,
            ),
        ),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            http_responses_session_bridge_max_sessions=256,
            http_responses_session_bridge_soft_shard_pending_limit=1,
            http_responses_session_bridge_soft_shard_max_shards=64,
        ),
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )

    snapshot = await service.get_http_bridge_runtime_snapshot()

    assert snapshot.total_sessions == 2
    assert snapshot.pending_requests == 1
    assert snapshot.codex_sessions == 1
    assert snapshot.soft_shard_sessions == 1
    assert snapshot.config.account_model_session_limit == 20
    assert snapshot.free_account_model_session_slots == 38
    assert snapshot.reclaimable_idle_sessions == 1
    assert snapshot.available_parallel_capacity == 39
    assert snapshot.health.anchor_at == anchor_at
    assert snapshot.health.latency_first_token_p50_ms == 2_233
    assert snapshot.health.endpoint_ping_ms is not None
    assert snapshot.health.next_update_seconds == 60
    assert snapshot.health.history[0].status == "ok"
    assert snapshot.by_account[0].key == "acc-codex"
    assert snapshot.by_account[0].pending_requests == 1
    assert snapshot.shard_families[0].sessions == 2
    assert snapshot.sessions[0].codex_session is True
    assert snapshot.sessions[0].has_last_completed_response is True


def test_http_bridge_soft_sharding_only_applies_to_fresh_prompt_cache_requests() -> None:
    base_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    hard_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid", None)

    assert proxy_service._http_bridge_soft_sharding_allowed(
        base_key,
        incoming_turn_state=None,
        previous_response_id=None,
        forwarded_request=False,
    )
    assert not proxy_service._http_bridge_soft_sharding_allowed(
        base_key,
        incoming_turn_state=None,
        previous_response_id="resp_123",
        forwarded_request=False,
    )
    assert not proxy_service._http_bridge_soft_sharding_allowed(
        base_key,
        incoming_turn_state="http_turn_123",
        previous_response_id=None,
        forwarded_request=False,
    )
    assert not proxy_service._http_bridge_soft_sharding_allowed(
        hard_key,
        incoming_turn_state=None,
        previous_response_id=None,
        forwarded_request=False,
    )


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_replaces_live_session_when_account_is_no_longer_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("request", "bridge-key", "key-1")
    stale_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4-mini",
        account=cast(Any, SimpleNamespace(id="acc-stale", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    replacement_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-fresh", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = stale_session
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(
        service,
        "_create_http_bridge_session",
        AsyncMock(return_value=replacement_session),
    )
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    reused = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        api_key=_make_api_key(key_id="key-1", assigned_account_ids=["acc-fresh"]),
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert reused is replacement_session
    assert service._http_bridge_sessions[key] is replacement_session
    assert stale_session.closed is True
    assert any(call.args == (stale_session,) for call in close_session.await_args_list)


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_preserves_hard_session_account_on_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-hard", None)
    stale_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="sid-hard",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    replacement_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="sid-hard",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.5",
        account=cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    captured_kwargs: dict[str, object] = {}

    async def fake_create_http_bridge_session_compatible(
        _key: proxy_service._HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        captured_kwargs.update(kwargs)
        return replacement_session

    service._http_bridge_sessions[key] = stale_session
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create_http_bridge_session_compatible)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "_http_bridge_session_reusable_for_request", lambda **_: False)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    reused = await service._get_or_create_http_bridge_session(
        key,
        headers={"session_id": "sid-hard"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-hard",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert reused is replacement_session
    assert captured_kwargs["preferred_account_id"] == "acc-owner"
    assert captured_kwargs["require_preferred_account"] is True
    assert stale_session.closed is True
    assert any(call.args == (stale_session,) for call in close_session.await_args_list)


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_opens_parallel_bridge_for_busy_recreate_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-hard-busy", None)
    pending_state = proxy_service._WebSocketRequestState(
        request_id="req-busy",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    busy_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="sid-hard-busy",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([pending_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = busy_session
    created_sessions: list[proxy_service._HTTPBridgeSession] = []

    async def fake_create(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **_kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        created = proxy_service._HTTPBridgeSession(
            key=create_key,
            headers={"session_id": "sid-hard-busy"},
            affinity=proxy_service._AffinityPolicy(
                key="sid-hard-busy",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            request_model="gpt-5.5",
            account=cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE)),
            upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
            upstream_control=proxy_service._WebSocketUpstreamControl(),
            pending_requests=deque(),
            pending_lock=anyio.Lock(),
            response_create_gate=asyncio.Semaphore(1),
            queued_request_count=0,
            last_used_at=2.0,
            idle_ttl_seconds=120.0,
        )
        created_sessions.append(created)
        return created

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "_http_bridge_session_reusable_for_request", lambda **_: False)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    created = await service._get_or_create_http_bridge_session(
        key,
        headers={"session_id": "sid-hard-busy"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-hard-busy",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert created is created_sessions[0]
    assert created.key != key
    assert created.key.affinity_kind == key.affinity_kind
    assert created.key.strength == "hard"
    assert "#codex-lb-parallel=" in created.key.affinity_key
    assert service._http_bridge_sessions[key] is busy_session
    assert service._http_bridge_sessions[created.key] is created
    assert busy_session.closed is False
    assert list(busy_session.pending_requests) == [pending_state]
    close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_waiter_opens_parallel_bridge_for_busy_inflight_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-inflight-busy", None)
    pending_state = proxy_service._WebSocketRequestState(
        request_id="req-inflight-busy",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    busy_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="sid-inflight-busy",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([pending_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    inflight_future: asyncio.Future[proxy_service._HTTPBridgeSession] = asyncio.get_running_loop().create_future()
    inflight_future.set_result(busy_session)
    service._http_bridge_inflight_sessions[key] = inflight_future
    created_sessions: list[proxy_service._HTTPBridgeSession] = []

    async def fake_create(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **_kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        created = proxy_service._HTTPBridgeSession(
            key=create_key,
            headers={"session_id": "sid-inflight-busy"},
            affinity=proxy_service._AffinityPolicy(
                key="sid-inflight-busy",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            request_model="gpt-5.5",
            account=cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE)),
            upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
            upstream_control=proxy_service._WebSocketUpstreamControl(),
            pending_requests=deque(),
            pending_lock=anyio.Lock(),
            response_create_gate=asyncio.Semaphore(1),
            queued_request_count=0,
            last_used_at=2.0,
            idle_ttl_seconds=120.0,
        )
        created_sessions.append(created)
        return created

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "_http_bridge_session_reusable_for_request", lambda **_: False)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    created = await service._get_or_create_http_bridge_session(
        key,
        headers={"session_id": "sid-inflight-busy"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-inflight-busy",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert created is created_sessions[0]
    assert created.key != key
    assert created.key.affinity_kind == key.affinity_kind
    assert created.key.strength == "hard"
    assert "#codex-lb-parallel=" in created.key.affinity_key
    assert key not in service._http_bridge_sessions
    assert service._http_bridge_sessions[created.key] is created
    assert busy_session.closed is False
    assert list(busy_session.pending_requests) == [pending_state]
    close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_opens_parallel_bridge_for_busy_promoted_prompt_cache_continuity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-promoted-busy", None)
    busy_account = cast(Any, SimpleNamespace(id="acc-busy", status=AccountStatus.ACTIVE))
    preferred_account = cast(Any, SimpleNamespace(id="acc-preferred", status=AccountStatus.ACTIVE))
    busy_session = _make_http_bridge_session(key=key, account=busy_account, pending_count=1)
    busy_session.codex_session = True
    busy_session.previous_response_ids.add("resp-old")
    service._http_bridge_sessions[key] = busy_session
    created_sessions: list[proxy_service._HTTPBridgeSession] = []
    captured_kwargs: dict[str, object] = {}

    async def fake_create(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        captured_kwargs.update(kwargs)
        created = _make_http_bridge_session(key=create_key, account=preferred_account)
        created.codex_session = True
        created.previous_response_ids.add("resp-new")
        created_sessions.append(created)
        return created

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    created = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-promoted-busy",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp-new",
        preferred_account_id="acc-preferred",
        allow_previous_response_recovery_rebind=True,
    )

    assert created is created_sessions[0]
    assert created.key != key
    assert created.key.affinity_kind == key.affinity_kind
    assert created.key.strength == "soft"
    assert "#codex-lb-parallel=" in created.key.affinity_key
    assert service._http_bridge_sessions[key] is busy_session
    assert service._http_bridge_sessions[created.key] is created
    assert busy_session.closed is False
    assert close_session.await_count == 0
    assert captured_kwargs["preferred_account_id"] == "acc-preferred"
    assert captured_kwargs["require_preferred_account"] is True


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_opens_parallel_bridge_for_busy_prompt_cache_previous_response_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-previous-response-busy", None)
    busy_account = cast(Any, SimpleNamespace(id="acc-busy", status=AccountStatus.ACTIVE))
    preferred_account = cast(Any, SimpleNamespace(id="acc-preferred", status=AccountStatus.ACTIVE))
    busy_session = _make_http_bridge_session(key=key, account=busy_account, pending_count=1)
    busy_session.previous_response_ids.add("resp-old")
    service._http_bridge_sessions[key] = busy_session
    created_sessions: list[proxy_service._HTTPBridgeSession] = []
    captured_kwargs: dict[str, object] = {}

    async def fake_create(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        captured_kwargs.update(kwargs)
        created = _make_http_bridge_session(key=create_key, account=preferred_account)
        created.previous_response_ids.add("resp-new")
        created_sessions.append(created)
        return created

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    created = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-previous-response-busy",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp-new",
        preferred_account_id="acc-preferred",
        allow_previous_response_recovery_rebind=True,
    )

    assert busy_session.codex_session is False
    assert created is created_sessions[0]
    assert created.key != key
    assert created.key.affinity_kind == key.affinity_kind
    assert created.key.strength == "soft"
    assert "#codex-lb-parallel=" in created.key.affinity_key
    assert service._http_bridge_sessions[key] is busy_session
    assert service._http_bridge_sessions[created.key] is created
    assert busy_session.closed is False
    assert close_session.await_count == 0
    assert captured_kwargs["preferred_account_id"] == "acc-preferred"
    assert captured_kwargs["require_preferred_account"] is True


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_replaces_prompt_cache_session_promoted_to_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-key", "key-1")
    stale_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4-mini",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        codex_session=True,
        downstream_turn_state="http_turn_legacy",
        downstream_turn_state_aliases={"http_turn_legacy"},
        previous_response_ids=set(),
    )
    replacement_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = stale_session
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(
        service,
        "_create_http_bridge_session",
        AsyncMock(return_value=replacement_session),
    )
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    reused = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        api_key=_make_api_key(key_id="key-1", assigned_account_ids=["acc-1"], account_assignment_scope_enabled=True),
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert reused is replacement_session
    assert service._http_bridge_sessions[key] is replacement_session
    assert stale_session.closed is True
    assert any(call.args == (stale_session,) for call in close_session.await_args_list)


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_registers_turn_state_alias_without_rekeying_prompt_cache_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    prompt_cache_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-key", "key-1")
    session = proxy_service._HTTPBridgeSession(
        key=prompt_cache_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        codex_session=False,
        downstream_turn_state=None,
        downstream_turn_state_aliases=set(),
        previous_response_ids={"resp_prev_1"},
    )
    service._http_bridge_sessions[prompt_cache_key] = session
    service._http_bridge_previous_response_index[
        proxy_service._http_bridge_previous_response_alias_key("resp_prev_1", "key-1")
    ] = prompt_cache_key
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    scheduled_refreshes: list[proxy_service._HTTPBridgeSession] = []
    monkeypatch.setattr(service, "_schedule_durable_http_bridge_session_refresh", scheduled_refreshes.append)

    resolved = await service._get_or_create_http_bridge_session(
        prompt_cache_key,
        headers={"x-codex-turn-state": "http_turn_promoted"},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        api_key=_make_api_key(key_id="key-1", assigned_account_ids=["acc-1"], account_assignment_scope_enabled=True),
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
    )

    assert resolved is session
    assert session.key == prompt_cache_key
    assert service._http_bridge_sessions[prompt_cache_key] is session
    assert (
        service._http_bridge_previous_response_index[
            proxy_service._http_bridge_previous_response_alias_key("resp_prev_1", "key-1")
        ]
        == prompt_cache_key
    )
    assert (
        service._http_bridge_turn_state_index[
            proxy_service._http_bridge_turn_state_alias_key("http_turn_promoted", "key-1")
        ]
        == prompt_cache_key
    )
    assert scheduled_refreshes == [session]


@pytest.mark.asyncio
async def test_durable_http_bridge_refresh_is_throttled_and_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-heartbeat", None),
        account=SimpleNamespace(id="acc-heartbeat", status=AccountStatus.ACTIVE),
    )
    session.durable_session_id = "durable-heartbeat"
    session.durable_owner_epoch = 1
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    refresh_calls = 0

    async def slow_refresh(refresh_session: proxy_service._HTTPBridgeSession) -> None:
        nonlocal refresh_calls
        assert refresh_session is session
        refresh_calls += 1
        refresh_started.set()
        await allow_refresh.wait()

    monkeypatch.setattr(service, "_refresh_durable_http_bridge_session", slow_refresh)

    service._schedule_durable_http_bridge_session_refresh(session)
    first_task = session.durable_renew_task
    assert first_task is not None
    await asyncio.wait_for(refresh_started.wait(), timeout=1.0)

    service._schedule_durable_http_bridge_session_refresh(session)
    assert session.durable_renew_task is first_task
    assert refresh_calls == 1

    allow_refresh.set()
    await asyncio.wait_for(first_task, timeout=1.0)
    await asyncio.sleep(0)
    assert session.durable_renew_task is None

    service._schedule_durable_http_bridge_session_refresh(session)
    await asyncio.sleep(0)
    assert refresh_calls == 1

    session.durable_renew_after = 0.0
    service._schedule_durable_http_bridge_session_refresh(session)
    second_task = session.durable_renew_task
    assert second_task is not None
    await asyncio.wait_for(second_task, timeout=1.0)
    assert refresh_calls == 2


@pytest.mark.asyncio
async def test_durable_http_bridge_refresh_failure_can_retry_after_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-heartbeat-retry", None),
        account=SimpleNamespace(id="acc-heartbeat-retry", status=AccountStatus.ACTIVE),
    )
    session.durable_session_id = "durable-heartbeat-retry"
    session.durable_owner_epoch = 1
    refresh_calls = 0

    async def failing_refresh(refresh_session: proxy_service._HTTPBridgeSession) -> None:
        nonlocal refresh_calls
        assert refresh_session is session
        refresh_calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service, "_refresh_durable_http_bridge_session", failing_refresh)

    service._schedule_durable_http_bridge_session_refresh(session)
    first_task = session.durable_renew_task
    assert first_task is not None
    await asyncio.wait_for(first_task, timeout=1.0)
    await asyncio.sleep(0)
    assert refresh_calls == 1
    assert session.durable_renew_task is None

    service._schedule_durable_http_bridge_session_refresh(session)
    await asyncio.sleep(0)
    assert refresh_calls == 1

    session.durable_renew_after = 0.0
    service._schedule_durable_http_bridge_session_refresh(session)
    second_task = session.durable_renew_task
    assert second_task is not None
    await asyncio.wait_for(second_task, timeout=1.0)
    assert refresh_calls == 2


@pytest.mark.asyncio
async def test_close_http_bridge_session_settles_refresh_before_releasing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-heartbeat-close", None),
        account=SimpleNamespace(id="acc-heartbeat-close", status=AccountStatus.ACTIVE),
    )
    session.durable_session_id = "durable-heartbeat-close"
    session.durable_owner_epoch = 7
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    refresh_finished = asyncio.Event()

    async def slow_refresh(refresh_session: proxy_service._HTTPBridgeSession) -> None:
        assert refresh_session is session
        refresh_started.set()
        await allow_refresh.wait()
        refresh_finished.set()

    async def release_live_session(**_kwargs: object) -> None:
        assert refresh_finished.is_set()

    monkeypatch.setattr(service, "_refresh_durable_http_bridge_session", slow_refresh)
    monkeypatch.setattr(service._durable_bridge, "release_live_session", AsyncMock(side_effect=release_live_session))

    service._schedule_durable_http_bridge_session_refresh(session)
    await asyncio.wait_for(refresh_started.wait(), timeout=1.0)
    close_task = asyncio.create_task(service._close_http_bridge_session(session))
    await asyncio.sleep(0)
    assert close_task.done() is False

    allow_refresh.set()
    await asyncio.wait_for(close_task, timeout=1.0)
    service._durable_bridge.release_live_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_turn_state_request_ignores_prompt_cache_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-hard-turn-state",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)

    def fake_prepare(
        _prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_promoted", None),
        headers={"x-codex-turn-state": "http_turn_promoted"},
        affinity=proxy_service._AffinityPolicy(
            key="http_turn_promoted",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    captured_key: dict[str, object] = {}
    captured_lookup: dict[str, object] = {}

    async def fake_get_or_create_http_bridge_session(*args: object, **kwargs: object):
        captured_key["value"] = args[0]
        captured_lookup["value"] = kwargs.get("durable_lookup")
        return session

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_durable_account_binding",
        AsyncMock(
            return_value=proxy_service._DurableAccountBinding(
                account_id="acc-1",
                supports_request_model=True,
            )
        ),
    )
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="durable-prompt-cache",
                canonical_kind="prompt_cache",
                canonical_key="cache-derived",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-remote",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_promoted",
                latest_response_id=None,
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fake_get_or_create_http_bridge_session)
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "http_turn_promoted"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    key = cast(proxy_service._HTTPBridgeSessionKey, captured_key["value"])
    assert key.affinity_kind == "prompt_cache"
    assert key.affinity_key == "cache-derived"
    lookup = cast(proxy_service.DurableBridgeLookup, captured_lookup["value"])
    assert lookup.canonical_kind == "prompt_cache"
    assert lookup.canonical_key == "cache-derived"
    assert lookup.owner_instance_id == "instance-remote"
    assert lookup.lease_expires_at is not None


def test_http_bridge_session_key_infers_strength_from_affinity_kind() -> None:
    assert proxy_service._HTTPBridgeSessionKey("turn_state_header", "turn", None).strength == "hard"
    assert proxy_service._HTTPBridgeSessionKey("session_header", "session", None).strength == "hard"
    assert proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache", None).strength == "soft"
    assert proxy_service._HTTPBridgeSessionKey("request", "request", None).strength == "soft"


def test_http_bridge_owner_check_required_keeps_prompt_cache_soft() -> None:
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache", None)

    assert proxy_service._http_bridge_owner_check_required(key, gateway_safe_mode=False) is False
    assert proxy_service._http_bridge_owner_check_required(key, gateway_safe_mode=True) is False


def test_http_bridge_owner_check_required_enables_sticky_thread_in_gateway_safe_mode() -> None:
    key = proxy_service._HTTPBridgeSessionKey("sticky_thread", "thread-key", None)

    assert proxy_service._http_bridge_owner_check_required(key, gateway_safe_mode=False) is False
    assert proxy_service._http_bridge_owner_check_required(key, gateway_safe_mode=True) is True


@pytest.mark.asyncio
async def test_select_account_with_budget_prefers_durable_account_id_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    select_account = AsyncMock(
        return_value=proxy_service.AccountSelection(
            account=cast(Any, SimpleNamespace(id="acc-preferred")),
            error_message=None,
            error_code=None,
        )
    )
    service._load_balancer = cast(Any, SimpleNamespace(select_account=select_account))
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(sticky_reallocation_budget_threshold_pct=95.0))
        ),
    )

    selection = await service._select_account_with_budget(
        time.monotonic() + 60.0,
        request_id="req-1",
        kind="http_bridge",
        request_stage="reattach",
        preferred_account_id="acc-preferred",
    )

    assert selection.account is not None
    assert selection.account.id == "acc-preferred"
    assert select_account.await_count == 1
    first_call = select_account.await_args_list[0]
    assert first_call.kwargs["account_ids"] == {"acc-preferred"}


@pytest.mark.asyncio
async def test_select_account_with_budget_skips_preferred_account_outside_assignment_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    select_account = AsyncMock(
        return_value=proxy_service.AccountSelection(
            account=cast(Any, SimpleNamespace(id="acc-allowed")),
            error_message=None,
            error_code=None,
        )
    )
    service._load_balancer = cast(Any, SimpleNamespace(select_account=select_account))
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(sticky_reallocation_budget_threshold_pct=95.0))
        ),
    )

    selection = await service._select_account_with_budget(
        time.monotonic() + 60.0,
        request_id="req-2",
        kind="http_bridge",
        request_stage="reattach",
        api_key=_make_api_key(key_id="key-1", assigned_account_ids=["acc-allowed"]),
        preferred_account_id="acc-preferred",
    )

    assert selection.account is not None
    assert selection.account.id == "acc-allowed"
    assert select_account.await_count == 1
    first_call = select_account.await_args_list[0]
    assert first_call.kwargs["account_ids"] == {"acc-allowed"}


@pytest.mark.asyncio
async def test_select_account_with_budget_allows_http_bridge_account_at_session_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-full", status=AccountStatus.ACTIVE))
    lease = service._http_bridge_account_model_sessions.try_acquire(
        account_id=account.id,
        model="gpt-5.4",
        limit=1,
    )
    assert lease is not None
    select_account = AsyncMock(
        return_value=proxy_service.AccountSelection(
            account=account,
            error_message=None,
            error_code=None,
        )
    )
    service._load_balancer = cast(Any, SimpleNamespace(select_account=select_account))
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_http_bridge_account_model_session_limit=1,
            proxy_account_model_concurrency_limit=0,
        ),
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(sticky_reallocation_budget_threshold_pct=95.0))
        ),
    )

    selection = await service._select_account_with_budget(
        time.monotonic() + 60.0,
        request_id="req-session-limit",
        kind="http_bridge",
        model="gpt-5.4",
    )

    assert selection.account is account
    assert select_account.await_count == 1
    assert select_account.await_args.kwargs["exclude_account_ids"] == set()
    lease.release()


def test_headers_with_authorization_restores_missing_proxy_api_header() -> None:
    headers = proxy_service._headers_with_authorization({"x-request-id": "req-1"}, "Bearer proxy-key")

    assert headers["Authorization"] == "Bearer proxy-key"
    assert headers["x-request-id"] == "req-1"


def test_headers_with_authorization_does_not_override_existing_value() -> None:
    headers = proxy_service._headers_with_authorization({"authorization": "Bearer existing"}, "Bearer proxy-key")

    assert headers["authorization"] == "Bearer existing"


def test_make_http_bridge_session_key_prefers_signed_forwarded_affinity_over_generated_turn_state() -> None:
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})

    key = proxy_service._make_http_bridge_session_key(
        payload,
        headers={
            "x-codex-turn-state": "http_turn_generated",
            "x-codex-bridge-affinity-kind": "session_header",
            "x-codex-bridge-affinity-key": "sid-123",
        },
        affinity=proxy_service._AffinityPolicy(key="sid-123"),
        api_key=None,
        request_id="req-1",
        allow_forwarded_affinity_headers=True,
    )

    assert key.affinity_kind == "session_header"
    assert key.affinity_key == "sid-123"
    assert key.strength == "hard"


def test_make_http_bridge_session_key_ignores_forwarded_affinity_headers_on_public_requests() -> None:
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})

    key = proxy_service._make_http_bridge_session_key(
        payload,
        headers={
            "x-codex-bridge-affinity-kind": "session_header",
            "x-codex-bridge-affinity-key": "sid-123",
        },
        affinity=proxy_service._AffinityPolicy(key="cache-123", kind=proxy_service.StickySessionKind.PROMPT_CACHE),
        api_key=None,
        request_id="req-1",
        allow_forwarded_affinity_headers=False,
    )

    assert key.affinity_kind == "prompt_cache"
    assert key.affinity_key == "cache-123"
    assert key.strength == "soft"


def test_http_bridge_requires_cluster_registration_for_non_loopback_advertise_url() -> None:
    settings = Settings(
        http_responses_session_bridge_instance_id="instance-a",
        http_responses_session_bridge_advertise_base_url="http://instance-a.codex-lb-bridge.default.svc.cluster.local:2455",
    )

    assert proxy_service._http_bridge_requires_cluster_registration(settings) is True


def test_http_bridge_requires_cluster_registration_skips_loopback_single_replica() -> None:
    settings = Settings(http_responses_session_bridge_advertise_base_url="http://127.0.0.1:2455")

    assert proxy_service._http_bridge_requires_cluster_registration(settings) is False


def test_durable_bridge_lookup_active_owner_accepts_naive_datetime() -> None:
    lookup = proxy_service.DurableBridgeLookup(
        session_id="sess-1",
        canonical_kind="session_header",
        canonical_key="sid-123",
        api_key_scope="__anonymous__",
        account_id="acc-1",
        owner_instance_id="instance-a",
        owner_epoch=1,
        lease_expires_at=datetime(2099, 1, 1, 0, 0, 0),
        state=HttpBridgeSessionState.ACTIVE,
        latest_turn_state=None,
        latest_response_id=None,
    )

    assert proxy_service._durable_bridge_lookup_active_owner(lookup) == "instance-a"


@pytest.mark.asyncio
async def test_stream_via_http_bridge_injects_durable_previous_response_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-1",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-1",
                canonical_kind="session_header",
                canonical_key="sid-123",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_1",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-123"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] == "resp_latest"


@pytest.mark.asyncio
async def test_stream_via_http_bridge_ignores_durable_anchor_for_unsupported_account_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.5", "instructions": "hi", "input": "hello"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-unsupported-durable-account",
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-legacy", None),
        headers={"x-codex-session-id": "sid-legacy"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-legacy",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.5",
        account=cast(Any, SimpleNamespace(id="provider-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="legacy-free-session",
                canonical_kind="session_header",
                canonical_key="sid-legacy",
                api_key_scope="__anonymous__",
                account_id="free-legacy",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_legacy",
                latest_response_id="resp_legacy",
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_durable_account_binding",
        AsyncMock(
            return_value=proxy_service._DurableAccountBinding(
                account_id="free-legacy",
                supports_request_model=False,
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)

    async def fake_get_or_create(
        *args: object,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        captured["preferred_account_id"] = kwargs.get("preferred_account_id")
        captured["durable_account_supports_request_model"] = kwargs.get("durable_account_supports_request_model")
        return session

    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fake_get_or_create)
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-legacy"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None
    assert captured["preferred_account_id"] is None
    assert captured["durable_account_supports_request_model"] is False


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_session_anchor_for_soft_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-soft",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    prepared_previous_response_ids: list[str | None] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        prepared_previous_response_ids.append(prepared_payload.previous_response_id)
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-123", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-123",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        last_completed_response_id="resp_soft_latest",
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_enabled=True,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert prepared_previous_response_ids == [None]


@pytest.mark.asyncio
async def test_stream_via_http_bridge_skips_session_anchor_injection_when_trim_would_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard session-level previous_response_id injection.

    The session anchor must only be injected when the trim branch would
    actually strip the already-stored prefix. If the incoming payload is
    a full resend whose prefix cannot be trimmed (non-list input, shorter
    history, or a prefix fingerprint mismatch), injecting an anchor would
    send both the full history and a previous_response_id upstream, which
    duplicates context and distorts output/cost.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    # Non-list input: trim cannot possibly apply, so no anchor should be
    # injected even though the session has a completed response.
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "fresh turn text"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-session-anchor-guard",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    prepared_previous_response_ids: list[str | None] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        prepared_previous_response_ids.append(prepared_payload.previous_response_id)
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-anchor-guard", None),
        headers={"x-codex-session-id": "sid-anchor-guard"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-anchor-guard",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        codex_session=True,
        last_completed_response_id="resp_session_latest",
        last_completed_input_count=3,
        last_completed_input_prefix_fingerprint=proxy_service._fingerprint_input_items(
            [
                {"role": "user", "content": [{"type": "input_text", "text": "a"}]},
                {"role": "assistant", "content": [{"type": "output_text", "text": "b"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "c"}]},
            ]
        ),
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_enabled=True,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-anchor-guard"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    # No anchor should have been injected because the non-list input
    # would have left the trim branch inert, which would have duplicated
    # context upstream.
    assert prepared_previous_response_ids == [None]


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_durable_previous_response_anchor_for_full_resend_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
                {"role": "user", "content": "follow up"},
            ],
        },
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-full-resend",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}
    prepared_input_lengths: list[int] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        inp = prepared_payload.input
        prepared_input_lengths.append(len(inp) if isinstance(inp, list) else 1)
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-1",
                canonical_kind="session_header",
                canonical_key="sid-123",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_1",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-123"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None
    # Full-resend payloads are explicitly excluded from durable anchor
    # injection, so the bridge prepares the original request exactly once.
    assert prepared_input_lengths == [3]
    # This path never reaches the trim branch, so the fake request_state
    # returned by fake_prepare keeps its default metadata.
    assert request_state.input_full_fingerprint is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_injects_durable_anchor_for_trimmable_full_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    input_items = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
        {"role": "user", "content": "follow up"},
    ]
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": input_items,
        },
    )
    normalized_input = payload.input
    assert isinstance(normalized_input, list)
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-full-resend-trim",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    prepared_previous_response_ids: list[str | None] = []
    prepared_input_lengths: list[int] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        prepared_previous_response_ids.append(prepared_payload.previous_response_id)
        inp = prepared_payload.input
        prepared_input_lengths.append(len(inp) if isinstance(inp, list) else 1)
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-1",
                canonical_kind="session_header",
                canonical_key="sid-123",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id=None,
                owner_epoch=1,
                lease_expires_at=None,
                state=HttpBridgeSessionState.DRAINING,
                latest_turn_state="http_turn_1",
                latest_response_id="resp_latest",
                latest_input_item_count=2,
                latest_input_full_fingerprint=proxy_service._fingerprint_input_items(normalized_input[:2]),
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-123"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert prepared_previous_response_ids == [None, "resp_latest", "resp_latest"]
    assert prepared_input_lengths == [3, 3, 1]
    assert request_state.proxy_injected_previous_response_id is True
    assert request_state.fresh_upstream_request_is_retry_safe is True


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_durable_previous_response_anchor_for_explicit_prompt_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "prompt_cache_key": "thread-123",
        },
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-prompt-cache-anchor",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "thread-123", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="thread-123",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-1",
                canonical_kind="prompt_cache",
                canonical_key="thread-123",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_1",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_prefer_durable_account_for_soft_prompt_cache_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "prompt_cache_key": "thread-soft",
        },
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-soft-prompt-cache",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "thread-soft", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="thread-soft",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-soft-prompt-cache",
                canonical_kind="prompt_cache",
                canonical_key="thread-soft",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_soft",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)

    async def fake_get_or_create(
        *args: object,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        captured["preferred_account_id"] = kwargs.get("preferred_account_id")
        return session

    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fake_get_or_create)
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None
    assert captured["preferred_account_id"] is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_prefers_durable_account_for_soft_prompt_cache_follow_up_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello again",
            "prompt_cache_key": "thread-soft-follow-up",
        },
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-soft-prompt-cache-follow-up",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "thread-soft-follow-up", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="thread-soft-follow-up",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-soft-follow-up",
                canonical_kind="prompt_cache",
                canonical_key="thread-soft-follow-up",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_soft_follow_up",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)

    async def fake_get_or_create(
        *args: object,
        **kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        captured["preferred_account_id"] = kwargs.get("preferred_account_id")
        captured["request_stage"] = kwargs.get("request_stage")
        return session

    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fake_get_or_create)
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "http_turn_soft_follow_up"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None
    assert captured["request_stage"] == "follow_up"
    assert captured["preferred_account_id"] == "acc-1"


@pytest.mark.asyncio
async def test_close_http_bridge_session_fails_pending_downstream_requests() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-bridge-close",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=event_queue,
        transport="http",
    )
    pending_requests = deque([request_state])
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "close-thread", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="close-thread",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-close", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=pending_requests,
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )

    await service._close_http_bridge_session(session)

    failed_event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
    assert failed_event is not None
    assert '"code":"stream_incomplete"' in failed_event
    assert "HTTP bridge session closed before response.completed" in failed_event
    assert await asyncio.wait_for(event_queue.get(), timeout=1.0) is None
    assert list(session.pending_requests) == []
    assert session.queued_request_count == 0


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_durable_anchor_for_live_turn_state_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-live-turn-state",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session_key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_live", None)
    session = proxy_service._HTTPBridgeSession(
        key=session_key,
        headers={"x-codex-turn-state": "http_turn_live"},
        affinity=proxy_service._AffinityPolicy(
            key="http_turn_live",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[session_key] = session
    service._http_bridge_turn_state_index[proxy_service._http_bridge_turn_state_alias_key("http_turn_live", None)] = (
        session_key
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-live-turn-state",
                canonical_kind="turn_state_header",
                canonical_key="http_turn_live",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_live",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "http_turn_live"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_durable_anchor_for_live_prompt_cache_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "prompt_cache_key": "thread-live",
        },
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-live-prompt-cache",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "thread-live", None)
    session = proxy_service._HTTPBridgeSession(
        key=session_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="thread-live",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[session_key] = session

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-live-prompt-cache",
                canonical_kind="prompt_cache",
                canonical_key="thread-live",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state=None,
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_durable_anchor_when_forwarding_to_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-forward-owner",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_forward", None),
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            http_responses_session_bridge_enabled=True,
            http_responses_session_bridge_instance_id="instance-a",
        ),
    )
    service._ring_membership = cast(
        Any,
        SimpleNamespace(resolve_endpoint=AsyncMock(return_value="http://instance-b")),
    )
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-forward-owner",
                canonical_kind="turn_state_header",
                canonical_key="http_turn_forward",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-b",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_forward",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=owner_forward))

    async def fake_forward_http_bridge_request_to_owner(**kwargs: object):
        del kwargs
        if False:
            yield ""
        return

    monkeypatch.setattr(service, "_forward_http_bridge_request_to_owner", fake_forward_http_bridge_request_to_owner)

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "http_turn_forward"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_inject_durable_previous_response_anchor_for_derived_prompt_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"},
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-derived-prompt-cache-anchor",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured: dict[str, object] = {}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        captured["previous_response_id"] = prepared_payload.previous_response_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "derived-thread-123", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="derived-thread-123",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                        openai_prompt_cache_key_derivation_enabled=True,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service._durable_bridge,
        "lookup_request_targets",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="sess-1",
                canonical_kind="prompt_cache",
                canonical_key="derived-thread-123",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-a",
                owner_epoch=1,
                lease_expires_at=datetime.now(timezone.utc),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state="http_turn_1",
                latest_response_id="resp_latest",
            )
        ),
    )
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    assert captured["previous_response_id"] is None


@pytest.mark.asyncio
async def test_stream_via_http_bridge_resolves_previous_response_owner_from_request_logs_when_durable_lookup_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "previous_response_id": "resp_prev_owner_lookup",
        }
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-owner-lookup",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_owner_lookup",
        session_id="turn_http_owner",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await event_queue.put(None)
    captured_preferred: dict[str, object] = {}

    def fake_prepare(
        _prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        return request_state, '{"type":"response.create"}'

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    owner_lookup = AsyncMock(return_value="acc-owner-from-logs")
    monkeypatch.setattr(service, "_resolve_websocket_previous_response_owner", owner_lookup)
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)

    async def fake_get_or_create_http_bridge_session(*args: object, **kwargs: object):
        captured_preferred["value"] = kwargs.get("preferred_account_id")
        return session

    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fake_get_or_create_http_bridge_session)
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "turn_http_owner"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == []
    owner_lookup.assert_awaited_once_with(
        previous_response_id="resp_prev_owner_lookup",
        api_key=None,
        session_id="turn_http_owner",
        surface="http_bridge",
    )
    assert captured_preferred["value"] == "acc-owner-from-logs"


@pytest.mark.asyncio
async def test_stream_via_http_bridge_uses_generated_downstream_turn_state_for_owner_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "previous_response_id": "resp_prev_owner_lookup",
        }
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-generated-turn-state",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_owner_lookup",
        session_id="sid-shared",
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-shared", None),
        headers={"x-codex-session-id": "sid-shared"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-shared",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )

    prepared_input_lengths: list[int] = []

    def fake_prepare(
        _prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation, request_id
        inp = _prepared_payload.input
        prepared_input_lengths.append(len(inp) if isinstance(inp, list) else 1)
        return request_state, '{"type":"response.create"}'

    async def fake_stream_http_bridge_session_events(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ):
        del request_state, text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        yield 'data: {"type":"response.completed"}\n\n'

    owner_lookup = AsyncMock(return_value="acc-owner-from-turn-state")

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_resolve_websocket_previous_response_owner", owner_lookup)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=session))
    monkeypatch.setattr(service, "_stream_http_bridge_session_events", fake_stream_http_bridge_session_events)

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-shared"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=1800.0,
            max_sessions=8,
            queue_limit=4,
            downstream_turn_state="http_turn_generated",
        )
    ]

    assert chunks == ['data: {"type":"response.completed"}\n\n']
    owner_lookup.assert_awaited_once_with(
        previous_response_id="resp_prev_owner_lookup",
        api_key=None,
        session_id="http_turn_generated",
        surface="http_bridge",
    )
    assert request_state.session_id == "http_turn_generated"
    assert request_state.preferred_account_id == "acc-owner-from-turn-state"
    # No durable anchor is injected in this path; the request is prepared
    # once with the original single-item input while owner lookup uses the
    # generated downstream turn state for scoping.
    assert prepared_input_lengths == [1]


@pytest.mark.asyncio
async def test_http_bridge_waits_for_registration_for_hard_keys_before_startup_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.startup as startup_module

    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = Settings(
        http_responses_session_bridge_instance_id="instance-a",
        http_responses_session_bridge_advertise_base_url="http://instance-a.bridge.default.svc.cluster.local:2455",
    )
    monkeypatch.setattr(startup_module, "_startup_complete", False)
    monkeypatch.setattr(startup_module, "_bridge_registration_complete", False)

    assert (
        await proxy_service._http_bridge_should_wait_for_registration(
            service,
            proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
            settings,
        )
        is True
    )


@pytest.mark.asyncio
async def test_forward_http_bridge_request_to_owner_preserves_session_header_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
    )
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    captured: dict[str, object] = {}

    async def fake_stream_responses(**kwargs: object):
        captured.update(kwargs)
        if False:
            yield ""
        return

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_http_bridge_owner_client",
        cast(Any, SimpleNamespace(stream_responses=fake_stream_responses)),
    )

    chunks = [
        chunk
        async for chunk in service._forward_http_bridge_request_to_owner(
            owner_forward=owner_forward,
            payload=payload,
            headers={"x-codex-session-id": "sid-123"},
            api_key_reservation=None,
            codex_session_affinity=True,
            downstream_turn_state="http_turn_generated",
            request_started_at=10.0,
            proxy_api_authorization=None,
        )
    ]

    assert chunks == []
    context = cast(proxy_service.HTTPBridgeForwardContext, captured["context"])
    assert context.downstream_turn_state == "http_turn_generated"
    assert context.original_affinity_kind == "session_header"
    assert context.original_affinity_key == "sid-123"
    assert cast(dict[str, str], captured["headers"])["x-codex-session-id"] == "sid-123"


@pytest.mark.asyncio
async def test_forward_http_bridge_request_to_owner_raises_proxy_error_on_relay_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
    )
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})

    async def fake_stream_responses(**kwargs: object):
        del kwargs
        raise OwnerForwardRelayFailure("data: ignored\n\n")
        yield ""

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_http_bridge_owner_client",
        cast(Any, SimpleNamespace(stream_responses=fake_stream_responses)),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        async for _ in service._forward_http_bridge_request_to_owner(
            owner_forward=owner_forward,
            payload=payload,
            headers={"x-codex-session-id": "sid-123"},
            api_key_reservation=None,
            codex_session_affinity=True,
            downstream_turn_state="http_turn_generated",
            request_started_at=10.0,
            proxy_api_authorization=None,
        ):
            pass

    assert exc_info.value.status_code == 503
    assert exc_info.value.payload["error"]["code"] == "bridge_owner_unreachable"


@pytest.mark.asyncio
async def test_forward_http_bridge_request_to_owner_emits_terminal_sse_after_forwarded_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
    )
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})

    async def fake_stream_responses(**kwargs: object):
        del kwargs
        yield "data: first\n\n"
        raise OwnerForwardRelayFailure("data: terminal\n\n")

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_http_bridge_owner_client",
        cast(Any, SimpleNamespace(stream_responses=fake_stream_responses)),
    )

    chunks = [
        chunk
        async for chunk in service._forward_http_bridge_request_to_owner(
            owner_forward=owner_forward,
            payload=payload,
            headers={"x-codex-session-id": "sid-123"},
            api_key_reservation=None,
            codex_session_affinity=True,
            downstream_turn_state="http_turn_generated",
            request_started_at=10.0,
            proxy_api_authorization=None,
        )
    ]

    assert chunks == ["data: first\n\n", "data: terminal\n\n"]


@pytest.mark.asyncio
async def test_stream_via_http_bridge_does_not_rebind_after_forwarded_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
    )
    forward_calls = {"count": 0}

    async def fake_forward(**kwargs: object):
        del kwargs
        forward_calls["count"] += 1
        yield "data: first\n\n"
        raise ProxyResponseError(503, proxy_service.openai_error("bridge_owner_unreachable", "boom"))

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=owner_forward))
    monkeypatch.setattr(service, "_forward_http_bridge_request_to_owner", fake_forward)

    seen: list[str] = []
    with pytest.raises(ProxyResponseError):
        async for chunk in service._stream_via_http_bridge(
            payload,
            {"x-codex-session-id": "sid-123"},
            codex_session_affinity=True,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            propagate_http_errors=False,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        ):
            seen.append(chunk)

    assert seen == ["data: first\n\n"]
    assert forward_calls["count"] == 1
    service._get_or_create_http_bridge_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_fails_closed_on_forward_loop_prevented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
    )

    async def fake_forward(**kwargs: object):
        del kwargs
        raise ProxyResponseError(503, proxy_service.openai_error("bridge_forward_loop_prevented", "loop"))
        yield ""

    get_or_create = AsyncMock(return_value=owner_forward)
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_forward_http_bridge_request_to_owner", fake_forward)

    with pytest.raises(ProxyResponseError) as exc_info:
        async for _ in service._stream_via_http_bridge(
            payload,
            {"x-codex-session-id": "sid-123"},
            codex_session_affinity=True,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            propagate_http_errors=False,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        ):
            pass

    assert exc_info.value.payload["error"]["code"] == "bridge_forward_loop_prevented"
    get_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_reacquires_api_key_reservation_for_local_previous_response_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    api_key = _make_api_key(key_id="key-1", assigned_account_ids=[])
    initial_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="resv-initial",
        key_id=api_key.id,
        model="gpt-5.4",
    )
    retried_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="resv-retry",
        key_id=api_key.id,
        model="gpt-5.4",
    )
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "prompt_cache_key": "bridge-prev-rebind",
            "previous_response_id": "resp_prev_1",
        }
    )

    request_state_initial = proxy_service._WebSocketRequestState(
        request_id="req-initial",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=initial_reservation,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_1",
    )
    request_state_initial.request_stage = "follow_up"
    request_state_initial.preferred_account_id = "acc-1"
    request_state_retry = proxy_service._WebSocketRequestState(
        request_id="req-retry",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=retried_reservation,
        started_at=2.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_1",
    )

    prepare_reservations: list[proxy_service.ApiKeyUsageReservationData | None] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del prepared_payload, api_key, request_id
        prepare_reservations.append(api_key_reservation)
        if len(prepare_reservations) == 1:
            return request_state_initial, '{"type":"response.create","request":"initial"}'
        return request_state_retry, '{"type":"response.create","request":"retry"}'

    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-prev-rebind", api_key.id)
    session_initial = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="bridge-prev-rebind", kind=proxy_service.StickySessionKind.PROMPT_CACHE
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    session_retry = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="bridge-prev-rebind", kind=proxy_service.StickySessionKind.PROMPT_CACHE
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session_initial

    stream_calls = {"count": 0}

    async def fake_stream_http_bridge_session_events(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ):
        del request_state, text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        stream_calls["count"] += 1
        if stream_calls["count"] == 1:
            raise ProxyResponseError(400, proxy_service.openai_error("previous_response_not_found", "missing"))
        yield 'data: {"type":"response.completed"}\n\n'

    reserve_retry = AsyncMock(return_value=retried_reservation)
    get_or_create = AsyncMock(side_effect=[session_initial, session_retry])

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_stream_http_bridge_session_events", fake_stream_http_bridge_session_events)
    monkeypatch.setattr(service, "_close_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", reserve_retry)

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=api_key,
            api_key_reservation=initial_reservation,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == ['data: {"type":"response.completed"}\n\n']
    assert prepare_reservations == [initial_reservation, retried_reservation]
    reserve_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_bridge_local_owner_account_id_records_resolution_source(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))

    class _ObservedCounter:
        def __init__(self) -> None:
            self.samples: list[dict[str, object]] = []

        def labels(self, **labels: str):
            sample: dict[str, object] = {"labels": dict(labels), "value": 0.0}
            self.samples.append(sample)

            def inc(amount: float = 1.0) -> None:
                sample["value"] = cast(float, sample["value"]) + amount

            return SimpleNamespace(inc=inc)

    counter = _ObservedCounter()
    monkeypatch.setattr(proxy_service, "PROMETHEUS_AVAILABLE", True)
    monkeypatch.setattr(proxy_service, "continuity_owner_resolution_total", counter, raising=False)
    caplog.set_level(logging.INFO, logger="app.modules.proxy.service")

    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-prev-rebind", None)
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="bridge-prev-rebind", kind=proxy_service.StickySessionKind.PROMPT_CACHE
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session

    owner = await service._http_bridge_local_owner_account_id(
        key=key,
        incoming_turn_state=None,
        previous_response_id="resp_prev_local_owner_metric",
        api_key=None,
        request_model="gpt-5.4",
    )

    assert owner == "acc-1"
    assert "continuity_owner_resolution surface=http_bridge source=local_bridge_session outcome=hit" in caplog.text
    assert "resp_prev_local_owner_metric" not in caplog.text
    assert counter.samples == [
        {
            "labels": {"surface": "http_bridge", "source": "local_bridge_session", "outcome": "hit"},
            "value": 1.0,
        }
    ]


@pytest.mark.asyncio
async def test_stream_via_http_bridge_reacquires_api_key_reservation_after_owner_forward_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    api_key = _make_api_key(key_id="key-1", assigned_account_ids=[])
    initial_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="resv-initial",
        key_id=api_key.id,
        model="gpt-5.4",
    )
    retried_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="resv-retry",
        key_id=api_key.id,
        model="gpt-5.4",
    )
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "previous_response_id": "resp_prev_1",
        }
    )

    request_state_initial = proxy_service._WebSocketRequestState(
        request_id="req-initial",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=initial_reservation,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_1",
    )
    request_state_initial.request_stage = "follow_up"
    request_state_initial.preferred_account_id = "acc-1"
    request_state_retry = proxy_service._WebSocketRequestState(
        request_id="req-retry",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=retried_reservation,
        started_at=2.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_1",
    )

    prepare_reservations: list[proxy_service.ApiKeyUsageReservationData | None] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del prepared_payload, api_key, request_id
        prepare_reservations.append(api_key_reservation)
        if len(prepare_reservations) == 1:
            return request_state_initial, '{"type":"response.create","request":"initial"}'
        return request_state_retry, '{"type":"response.create","request":"retry"}'

    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", api_key.id),
    )
    session_retry = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", api_key.id),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )

    submitted_reservations: list[proxy_service.ApiKeyUsageReservationData | None] = []

    async def fake_forward_http_bridge_request_to_owner(**kwargs: object):
        del kwargs
        raise ProxyResponseError(400, proxy_service.openai_error("previous_response_not_found", "missing"))
        yield ""

    async def fake_submit_http_bridge_request(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        release_submit_lease: bool = True,
    ) -> None:
        del _session, text_data, queue_limit, release_submit_lease
        submitted_reservations.append(request_state.api_key_reservation)
        event_queue = request_state.event_queue
        assert event_queue is not None
        await event_queue.put('data: {"type":"response.completed"}\n\n')
        await event_queue.put(None)

    reserve_retry = AsyncMock(return_value=retried_reservation)
    get_or_create = AsyncMock(side_effect=[owner_forward, session_retry])

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_resolve_websocket_previous_response_owner", AsyncMock(return_value="acc-1"))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_forward_http_bridge_request_to_owner", fake_forward_http_bridge_request_to_owner)
    monkeypatch.setattr(service, "_submit_http_bridge_request", fake_submit_http_bridge_request)
    monkeypatch.setattr(service, "_detach_http_bridge_request", AsyncMock())
    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", reserve_retry)

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-session-id": "sid-123"},
            codex_session_affinity=True,
            propagate_http_errors=False,
            openai_cache_affinity=False,
            api_key=api_key,
            api_key_reservation=initial_reservation,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    assert chunks == ['data: {"type":"response.completed"}\n\n']
    assert prepare_reservations == [initial_reservation, retried_reservation]
    assert submitted_reservations == [retried_reservation]
    reserve_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_local_previous_response_rebind_fails_existing_pending_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "prompt_cache_key": "bridge-prev-rebind",
            "previous_response_id": "resp_prev_1",
        }
    )

    request_state_initial = proxy_service._WebSocketRequestState(
        request_id="req-initial",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_1",
    )
    request_state_initial.request_stage = "follow_up"
    request_state_initial.preferred_account_id = "acc-1"
    request_state_retry = proxy_service._WebSocketRequestState(
        request_id="req-retry",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=2.0,
        event_queue=asyncio.Queue(),
        transport="http",
        previous_response_id="resp_prev_1",
    )

    stale_pending_queue: asyncio.Queue[str | None] = asyncio.Queue()
    stale_pending_request = proxy_service._WebSocketRequestState(
        request_id="req-stale",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=0.5,
        event_queue=stale_pending_queue,
        transport="http",
    )
    stale_pending_request.skip_request_log = True

    prepare_calls = {"count": 0}

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del prepared_payload, api_key, api_key_reservation, request_id
        prepare_calls["count"] += 1
        if prepare_calls["count"] == 1:
            return request_state_initial, '{"type":"response.create","request":"initial"}'
        return request_state_retry, '{"type":"response.create","request":"retry"}'

    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-prev-rebind", None)
    session_initial = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="bridge-prev-rebind", kind=proxy_service.StickySessionKind.PROMPT_CACHE
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([stale_pending_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    session_retry = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="bridge-prev-rebind", kind=proxy_service.StickySessionKind.PROMPT_CACHE
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session_initial

    stream_calls = {"count": 0}

    async def fake_stream_http_bridge_session_events(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ):
        del request_state, text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        stream_calls["count"] += 1
        if stream_calls["count"] == 1:
            raise ProxyResponseError(400, proxy_service.openai_error("previous_response_not_found", "missing"))
        yield 'data: {"type":"response.completed"}\n\n'

    get_or_create = AsyncMock(side_effect=[session_initial, session_retry])

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_stream_http_bridge_session_events", fake_stream_http_bridge_session_events)
    monkeypatch.setattr(service, "_close_http_bridge_session", AsyncMock())

    chunks = [
        chunk
        async for chunk in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=False,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        )
    ]

    failed_block = await asyncio.wait_for(stale_pending_queue.get(), timeout=0.2)
    done_marker = await asyncio.wait_for(stale_pending_queue.get(), timeout=0.2)

    assert chunks == ['data: {"type":"response.completed"}\n\n']
    assert isinstance(failed_block, str)
    assert '"type":"response.failed"' in failed_block
    assert '"code":"stream_incomplete"' in failed_block
    assert done_marker is None
    assert not session_initial.pending_requests
    assert session_initial.queued_request_count == 0


@pytest.mark.asyncio
async def test_stream_via_http_bridge_rolls_over_session_after_context_length_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "prompt_cache_key": "bridge-context-overflow",
        }
    )

    request_state = proxy_service._WebSocketRequestState(
        request_id="req-context-overflow",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    stale_pending_queue: asyncio.Queue[str | None] = asyncio.Queue()
    stale_pending_request = proxy_service._WebSocketRequestState(
        request_id="req-stale",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=0.5,
        event_queue=stale_pending_queue,
        transport="http",
    )
    stale_pending_request.skip_request_log = True

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del prepared_payload, api_key, api_key_reservation, request_id
        return request_state, '{"type":"response.create","request":"initial"}'

    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-context-overflow", None)
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="bridge-context-overflow", kind=proxy_service.StickySessionKind.PROMPT_CACHE
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([stale_pending_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session

    async def fake_stream_http_bridge_session_events(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ):
        del request_state, text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        raise ProxyResponseError(
            400,
            proxy_service.openai_error(
                "context_length_exceeded",
                "Your input exceeds the context window of this model.",
                error_type="invalid_request_error",
            ),
        )
        yield

    close_session = AsyncMock()
    get_or_create = AsyncMock(return_value=session)

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_stream_http_bridge_session_events", fake_stream_http_bridge_session_events)
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    with pytest.raises(ProxyResponseError) as exc_info:
        async for _ in service._stream_via_http_bridge(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=True,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        ):
            pass

    failed_block = await asyncio.wait_for(stale_pending_queue.get(), timeout=0.2)
    done_marker = await asyncio.wait_for(stale_pending_queue.get(), timeout=0.2)

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["error"]["code"] == "context_length_exceeded"
    assert key not in service._http_bridge_sessions
    close_session.assert_awaited_once_with(session)
    assert isinstance(failed_block, str)
    assert '"type":"response.failed"' in failed_block
    assert '"code":"stream_incomplete"' in failed_block
    assert done_marker is None
    assert not session.pending_requests
    assert session.queued_request_count == 0


@pytest.mark.asyncio
async def test_stream_via_http_bridge_context_overflow_keeps_hard_affinity_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
        }
    )

    request_state = proxy_service._WebSocketRequestState(
        request_id="req-context-overflow-hard",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del prepared_payload, api_key, api_key_reservation, request_id
        return request_state, '{"type":"response.create","request":"initial"}'

    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "turn_hard_overflow", None)
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-turn-state": "turn_hard_overflow"},
        affinity=proxy_service._AffinityPolicy(
            key="turn_hard_overflow", kind=proxy_service.StickySessionKind.CODEX_SESSION
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session

    async def fake_stream_http_bridge_session_events(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ):
        del request_state, text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        raise ProxyResponseError(
            400,
            proxy_service.openai_error(
                "context_length_exceeded",
                "Your input exceeds the context window of this model.",
                error_type="invalid_request_error",
            ),
        )
        yield

    close_session = AsyncMock()
    get_or_create = AsyncMock(return_value=session)

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_stream_http_bridge_session_events", fake_stream_http_bridge_session_events)
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    with pytest.raises(ProxyResponseError) as exc_info:
        async for _ in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "turn_hard_overflow"},
            codex_session_affinity=True,
            propagate_http_errors=True,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
        ):
            pass

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["error"]["code"] == "context_length_exceeded"
    assert key in service._http_bridge_sessions
    close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_context_overflow_does_not_retry_hard_affinity_with_previous_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "instructions": "hi",
            "input": "hello",
            "previous_response_id": "resp_prev_123",
        }
    )

    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "turn_hard_overflow_recover", None)
    initial_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-turn-state": "turn_hard_overflow_recover"},
        affinity=proxy_service._AffinityPolicy(
            key="turn_hard_overflow_recover",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = initial_session

    prepare_previous_response_ids: list[str | None] = []

    def fake_prepare(
        prepared_payload: proxy_service.ResponsesRequest,
        _headers: dict[str, str] | Any,
        *,
        api_key: proxy_service.ApiKeyData | None,
        api_key_reservation: proxy_service.ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> tuple[proxy_service._WebSocketRequestState, str]:
        del api_key, api_key_reservation
        prepare_previous_response_ids.append(prepared_payload.previous_response_id)
        request_state = proxy_service._WebSocketRequestState(
            request_id=request_id,
            model=prepared_payload.model,
            service_tier=None,
            reasoning_effort=None,
            api_key_reservation=None,
            started_at=1.0,
            event_queue=asyncio.Queue(),
            transport="http",
            previous_response_id=prepared_payload.previous_response_id,
            session_id="turn_hard_overflow_recover",
        )
        return request_state, '{"type":"response.create"}'

    stream_attempt = 0

    async def fake_stream_http_bridge_session_events(
        _session: proxy_service._HTTPBridgeSession,
        *,
        request_state: proxy_service._WebSocketRequestState,
        text_data: str,
        queue_limit: int,
        propagate_http_errors: bool,
        downstream_turn_state: str | None,
        submit_lease_held: bool = True,
    ):
        nonlocal stream_attempt
        del request_state, text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        stream_attempt += 1
        if stream_attempt == 1:
            raise ProxyResponseError(
                400,
                proxy_service.openai_error(
                    "context_length_exceeded",
                    "Your input exceeds the context window of this model.",
                    error_type="invalid_request_error",
                ),
            )
        yield 'data: {"type":"response.completed"}\n\n'

    close_session = AsyncMock()
    get_or_create = AsyncMock(return_value=initial_session)

    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        sticky_threads_enabled=False,
                        openai_cache_affinity_max_age_seconds=1800,
                        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
                        http_responses_session_bridge_gateway_safe_mode=False,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_prepare_http_bridge_request", fake_prepare)
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", get_or_create)
    monkeypatch.setattr(service, "_stream_http_bridge_session_events", fake_stream_http_bridge_session_events)
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    with pytest.raises(ProxyResponseError) as exc_info:
        async for _ in service._stream_via_http_bridge(
            payload,
            headers={"x-codex-turn-state": "turn_hard_overflow_recover"},
            codex_session_affinity=True,
            propagate_http_errors=True,
            openai_cache_affinity=True,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
            idle_ttl_seconds=120.0,
            codex_idle_ttl_seconds=900.0,
            max_sessions=8,
            queue_limit=4,
            downstream_turn_state="turn_hard_overflow_recover",
        ):
            pass

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["error"]["code"] == "context_length_exceeded"
    assert prepare_previous_response_ids == ["resp_prev_123"]
    assert stream_attempt == 1
    close_session.assert_not_awaited()
    assert len(get_or_create.await_args_list) == 1


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_returns_owner_forward_for_hard_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_123", None)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )
    service._ring_membership = cast(Any, SimpleNamespace(resolve_endpoint=AsyncMock(return_value="http://instance-b")))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-turn-state": "http_turn_123"},
        affinity=proxy_service._AffinityPolicy(key="http_turn_123"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
    )

    assert isinstance(resolved, proxy_service._HTTPBridgeOwnerForward)
    assert resolved.owner_instance == "instance-b"
    assert resolved.owner_endpoint == "http://instance-b"
    assert resolved.key == key


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_preserves_explicit_forwarded_affinity_on_missing_turn_state_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    captured: dict[str, object] = {}

    async def fake_create_http_bridge_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        *,
        headers: dict[str, str],
        affinity: proxy_service._AffinityPolicy,
        api_key: proxy_service.ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
    ) -> proxy_service._HTTPBridgeSession:
        del headers, affinity, api_key, request_model, idle_ttl_seconds, request_stage, preferred_account_id
        captured["key"] = create_key
        return created_session

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", fake_create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-turn-state": "http_turn_generated"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        forwarded_request=True,
        forwarded_affinity_kind="session_header",
        forwarded_affinity_key="sid-123",
    )

    assert resolved is created_session
    assert captured["key"] == key


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_falls_back_to_session_header_when_turn_state_alias_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    requested_key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_generated", None)
    fallback_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=fallback_key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    captured: dict[str, object] = {}

    async def fake_create_http_bridge_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        *,
        headers: dict[str, str],
        affinity: proxy_service._AffinityPolicy,
        api_key: proxy_service.ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
    ) -> proxy_service._HTTPBridgeSession:
        del headers, affinity, api_key, request_model, idle_ttl_seconds, request_stage, preferred_account_id
        captured["key"] = create_key
        return created_session

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", fake_create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        requested_key,
        headers={
            "x-codex-turn-state": "http_turn_generated",
            "x-codex-session-id": "sid-123",
        },
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
    )

    assert resolved is created_session
    assert captured["key"] == requested_key


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_preserves_durable_canonical_prompt_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    requested_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "pc-123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=requested_key,
        headers={"x-codex-turn-state": "http_turn_generated"},
        affinity=proxy_service._AffinityPolicy(
            key="pc-123",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    captured: dict[str, object] = {}

    async def fake_create_http_bridge_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        *,
        headers: dict[str, str],
        affinity: proxy_service._AffinityPolicy,
        api_key: proxy_service.ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
    ) -> proxy_service._HTTPBridgeSession:
        del headers, affinity, api_key, request_model, idle_ttl_seconds, request_stage, preferred_account_id
        captured["key"] = create_key
        return created_session

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", fake_create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        requested_key,
        headers={"x-codex-turn-state": "http_turn_generated"},
        affinity=proxy_service._AffinityPolicy(
            key="pc-123",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-1",
            canonical_kind="prompt_cache",
            canonical_key="pc-123",
            api_key_scope="__anonymous__",
            account_id="acc-1",
            owner_instance_id="instance-a",
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state="http_turn_generated",
            latest_response_id="resp_prev_1",
        ),
    )

    assert resolved is created_session
    assert captured["key"] == requested_key


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_recovers_from_previous_response_id_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_missing_alias", None)
    recovered_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    recovered_session = proxy_service._HTTPBridgeSession(
        key=recovered_key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
        previous_response_ids={"resp_prev_1"},
    )
    service._http_bridge_sessions[recovered_key] = recovered_session
    service._http_bridge_previous_response_index[
        proxy_service._http_bridge_previous_response_alias_key("resp_prev_1", None)
    ] = recovered_key
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-turn-state": "http_turn_missing_alias"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
    )

    assert resolved is recovered_session
    assert "http_turn_missing_alias" in recovered_session.downstream_turn_state_aliases


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_drops_stale_previous_response_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_missing_alias", None)
    stale_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-stale", None)
    created_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-new", None)
    stale_session = proxy_service._HTTPBridgeSession(
        key=stale_key,
        headers={"x-codex-session-id": "sid-stale"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-stale",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
        closed=True,
        previous_response_ids={"resp_prev_1"},
    )
    created_session = proxy_service._HTTPBridgeSession(
        key=created_key,
        headers={"x-codex-session-id": "sid-new"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-new",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-2", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=3.0,
        idle_ttl_seconds=120.0,
    )
    alias_key = proxy_service._http_bridge_previous_response_alias_key("resp_prev_1", None)
    service._http_bridge_sessions[stale_key] = stale_session
    service._http_bridge_previous_response_index[alias_key] = stale_key
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    create_http_bridge_session = AsyncMock(return_value=created_session)
    monkeypatch.setattr(service, "_create_http_bridge_session", create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={
            "x-codex-turn-state": "http_turn_missing_alias",
            "x-codex-session-id": "sid-new",
        },
        affinity=proxy_service._AffinityPolicy(
            key="sid-new",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
    )

    assert resolved is created_session
    assert alias_key not in service._http_bridge_previous_response_index


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_allows_local_rebind_for_previous_response_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    create_http_bridge_session = AsyncMock(return_value=created_session)
    monkeypatch.setattr(service, "_create_http_bridge_session", create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
        allow_previous_response_recovery_rebind=True,
    )

    assert resolved is created_session


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_allows_local_rebind_for_bootstrap_owner_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    create_http_bridge_session = AsyncMock(return_value=created_session)
    monkeypatch.setattr(service, "_create_http_bridge_session", create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_bootstrap_owner_rebind=True,
    )

    assert resolved is created_session


@pytest.mark.asyncio
async def test_should_attempt_local_bootstrap_rebind_for_session_header_without_turn_state() -> None:
    exc = ProxyResponseError(
        503,
        {"error": {"code": "bridge_owner_unreachable", "message": "owner down", "type": "server_error"}},
    )

    assert (
        proxy_service._http_bridge_should_attempt_local_bootstrap_rebind(
            exc,
            key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
            headers={"x-codex-session-id": "sid-123"},
            previous_response_id=None,
        )
        is True
    )

    assert (
        proxy_service._http_bridge_should_attempt_local_bootstrap_rebind(
            exc,
            key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
            headers={"x-codex-session-id": "sid-123", "x-codex-turn-state": "http_turn_123"},
            previous_response_id=None,
        )
        is False
    )


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_recovers_locally_when_owner_endpoint_missing_without_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-turn-state": "http_turn_123"},
        affinity=proxy_service._AffinityPolicy(key="http_turn_123"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    create_http_bridge_session = AsyncMock(return_value=created_session)
    monkeypatch.setattr(service, "_create_http_bridge_session", create_http_bridge_session)
    claim_durable = AsyncMock()
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", claim_durable)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )
    service._ring_membership = cast(Any, SimpleNamespace(resolve_endpoint=AsyncMock(return_value=None)))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-turn-state": "http_turn_123"},
        affinity=proxy_service._AffinityPolicy(key="http_turn_123"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
    )

    assert resolved is created_session
    claim_durable.assert_awaited_once()
    await_args = claim_durable.await_args
    assert await_args is not None
    assert await_args.kwargs["allow_takeover"] is True
    service._ring_membership.resolve_endpoint.assert_awaited_once_with("instance-b")


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_recovers_locally_when_owner_endpoint_missing_but_replay_anchor_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-turn-state": "http_turn_123"},
        affinity=proxy_service._AffinityPolicy(key="http_turn_123"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    claim_durable = AsyncMock()
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", claim_durable)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )
    service._ring_membership = cast(Any, SimpleNamespace(resolve_endpoint=AsyncMock(return_value=None)))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-turn-state": "http_turn_123"},
        affinity=proxy_service._AffinityPolicy(key="http_turn_123"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
        allow_forward_to_owner=True,
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-1",
            canonical_kind="turn_state_header",
            canonical_key="http_turn_123",
            api_key_scope="__anonymous__",
            account_id="acc-1",
            owner_instance_id="instance-b",
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state="http_turn_123",
            latest_response_id="resp_prev_1",
        ),
    )

    assert resolved is created_session
    claim_durable.assert_awaited_once()
    await_args = claim_durable.await_args
    assert await_args is not None
    assert await_args.kwargs["allow_takeover"] is True


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_recovers_locally_without_anchor_for_single_instance_stale_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "turn_123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-turn-state": "turn_123"},
        affinity=proxy_service._AffinityPolicy(key="turn_123"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    claim_durable = AsyncMock()
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", claim_durable)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    service._ring_membership = None
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ("instance-a",))),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-turn-state": "turn_123"},
        affinity=proxy_service._AffinityPolicy(key="turn_123"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-1",
            canonical_kind="turn_state_header",
            canonical_key="turn_123",
            api_key_scope="__anonymous__",
            account_id="acc-1",
            owner_instance_id="instance-stale",
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state="turn_123",
            latest_response_id=None,
        ),
    )

    assert resolved is created_session
    claim_durable.assert_awaited_once()
    await_args = claim_durable.await_args
    assert await_args is not None
    assert await_args.kwargs["allow_takeover"] is True


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_prompt_cache_takes_over_stale_single_instance_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    claim_durable = AsyncMock()
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", claim_durable)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    service._ring_membership = None
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ("instance-a",))),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-key"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-1",
            canonical_kind="prompt_cache",
            canonical_key="cache-key",
            api_key_scope="__anonymous__",
            account_id="acc-1",
            owner_instance_id="instance-stale",
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state="http_turn_prompt_cache",
            latest_response_id=None,
        ),
    )

    assert resolved is created_session
    claim_durable.assert_awaited_once()
    await_args = claim_durable.await_args
    assert await_args is not None
    assert await_args.kwargs["allow_takeover"] is True


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_discards_local_session_when_durable_owner_is_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    existing_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-stale", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-new", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=3.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = existing_session
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )
    service._ring_membership = cast(Any, SimpleNamespace(resolve_endpoint=AsyncMock(return_value=None)))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        previous_response_id="resp_prev_1",
        allow_forward_to_owner=True,
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-1",
            canonical_kind="session_header",
            canonical_key="sid-123",
            api_key_scope="__anonymous__",
            account_id="acc-1",
            owner_instance_id="instance-b",
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state="http_turn_123",
            latest_response_id="resp_prev_1",
        ),
    )

    assert resolved is created_session
    close_session.assert_awaited_once_with(existing_session)


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_does_not_publish_before_durable_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-race", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-race"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-race",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    close_session = AsyncMock()

    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    monkeypatch.setattr(
        service,
        "_claim_durable_http_bridge_session",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )

    async def _call() -> proxy_service._HTTPBridgeSession:
        return await service._get_or_create_http_bridge_session(
            key,
            headers={"x-codex-session-id": "sid-race"},
            affinity=proxy_service._AffinityPolicy(
                key="sid-race",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=8,
        )

    first = asyncio.create_task(_call())
    await asyncio.sleep(0)
    second = asyncio.create_task(_call())

    with pytest.raises(RuntimeError, match="db unavailable"):
        await first
    with pytest.raises(RuntimeError, match="db unavailable"):
        await second

    assert key not in service._http_bridge_sessions
    assert close_session.await_count >= 1
    assert all(call.args == (created_session,) for call in close_session.await_args_list)


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_waiter_propagates_terminal_inflight_proxy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-race", None)
    inflight_future: asyncio.Future[proxy_service._HTTPBridgeSession] = asyncio.get_running_loop().create_future()
    inflight_future.set_exception(
        ProxyResponseError(
            409,
            proxy_service.openai_error(
                "bridge_instance_mismatch",
                "HTTP bridge session is owned by a different instance; retry to reach the correct replica",
                error_type="server_error",
            ),
        )
    )
    service._http_bridge_inflight_sessions[key] = inflight_future

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ("instance-a",))),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await asyncio.wait_for(
            service._get_or_create_http_bridge_session(
                key,
                headers={"x-codex-session-id": "sid-race"},
                affinity=proxy_service._AffinityPolicy(
                    key="sid-race",
                    kind=proxy_service.StickySessionKind.CODEX_SESSION,
                ),
                api_key=None,
                request_model="gpt-5.4",
                idle_ttl_seconds=120.0,
                max_sessions=8,
            ),
            timeout=0.1,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.payload["error"]["code"] == "bridge_instance_mismatch"


@pytest.mark.asyncio
async def test_close_all_http_bridge_sessions_fails_inflight_waiters() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-shutdown", None)
    inflight_future: asyncio.Future[proxy_service._HTTPBridgeSession] = asyncio.get_running_loop().create_future()
    service._http_bridge_inflight_sessions[key] = inflight_future

    await service.close_all_http_bridge_sessions()

    with pytest.raises(ProxyResponseError) as exc_info:
        await inflight_future

    assert exc_info.value.status_code == 503
    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_close_all_http_bridge_sessions_fails_capacity_waiters_instead_of_creating_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    existing_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-capacity-existing", None)
    existing = proxy_service._HTTPBridgeSession(
        key=existing_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="sid-capacity-existing",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-existing", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=cast(deque[proxy_service._WebSocketRequestState], deque()),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        codex_session=True,
        prewarm_lock=anyio.Lock(),
    )
    service._http_bridge_sessions[existing_key] = existing
    inflight_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-capacity-inflight", None)
    inflight_future: asyncio.Future[proxy_service._HTTPBridgeSession] = asyncio.get_running_loop().create_future()
    service._http_bridge_inflight_sessions[inflight_key] = inflight_future

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_http_bridge_pending_count", AsyncMock(return_value=1))
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_should_wait_for_registration", AsyncMock(return_value=False))
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ("instance-a",))),
    )
    create_http_bridge_session = AsyncMock()
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_close_http_bridge_session", AsyncMock())

    waiter = asyncio.create_task(
        service._get_or_create_http_bridge_session(
            proxy_service._HTTPBridgeSessionKey("session_header", "sid-capacity-request", None),
            headers={"x-codex-session-id": "sid-capacity-request"},
            affinity=proxy_service._AffinityPolicy(
                key="sid-capacity-request",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=1,
        )
    )
    await asyncio.sleep(0)

    await service.close_all_http_bridge_sessions()

    with pytest.raises(ProxyResponseError) as exc_info:
        await asyncio.wait_for(waiter, timeout=0.1)

    assert exc_info.value.status_code == 503
    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    create_http_bridge_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_zero_max_sessions_disables_capacity_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    existing_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-existing", None)
    service._http_bridge_sessions[existing_key] = _make_http_bridge_session(
        key=existing_key,
        account=cast(Any, SimpleNamespace(id="acc-existing", status=AccountStatus.ACTIVE)),
        pending_count=1,
    )
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-new", None)
    created_sessions: list[proxy_service._HTTPBridgeSession] = []

    async def fake_create(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **_kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        created = _make_http_bridge_session(
            key=create_key,
            account=cast(Any, SimpleNamespace(id="acc-new", status=AccountStatus.ACTIVE)),
        )
        created_sessions.append(created)
        return created

    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", fake_create)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ("instance-a",))),
    )
    monkeypatch.setattr(service, "_close_http_bridge_session", AsyncMock())

    created = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-session-id": "sid-new"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-new",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        max_sessions=0,
    )

    assert created is created_sessions[0]
    assert service._http_bridge_sessions[existing_key].closed is False
    assert service._http_bridge_sessions[key] is created


def test_select_http_bridge_busy_parallel_key_zero_max_sessions_scans_without_fixed_cap() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    base_key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-parallel", None)
    service._http_bridge_sessions[proxy_service._http_bridge_busy_parallel_key(base_key, 1)] = (
        _make_http_bridge_session(
            key=proxy_service._http_bridge_busy_parallel_key(base_key, 1),
            account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        )
    )

    selected = service._select_http_bridge_busy_parallel_key_locked(base_key, max_sessions=0)

    assert selected is not None
    assert bridge_keys._http_bridge_busy_parallel_index(selected) == 2


@pytest.mark.asyncio
async def test_claim_durable_http_bridge_session_propagates_claim_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(
        service._durable_bridge,
        "claim_live_session",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())

    with pytest.raises(RuntimeError, match="db unavailable"):
        await service._claim_durable_http_bridge_session(session, allow_takeover=True)


@pytest.mark.asyncio
async def test_claim_durable_http_bridge_session_falls_back_when_tables_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(
        service._durable_bridge,
        "claim_live_session",
        AsyncMock(side_effect=RuntimeError("no such table: http_bridge_sessions")),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())

    await service._claim_durable_http_bridge_session(session, allow_takeover=True)

    assert session.durable_session_id is None
    assert session.durable_owner_epoch is None


@pytest.mark.asyncio
async def test_claim_durable_http_bridge_session_rejects_remote_owner_without_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(
        service._durable_bridge,
        "claim_live_session",
        AsyncMock(
            return_value=proxy_service.DurableBridgeLookup(
                session_id="durable-1",
                canonical_kind="session_header",
                canonical_key="sid-123",
                api_key_scope="__anonymous__",
                account_id="acc-1",
                owner_instance_id="instance-b",
                owner_epoch=2,
                lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
                state=HttpBridgeSessionState.ACTIVE,
                latest_turn_state=None,
                latest_response_id=None,
            )
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._claim_durable_http_bridge_session(session, allow_takeover=False)

    assert exc_info.value.status_code == 409
    assert exc_info.value.payload["error"]["code"] == "bridge_instance_mismatch"


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_hard_continuity_lookup_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    create_http_bridge_session = AsyncMock(return_value=created_session)
    monkeypatch.setattr(service, "_create_http_bridge_session", create_http_bridge_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "_http_bridge_owner_instance",
        AsyncMock(side_effect=ConnectionRefusedError("db unavailable")),
    )
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(side_effect=ConnectionRefusedError("db unavailable")),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._get_or_create_http_bridge_session(
            key,
            headers={"x-codex-session-id": "sid-123"},
            affinity=proxy_service._AffinityPolicy(
                key="sid-123",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=8,
        )

    create_http_bridge_session.assert_not_awaited()
    exc = exc_info.value
    assert exc.status_code == 502
    assert exc.payload["error"]["code"] == "upstream_unavailable"
    assert exc.payload["error"]["message"] == "HTTP bridge owner metadata unavailable; retry later."


@pytest.mark.asyncio
async def test_maybe_prewarm_http_bridge_session_skips_continuity_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        codex_session=True,
        prewarm_lock=anyio.Lock(),
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-1",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        previous_response_id="resp_prev_1",
        transport="http",
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: SimpleNamespace(http_responses_session_bridge_codex_prewarm_enabled=True),
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    await service._maybe_prewarm_http_bridge_session(
        session,
        request_state=request_state,
        text_data='{"model":"gpt-5.4","input":"hello"}',
    )

    assert session.prewarmed is False
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_http_bridge_request_rejects_replaced_session_after_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    service._work_admission = proxy_service.WorkAdmissionController(
        token_refresh_limit=0,
        websocket_connect_limit=0,
        response_create_limit=0,
        compact_response_create_limit=0,
    )
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-stale", None)
    account = cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE))
    send_text = AsyncMock()
    stale_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-stale"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-stale",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=account,
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    replacement_session = _make_http_bridge_session(key=key, account=account)
    service._http_bridge_sessions[key] = replacement_session
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-stale-submit",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_maybe_prewarm_http_bridge_session", AsyncMock())

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request(
            stale_session,
            request_state=request_state,
            text_data='{"type":"response.create","input":"hello"}',
            queue_limit=0,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    send_text.assert_not_awaited()
    assert stale_session.queued_request_count == 0
    assert list(stale_session.pending_requests) == []
    assert request_state.response_create_admission is None
    assert request_state.account_model_concurrency is None


@pytest.mark.asyncio
async def test_retry_http_bridge_request_on_fresh_upstream_reconnects_without_resending_previous_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-1",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        previous_response_id="resp_prev_1",
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session=session,
        request_state=request_state,
        text_data='{"type":"response.create","previous_response_id":"resp_prev_1"}',
        send_request=False,
    )

    assert recovered is True
    assert request_state.replay_count == 1
    reconnect.assert_awaited_once_with(
        session,
        request_state=request_state,
        restart_reader=True,
        prefer_same_account=True,
    )
    send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_http_bridge_request_on_fresh_upstream_refuses_to_resend_previous_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-1",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        previous_response_id="resp_prev_1",
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session=session,
        request_state=request_state,
        text_data='{"type":"response.create","previous_response_id":"resp_prev_1"}',
        send_request=True,
    )

    assert recovered is False
    assert request_state.replay_count == 0
    reconnect.assert_not_awaited()
    send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_http_bridge_request_on_fresh_upstream_resets_response_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-123", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-123",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-created-no-text",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        response_id="resp_created_no_text",
        awaiting_response_created=False,
        request_text='{"type":"response.create","input":"hello"}',
        transport="http",
        http_bridge_send_completed_at=1.0,
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session=session,
        request_state=request_state,
        text_data='{"type":"response.create","input":"hello"}',
        reset_response_state=True,
    )

    assert recovered is True
    assert request_state.replay_count == 1
    assert request_state.response_id is None
    assert request_state.awaiting_response_created is True
    reconnect.assert_awaited_once_with(
        session,
        request_state=request_state,
        restart_reader=True,
        prefer_same_account=True,
    )
    send_text.assert_awaited_once_with('{"type":"response.create","input":"hello"}')
    assert request_state.http_bridge_send_completed_at is not None
    assert request_state.http_bridge_send_completed_at > 1.0
    assert session.last_used_at == request_state.http_bridge_send_completed_at


@pytest.mark.asyncio
async def test_retry_http_bridge_request_on_fresh_upstream_replays_retry_safe_injection_without_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable-anchor injections opt in to fresh-turn replay on send failure.

    The proxy captures the original unanchored full-resend payload before
    injecting ``previous_response_id`` on durable reattach. That text is a
    safe fresh-turn replay target because it already contains the full
    history; dropping the anchor and replaying is equivalent to the
    client's own retry.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-safe", None),
        headers={"x-codex-session-id": "sid-safe"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-safe",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-retry-safe",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        previous_response_id="resp_prev_safe",
        proxy_injected_previous_response_id=True,
        fresh_upstream_request_text='{"type":"response.create","input":"full-history-fallback"}',
        fresh_upstream_request_is_retry_safe=True,
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session=session,
        request_state=request_state,
        text_data='{"type":"response.create","previous_response_id":"resp_prev_safe","input":"full-history-fallback"}',
        send_request=True,
    )

    assert recovered is True
    assert request_state.replay_count == 1
    # Replaying should have dropped the anchor metadata so the request
    # executes as a fresh turn using the captured unanchored payload.
    assert request_state.previous_response_id is None
    assert request_state.proxy_injected_previous_response_id is False
    send_text.assert_awaited_once_with('{"type":"response.create","input":"full-history-fallback"}')


@pytest.mark.asyncio
async def test_retry_http_bridge_request_on_fresh_upstream_refuses_session_level_injection_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session-level injections must not be replayed as fresh turns.

    When the proxy injects ``previous_response_id`` from a bridge session's
    last completed response, the original payload may have relied on the
    anchor for context (for example a single-item follow-up whose prior
    turns live only in the stored conversation). Dropping the anchor and
    replaying would silently turn the continuation into a context-free
    fresh turn and return wrong-but-successful output instead of surfacing
    the retriable send failure.
    """
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-unsafe", None),
        headers={"x-codex-session-id": "sid-unsafe"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-unsafe",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-retry-unsafe",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=1.0,
        previous_response_id="resp_prev_unsafe",
        proxy_injected_previous_response_id=True,
        fresh_upstream_request_text='{"type":"response.create","input":"single-item-followup"}',
        fresh_upstream_request_is_retry_safe=False,
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session=session,
        request_state=request_state,
        text_data='{"type":"response.create","previous_response_id":"resp_prev_unsafe","input":"single-item-followup"}',
        send_request=True,
    )

    assert recovered is False
    assert request_state.replay_count == 0
    reconnect.assert_not_awaited()
    send_text.assert_not_awaited()


def test_http_bridge_can_recover_during_drain_for_previous_response_anchor() -> None:
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_123", None)

    assert (
        proxy_service._http_bridge_can_recover_during_drain(
            key=key,
            headers={"x-codex-turn-state": "http_turn_123"},
            previous_response_id="resp_prev_1",
            durable_lookup=None,
        )
        is True
    )


def test_http_bridge_can_recover_during_drain_for_session_header_bootstrap() -> None:
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)

    assert (
        proxy_service._http_bridge_can_recover_during_drain(
            key=key,
            headers={"x-codex-session-id": "sid-123"},
            previous_response_id=None,
            durable_lookup=None,
        )
        is False
    )


def test_http_bridge_can_recover_during_drain_ignores_soft_prompt_cache_latest_response_anchor() -> None:
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    durable_lookup = proxy_service.DurableBridgeLookup(
        session_id="sess-soft",
        canonical_kind="prompt_cache",
        canonical_key="cache-key",
        api_key_scope="__anonymous__",
        account_id="acc-1",
        owner_instance_id="instance-a",
        owner_epoch=1,
        lease_expires_at=datetime.now(timezone.utc),
        state=HttpBridgeSessionState.ACTIVE,
        latest_turn_state="http_turn_soft",
        latest_response_id="resp_soft",
    )

    assert (
        proxy_service._http_bridge_can_recover_during_drain(
            key=key,
            headers={},
            previous_response_id=None,
            durable_lookup=durable_lookup,
        )
        is False
    )


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_soft_mismatch_rebinds_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-fresh", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-key"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
        gateway_safe_mode=True,
    )

    assert resolved is created_session


@pytest.mark.asyncio
async def test_create_http_bridge_session_retries_previous_response_owner_without_using_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None)
    preferred_account = cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE))
    fallback_account = cast(Any, SimpleNamespace(id="acc-fallback", status=AccountStatus.ACTIVE))
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=preferred_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=fallback_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=preferred_account, error_message=None, error_code=None),
        ]
    )
    ensure_fresh = AsyncMock(side_effect=[aiohttp.ClientError("preferred connect failed"), preferred_account])
    open_upstream = AsyncMock(
        return_value=cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))
    )

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                        sticky_reallocation_budget_threshold_pct=95.0,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", ensure_fresh)
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_upstream)
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    session = await service._create_http_bridge_session(
        key,
        headers={"x-codex-session-id": "sid-123"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-123",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        request_stage="reattach",
        preferred_account_id="acc-owner",
        require_preferred_account=True,
    )

    assert session.account is preferred_account
    assert select_account.await_count == 3
    assert ensure_fresh.await_count == 2
    open_upstream.assert_awaited_once()
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_create_http_bridge_session_fails_over_on_connect_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-forbidden", None)
    first_account = cast(Any, SimpleNamespace(id="acc-forbidden-a", status=AccountStatus.ACTIVE))
    second_account = cast(Any, SimpleNamespace(id="acc-forbidden-b", status=AccountStatus.ACTIVE))
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=first_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=second_account, error_message=None, error_code=None),
        ]
    )
    forbidden_error = ProxyResponseError(
        403,
        proxy_service.openai_error("forbidden", "Forbidden", error_type="permission_error"),
    )
    upstream = cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))
    open_upstream = AsyncMock(side_effect=[forbidden_error, upstream])
    record_error = AsyncMock()

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                        sticky_reallocation_budget_threshold_pct=95.0,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(side_effect=[first_account, second_account]))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_upstream)
    monkeypatch.setattr(service._load_balancer, "record_error", record_error)
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    session = await service._create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-forbidden"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
    )

    assert session.account is second_account
    assert session.upstream is upstream
    assert select_account.await_count == 2
    second_call = select_account.await_args_list[1]
    assert second_call.kwargs["exclude_account_ids"] == {"acc-forbidden-a"}
    record_error.assert_awaited_once_with(first_account)
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_reconnect_http_bridge_session_can_avoid_current_account_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-reconnect-failover", None)
    old_account = cast(Any, SimpleNamespace(id="acc-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-new", status=AccountStatus.ACTIVE))
    old_upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    new_upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()),
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-reconnect-failover",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=old_account,
        upstream=old_upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reconnect-avoid-current",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        transport="http",
    )
    select_calls: list[dict[str, object]] = []

    async def fake_select_account_with_budget(deadline: float, **kwargs: object) -> proxy_service.AccountSelection:
        del deadline
        select_calls.append(kwargs)
        assert kwargs["preferred_account_id"] is None
        assert kwargs["exclude_account_ids"] == {"acc-old"}
        return proxy_service.AccountSelection(account=new_account, error_message=None, error_code=None)

    class _Lease:
        def release(self) -> None:
            return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", fake_select_account_with_budget)
    monkeypatch.setattr(service, "_try_acquire_http_bridge_session_account_model_concurrency", lambda **_: _Lease())
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=new_account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=new_upstream))

    await service._reconnect_http_bridge_session(
        session,
        request_state=request_state,
        prefer_same_account=False,
    )

    assert select_calls
    assert session.account is new_account
    assert session.upstream is new_upstream
    old_upstream.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_http_bridge_session_translates_exhausted_connect_forbidden_to_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-forbidden-exhausted", None)
    accounts = [
        cast(Any, SimpleNamespace(id=f"acc-forbidden-{index}", status=AccountStatus.ACTIVE)) for index in range(3)
    ]
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=account, error_message=None, error_code=None) for account in accounts
        ]
    )
    forbidden_error = ProxyResponseError(
        403,
        proxy_service.openai_error("forbidden", "Forbidden", error_type="permission_error"),
    )
    record_error = AsyncMock()

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                        sticky_reallocation_budget_threshold_pct=95.0,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(side_effect=accounts))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(side_effect=forbidden_error))
    monkeypatch.setattr(service._load_balancer, "record_error", record_error)

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-forbidden-exhausted"),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    assert select_account.await_count == 3
    assert record_error.await_count == 3


@pytest.mark.asyncio
async def test_create_http_bridge_session_uses_high_waterline_without_active_session_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-old", status=AccountStatus.ACTIVE))
    fresh_account = cast(Any, SimpleNamespace(id="2908709191@qq.com", status=AccountStatus.ACTIVE))
    existing_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "existing-cache", None)
    service._http_bridge_sessions[existing_key] = proxy_service._HTTPBridgeSession(
        key=existing_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="existing-cache"),
        request_model="gpt-5.4",
        account=old_account,
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    select_account = AsyncMock(
        return_value=proxy_service.AccountSelection(account=fresh_account, error_message=None, error_code=None)
    )
    upstream = cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=fresh_account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=upstream))
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    session = await service._create_http_bridge_session(
        proxy_service._HTTPBridgeSessionKey("prompt_cache", "new-cache", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="new-cache"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
    )

    assert session.account is fresh_account
    assert select_account.await_count == 1
    assert select_account.await_args.kwargs["exclude_account_ids"] == set()
    assert select_account.await_args.kwargs["routing_strategy"] == "high_waterline"
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_create_http_bridge_session_surfaces_no_account_without_active_session_bias_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-old", status=AccountStatus.ACTIVE))
    existing_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "existing-cache", None)
    service._http_bridge_sessions[existing_key] = proxy_service._HTTPBridgeSession(
        key=existing_key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="existing-cache"),
        request_model="gpt-5.4",
        account=old_account,
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    select_account = AsyncMock(
        return_value=proxy_service.AccountSelection(
            account=None,
            error_message="No active accounts available",
            error_code=None,
        )
    )
    upstream = cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=old_account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=upstream))
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._create_http_bridge_session(
            proxy_service._HTTPBridgeSessionKey("prompt_cache", "new-cache", None),
            headers={},
            affinity=proxy_service._AffinityPolicy(key="new-cache"),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
        )

    assert exc_info.value.status_code == 503
    assert select_account.await_count == 1
    assert select_account.await_args.kwargs["exclude_account_ids"] == set()
    assert select_account.await_args.kwargs["routing_strategy"] == "high_waterline"


@pytest.mark.asyncio
async def test_create_http_bridge_session_keeps_high_waterline_preference_with_active_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    hot_account = cast(Any, SimpleNamespace(id="acc-hot", status=AccountStatus.ACTIVE))
    low_active_account = cast(Any, SimpleNamespace(id="acc-low-active", status=AccountStatus.ACTIVE))
    for key in (
        proxy_service._HTTPBridgeSessionKey("prompt_cache", "hot-cache-1", None),
        proxy_service._HTTPBridgeSessionKey("prompt_cache", "hot-cache-2", None),
    ):
        service._http_bridge_sessions[key] = _make_http_bridge_session(key=key, account=hot_account)
    low_active_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "low-active-cache", None)
    service._http_bridge_sessions[low_active_key] = _make_http_bridge_session(
        key=low_active_key,
        account=low_active_account,
    )
    select_account = AsyncMock(
        return_value=proxy_service.AccountSelection(account=hot_account, error_message=None, error_code=None)
    )
    upstream = cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=hot_account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=upstream))
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    session = await service._create_http_bridge_session(
        proxy_service._HTTPBridgeSessionKey("prompt_cache", "new-cache", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="new-cache"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
    )

    assert session.account is hot_account
    assert select_account.await_count == 1
    assert select_account.await_args.kwargs["exclude_account_ids"] == set()
    assert select_account.await_args.kwargs["routing_strategy"] == "high_waterline"
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_create_http_bridge_session_reclaims_idle_session_when_account_model_slots_are_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-full", status=AccountStatus.ACTIVE))
    old_key = proxy_service._HTTPBridgeSessionKey("session_header", "old-sid", None)
    old_lease = service._http_bridge_account_model_sessions.try_acquire(
        account_id=account.id,
        model="gpt-5.4",
        limit=1,
    )
    assert old_lease is not None
    old_upstream = SimpleNamespace(close=AsyncMock())
    old_session = _make_http_bridge_session(key=old_key, account=account)
    old_session.request_model = "gpt-5.4"
    old_session.upstream = cast(UpstreamResponsesWebSocket, old_upstream)
    old_session.account_model_session_lease = old_lease
    service._http_bridge_sessions[old_key] = old_session
    app_settings = Settings(
        http_responses_session_bridge_enabled=True,
        http_responses_session_bridge_soft_shard_max_shards=1,
        proxy_http_bridge_account_model_session_limit=1,
        proxy_http_bridge_account_model_connect_limit=0,
        proxy_account_model_concurrency_limit=0,
    )

    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=None, error_message="No available accounts", error_code=None),
            proxy_service.AccountSelection(account=account, error_message=None, error_code=None),
        ]
    )

    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()),
    )

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(proxy_service, "get_settings", lambda: app_settings)
    monkeypatch.setattr(
        proxy_service,
        "get_settings_cache",
        lambda: cast(
            Any,
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                        sticky_reallocation_budget_threshold_pct=95.0,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=upstream))
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    new_session = await service._create_http_bridge_session(
        proxy_service._HTTPBridgeSessionKey("session_header", "new-sid", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="new-sid", kind=proxy_service.StickySessionKind.CODEX_SESSION),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
    )

    assert old_key not in service._http_bridge_sessions
    assert old_session.closed is True
    old_upstream.close.assert_awaited_once()
    assert new_session.account is account
    assert select_account.await_count == 2
    assert new_session.account_model_session_lease is not None
    assert service._http_bridge_account_model_sessions.active_count(account_id=account.id, model="gpt-5.4") == 1
    await service._close_http_bridge_session(new_session)


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_prompt_cache_mismatch_stays_local_when_gateway_safe_mode_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )
    created_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-fresh", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=created_session))
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-key"),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
        gateway_safe_mode=False,
    )

    assert resolved is created_session


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_sticky_thread_mismatch_forwards_in_gateway_safe_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("sticky_thread", "thread-key", None)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )
    service._ring_membership = cast(Any, SimpleNamespace(resolve_endpoint=AsyncMock(return_value="http://instance-b")))

    resolved = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="thread-key", kind=proxy_service.StickySessionKind.STICKY_THREAD),
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        allow_forward_to_owner=True,
        gateway_safe_mode=True,
    )

    assert isinstance(resolved, proxy_service._HTTPBridgeOwnerForward)
    assert resolved.owner_instance == "instance-b"
    assert resolved.owner_endpoint == "http://instance-b"


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_prevents_forward_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "http_turn_123", None)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    create_http_bridge_session = AsyncMock()
    monkeypatch.setattr(service, "_create_http_bridge_session", create_http_bridge_session)
    claim_durable = AsyncMock()
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", claim_durable)
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-b"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a", "instance-b"])),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._get_or_create_http_bridge_session(
            key,
            headers={"x-codex-turn-state": "http_turn_123"},
            affinity=proxy_service._AffinityPolicy(key="http_turn_123"),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=8,
            allow_forward_to_owner=True,
            forwarded_request=True,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.payload["error"]["code"] == "bridge_forward_loop_prevented"
    create_http_bridge_session.assert_not_awaited()
    claim_durable.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_replaces_live_session_when_scope_becomes_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("request", "bridge-key", "key-1")
    stale_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4-mini",
        account=cast(Any, SimpleNamespace(id="acc-stale", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
    )
    replacement_session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-fresh", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=0,
        last_used_at=2.0,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = stale_session
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock())
    monkeypatch.setattr(
        service,
        "_create_http_bridge_session",
        AsyncMock(return_value=replacement_session),
    )
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: _make_app_settings(),
    )
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="instance-a"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("instance-a", ["instance-a"])),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    reused = await service._get_or_create_http_bridge_session(
        key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        api_key=_make_api_key(
            key_id="key-1",
            assigned_account_ids=[],
            account_assignment_scope_enabled=True,
        ),
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
    )

    assert reused is replacement_session
    assert service._http_bridge_sessions[key] is replacement_session
    assert stale_session.closed is True
    assert any(call.args == (stale_session,) for call in close_session.await_args_list)


@pytest.mark.asyncio
async def test_http_bridge_reader_evicts_session_after_upstream_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-key", None)
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-http-reader-disconnect",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(
            receive=AsyncMock(
                return_value=UpstreamWebSocketMessage(
                    kind="error",
                    close_code=1011,
                    error="sent 1011 (internal error) keepalive ping timeout; no close frame received",
                )
            ),
            close=AsyncMock(),
        ),
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    session.downstream_turn_state_aliases.add("turn-state-1")
    session.previous_response_ids.add("resp_1")
    service._http_bridge_sessions[key] = session
    service._http_bridge_turn_state_index[
        proxy_service._http_bridge_turn_state_alias_key("turn-state-1", key.api_key_id)
    ] = key
    service._http_bridge_previous_response_index[
        proxy_service._http_bridge_previous_response_alias_key("resp_1", key.api_key_id)
    ] = key

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    write_request_log = AsyncMock()
    monkeypatch.setattr(service, "_write_request_log", write_request_log)

    await service._relay_http_bridge_upstream_messages(session)

    failed_event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
    assert failed_event is not None
    assert '"code":"stream_incomplete"' in failed_event
    assert "keepalive ping timeout" in failed_event
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    assert key not in service._http_bridge_sessions
    assert not service._http_bridge_turn_state_index
    assert not service._http_bridge_previous_response_index
    assert session.downstream_turn_state_aliases == set()
    assert session.previous_response_ids == set()
    assert session.closed is True
    upstream.close.assert_awaited_once()
    write_request_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_bridge_reader_times_out_before_response_created_despite_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-startup-timeout", None)
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-startup-timeout",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        previous_response_id="resp-client-owned",
        awaiting_response_created=True,
        event_queue=event_queue,
        request_text='{"type":"response.create","previous_response_id":"resp-client-owned"}',
        transport="http",
        session_id="session-startup-timeout",
        http_bridge_send_completed_at=time.monotonic(),
    )
    upstream_messages: asyncio.Queue[UpstreamWebSocketMessage] = asyncio.Queue()
    await upstream_messages.put(
        UpstreamWebSocketMessage(
            kind="text",
            text='{"type":"codex.rate_limits","rate_limits":{}}',
        )
    )
    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=upstream_messages.get, close=AsyncMock()),
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-startup-timeout"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-startup-timeout", status=AccountStatus.ACTIVE)),
        upstream=upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_request_budget_seconds=30.0,
            stream_idle_timeout_seconds=30.0,
            http_responses_session_bridge_response_created_timeout_seconds=0.05,
        ),
    )
    write_request_log = AsyncMock()
    monkeypatch.setattr(service, "_write_request_log", write_request_log)
    fail_timeout_requests = service._fail_response_created_timeout_requests

    async def fail_timeout_requests_while_retirement_is_exclusive(*args: Any, **kwargs: Any):
        assert session.lifecycle_lock.locked()
        return await fail_timeout_requests(*args, **kwargs)

    monkeypatch.setattr(
        service,
        "_fail_response_created_timeout_requests",
        fail_timeout_requests_while_retirement_is_exclusive,
    )

    await asyncio.wait_for(service._relay_http_bridge_upstream_messages(session), timeout=1.0)

    metadata_event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
    timeout_event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
    assert request_state.http_bridge_upstream_first_event_type == "codex.rate_limits"
    assert metadata_event is not None and '"type":"codex.rate_limits"' in metadata_event
    assert timeout_event is not None and '"code":"response_created_timeout"' in timeout_event
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    assert key not in service._http_bridge_sessions
    assert session.closed is True
    write_request_log.assert_awaited_once()
    assert write_request_log.await_args.kwargs["session_id"] == "session-startup-timeout"


@pytest.mark.asyncio
async def test_http_bridge_reader_retires_siblings_after_response_created_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-startup-sibling", None)
    expired_queue: asyncio.Queue[str | None] = asyncio.Queue()
    sibling_queue: asyncio.Queue[str | None] = asyncio.Queue()
    expired_request = proxy_service._WebSocketRequestState(
        request_id="req-startup-expired",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        awaiting_response_created=True,
        event_queue=expired_queue,
        request_text='{"type":"response.create","input":[]}',
        transport="http",
        http_bridge_send_completed_at=time.monotonic() - 1.0,
    )
    sibling_request = proxy_service._WebSocketRequestState(
        request_id="req-startup-sibling",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="resp-startup-sibling",
        event_queue=sibling_queue,
        transport="http",
    )
    upstream_messages: asyncio.Queue[UpstreamWebSocketMessage] = asyncio.Queue()
    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=upstream_messages.get, close=AsyncMock()),
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-startup-sibling"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-startup-sibling", status=AccountStatus.ACTIVE)),
        upstream=upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([expired_request, sibling_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=2,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_request_budget_seconds=30.0,
            stream_idle_timeout_seconds=30.0,
            http_responses_session_bridge_response_created_timeout_seconds=0.01,
        ),
    )
    monkeypatch.setattr(service, "_write_request_log", AsyncMock())

    await asyncio.wait_for(service._relay_http_bridge_upstream_messages(session), timeout=1.0)

    expired_event = await asyncio.wait_for(expired_queue.get(), timeout=0.1)
    sibling_event = await asyncio.wait_for(sibling_queue.get(), timeout=0.1)
    assert expired_event is not None and '"code":"response_created_timeout"' in expired_event
    assert sibling_event is not None and '"code":"stream_incomplete"' in sibling_event
    assert "another request timed out before response.created" in sibling_event
    assert key not in service._http_bridge_sessions


@pytest.mark.asyncio
async def test_http_bridge_reader_ignores_stale_response_created_timeout_after_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-startup-detached", None)
    now = time.monotonic()
    expired_request = proxy_service._WebSocketRequestState(
        request_id="req-startup-detached",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=now,
        awaiting_response_created=True,
        event_queue=asyncio.Queue(),
        request_text='{"type":"response.create","input":[]}',
        transport="http",
        http_bridge_send_completed_at=now,
    )
    sibling_queue: asyncio.Queue[str | None] = asyncio.Queue()
    sibling_request = proxy_service._WebSocketRequestState(
        request_id="req-startup-later-sibling",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=now,
        awaiting_response_created=True,
        event_queue=sibling_queue,
        request_text='{"type":"response.create","input":["later"]}',
        transport="http",
        http_bridge_send_completed_at=now + 10.0,
    )
    second_receive_started = asyncio.Event()
    receive_count = 0

    async def receive() -> UpstreamWebSocketMessage:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            async with session.pending_lock:
                session.pending_requests.remove(expired_request)
                session.queued_request_count -= 1
        else:
            second_receive_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=receive, close=AsyncMock()),
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-startup-detached"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-startup-detached", status=AccountStatus.ACTIVE)),
        upstream=upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([expired_request, sibling_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=2,
        last_used_at=now,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_request_budget_seconds=30.0,
            stream_idle_timeout_seconds=30.0,
            http_responses_session_bridge_response_created_timeout_seconds=0.05,
        ),
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session", reconnect)

    reader = asyncio.create_task(service._relay_http_bridge_upstream_messages(session))
    await asyncio.wait_for(second_receive_started.wait(), timeout=0.5)

    assert reader.done() is False
    assert list(session.pending_requests) == [sibling_request]
    assert sibling_queue.empty()
    reconnect.assert_not_awaited()

    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


@pytest.mark.asyncio
async def test_http_bridge_terminal_queue_closes_after_finalization_even_when_finalization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-terminal-order",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=event_queue,
        transport="http",
        response_id="resp-terminal-order",
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "terminal-order", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="terminal-order"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-terminal-order", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )

    async def fail_finalization(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        assert list(event_queue._queue) == [
            'data: {"type":"response.completed","response":{"id":"resp-terminal-order"}}\n\n'
        ]
        raise RuntimeError("finalization failed")

    monkeypatch.setattr(service, "_finalize_websocket_request_state", fail_finalization)

    with pytest.raises(RuntimeError, match="finalization failed"):
        await service._process_http_bridge_upstream_text(
            session,
            '{"type":"response.completed","response":{"id":"resp-terminal-order"}}',
        )

    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) == (
        'data: {"type":"response.completed","response":{"id":"resp-terminal-order"}}\n\n'
    )
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None


@pytest.mark.asyncio
async def test_http_bridge_reader_unexpected_processing_error_fails_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-http-reader-crash",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    event_queue = request_state.event_queue
    assert event_queue is not None
    await asyncio.wait_for(event_queue.put("seed"), timeout=0.1)
    await asyncio.wait_for(event_queue.get(), timeout=0.1)
    gate = asyncio.Semaphore(1)
    await gate.acquire()
    request_state.response_create_gate_acquired = True
    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(
            receive=AsyncMock(return_value=SimpleNamespace(kind="text", text='{"type":"response.created"}')),
            close=AsyncMock(),
        ),
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-key", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-key"),
        request_model="gpt-5.4",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=gate,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_process_http_bridge_upstream_text", AsyncMock(side_effect=RuntimeError("boom")))
    write_request_log = AsyncMock()
    monkeypatch.setattr(service, "_write_request_log", write_request_log)

    await service._relay_http_bridge_upstream_messages(session)

    failed_event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
    assert failed_event is not None
    assert '"code":"stream_incomplete"' in failed_event
    assert "reader" in failed_event
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    assert session.closed is True
    assert list(session.pending_requests) == []
    write_request_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_websocket_reader_unexpected_processing_error_fails_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-ws-reader-crash",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        transport="websocket",
    )
    gate = asyncio.Semaphore(1)
    await gate.acquire()
    request_state.response_create_gate_acquired = True
    pending_requests: deque[proxy_service._WebSocketRequestState] = deque([request_state])
    pending_lock = anyio.Lock()
    send_text = AsyncMock()
    websocket = cast(
        WebSocket,
        SimpleNamespace(send_text=send_text, send_bytes=AsyncMock(), close=AsyncMock()),
    )
    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(
            receive=AsyncMock(return_value=SimpleNamespace(kind="text", text='{"type":"response.created"}')),
            close=AsyncMock(),
        ),
    )

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_process_upstream_websocket_text", AsyncMock(side_effect=RuntimeError("boom")))
    write_request_log = AsyncMock()
    monkeypatch.setattr(service, "_write_request_log", write_request_log)

    await service._relay_upstream_websocket_messages(
        websocket,
        upstream,
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        account_id_value="acc-1",
        pending_requests=pending_requests,
        pending_lock=pending_lock,
        client_send_lock=anyio.Lock(),
        api_key=None,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        response_create_gate=gate,
        proxy_request_budget_seconds=60.0,
        stream_idle_timeout_seconds=60.0,
        downstream_activity=proxy_service._DownstreamWebSocketActivity(),
    )

    send_text.assert_awaited()
    terminal_payload = send_text.await_args_list[0].args[0]
    assert '"code":"stream_incomplete"' in terminal_payload
    assert "reader" in terminal_payload
    assert list(pending_requests) == []
    write_request_log.assert_awaited_once()
