from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import aiohttp
import anyio
import pytest
from fastapi import WebSocket

from app.core.auth.refresh import RefreshError
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket, UpstreamWebSocketMessage
from app.core.config.settings import Settings
from app.db.models import AccountStatus, HttpBridgeSessionState
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import keys as bridge_keys
from app.modules.proxy._service.http_bridge import lifecycle as bridge_lifecycle
from app.modules.proxy._service.http_bridge import request_submit as bridge_request_submit
from app.modules.proxy._service.http_bridge import stream as bridge_stream
from app.modules.proxy._service.http_bridge import upstream_events as bridge_upstream_events
from app.modules.proxy._service.support import _DiscardedRequestAccounting, _WebSocketReceiveTimeout
from app.modules.proxy._service.websocket import relay as proxy_websocket_relay
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLease
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
async def test_stream_via_http_bridge_converts_post_keepalive_startup_failure_to_sse(
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
    release_failure = asyncio.Event()
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-startup-failure",
        key_id="key-startup-failure",
        model="gpt-5.4",
    )
    release_reservation = AsyncMock()

    async def fail_get_or_create(*_args: Any, **_kwargs: Any) -> proxy_service._HTTPBridgeSession:
        await release_failure.wait()
        raise ProxyResponseError(
            502,
            proxy_service.openai_error(
                "upstream_unavailable",
                "upstream websocket handshake failed",
            ),
        )

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
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fail_get_or_create)
    monkeypatch.setattr(service, "_release_websocket_reservation", release_reservation)

    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-startup-failure"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
        idle_ttl_seconds=120.0,
        codex_idle_ttl_seconds=1800.0,
        max_sessions=8,
        queue_limit=4,
    )
    try:
        keepalive = proxy_service.parse_sse_data_json(await asyncio.wait_for(anext(stream), timeout=1.0))
        assert keepalive is not None
        assert keepalive["type"] == "codex.keepalive"

        release_failure.set()
        terminal = proxy_service.parse_sse_data_json(await asyncio.wait_for(anext(stream), timeout=1.0))
        assert terminal is not None
        assert terminal["type"] == "response.failed"
        response = terminal["response"]
        assert isinstance(response, dict)
        error = response["error"]
        assert isinstance(error, dict)
        assert error["code"] == "upstream_unavailable"
        assert error["message"] == "HTTP bridge owner stream failed after acceptance"
        await service.close_proxy_cleanup_tasks()
        release_reservation.assert_awaited_once_with(reservation)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_settles_abandoned_startup_failure(
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
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-abandoned-startup",
        key_id="key-abandoned-startup",
        model="gpt-5.4",
    )
    release_reservation = AsyncMock()

    async def fail_when_cancelled(*_args: Any, **_kwargs: Any) -> proxy_service._HTTPBridgeSession:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise ProxyResponseError(
                502,
                proxy_service.openai_error("upstream_unavailable", "cancel raced startup failure"),
            ) from exc
        raise AssertionError("unreachable")

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
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", fail_when_cancelled)
    monkeypatch.setattr(service, "_release_websocket_reservation", release_reservation)

    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-abandoned-startup"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
        idle_ttl_seconds=120.0,
        codex_idle_ttl_seconds=1800.0,
        max_sessions=8,
        queue_limit=4,
    )
    try:
        keepalive = proxy_service.parse_sse_data_json(await asyncio.wait_for(anext(stream), timeout=1.0))
        assert keepalive is not None
        assert keepalive["type"] == "codex.keepalive"
        await stream.aclose()
        await asyncio.sleep(0)
        release_reservation.assert_awaited_once_with(reservation)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_exception_handler)


@pytest.mark.asyncio
async def test_stream_via_http_bridge_releases_session_returned_while_startup_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-unclaimed-startup", None),
        account=cast(Any, SimpleNamespace(id="acc-unclaimed", status=AccountStatus.ACTIVE)),
    )
    session.submit_lease_count = 1

    cancellation_observed = asyncio.Event()

    async def complete_after_keepalive(*_args: Any, **_kwargs: Any) -> proxy_service._HTTPBridgeSession:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            return session

    release_submit_lease = AsyncMock()
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
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", complete_after_keepalive)
    monkeypatch.setattr(service, "_release_http_bridge_submit_lease", release_submit_lease)

    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-unclaimed-startup"},
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
    assert '"type":"codex.keepalive"' in await asyncio.wait_for(anext(stream), timeout=1.0)
    await stream.aclose()

    assert cancellation_observed.is_set()
    release_submit_lease.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_stream_via_http_bridge_reconciles_session_returned_after_cancellation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-late-unclaimed-startup", None),
        account=cast(Any, SimpleNamespace(id="acc-late-unclaimed", status=AccountStatus.ACTIVE)),
    )
    session.submit_lease_count = 1
    cancellation_observed = asyncio.Event()
    allow_late_return = asyncio.Event()
    submit_lease_released = asyncio.Event()

    async def complete_after_cancellation_timeout(
        *_args: Any,
        **_kwargs: Any,
    ) -> proxy_service._HTTPBridgeSession:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            await allow_late_return.wait()
            return session

    async def release_submit_lease(returned_session: proxy_service._HTTPBridgeSession) -> None:
        assert returned_session is session
        returned_session.submit_lease_count -= 1
        submit_lease_released.set()

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
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", complete_after_cancellation_timeout)
    monkeypatch.setattr(service, "_release_http_bridge_submit_lease", release_submit_lease)

    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-late-unclaimed-startup"},
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
    assert '"type":"codex.keepalive"' in await asyncio.wait_for(anext(stream), timeout=1.0)
    await asyncio.wait_for(stream.aclose(), timeout=1.5)

    assert cancellation_observed.is_set()
    assert session.submit_lease_count == 1
    assert service._proxy_cleanup_tasks

    allow_late_return.set()
    await asyncio.wait_for(submit_lease_released.wait(), timeout=0.2)
    await service.close_proxy_cleanup_tasks()

    assert session.submit_lease_count == 0


@pytest.mark.asyncio
async def test_get_or_create_http_bridge_session_deadline_is_hard_when_acquisition_suppresses_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "hard-acquire-deadline", None)
    acquisition_started = asyncio.Event()
    allow_cancelled_operation_to_finish = asyncio.Event()

    async def cancellation_suppressing_registration(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        acquisition_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await allow_cancelled_operation_to_finish.wait()
            raise RuntimeError("late acquisition failure")

    monkeypatch.setattr(
        service,
        "_http_bridge_should_wait_for_registration_compatible",
        cancellation_suppressing_registration,
    )
    started_at = time.monotonic()
    with pytest.raises(ProxyResponseError) as exc_info:
        await asyncio.wait_for(
            service._get_or_create_http_bridge_session(
                key,
                headers={},
                affinity=proxy_service._AffinityPolicy(key=key.affinity_key),
                api_key=None,
                request_model="gpt-5.4",
                idle_ttl_seconds=120.0,
                max_sessions=8,
                request_deadline_at=time.monotonic() + 0.02,
            ),
            timeout=0.2,
        )

    assert time.monotonic() - started_at < 0.2
    assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
    assert acquisition_started.is_set()
    assert service._proxy_cleanup_tasks
    allow_cancelled_operation_to_finish.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_stream_via_http_bridge_closes_owner_forward_child_on_downstream_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-owner-close", None),
    )
    child_closed = asyncio.Event()

    async def owner_stream() -> AsyncIterator[str]:
        try:
            yield 'data: {"type":"response.created","response":{"id":"resp-owner"}}\n\n'
            await asyncio.Event().wait()
        finally:
            child_closed.set()

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
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(return_value=owner_forward))
    monkeypatch.setattr(service, "_forward_http_bridge_request_to_owner", lambda **_kwargs: owner_stream())

    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-owner-close"},
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
    assert '"type":"response.created"' in await asyncio.wait_for(anext(stream), timeout=1.0)
    await stream.aclose()

    assert child_closed.is_set()


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


def test_http_bridge_post_accept_failure_preserves_capability_unavailable_code() -> None:
    frame = proxy_service._http_bridge_post_accept_failure_frame(
        ProxyResponseError(
            503,
            proxy_service.openai_error(
                "upstream_capability_unavailable",
                "No eligible native Responses provider",
            ),
        )
    )

    event = proxy_service.parse_sse_data_json(frame)
    assert event is not None
    response = event["response"]
    assert isinstance(response, dict)
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "upstream_capability_unavailable"
    assert error["message"] == "No compatible upstream provider is available"


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
    assert first_task in service._proxy_cleanup_tasks
    await asyncio.wait_for(refresh_started.wait(), timeout=1.0)

    service._schedule_durable_http_bridge_session_refresh(session)
    assert session.durable_renew_task is first_task
    assert refresh_calls == 1

    allow_refresh.set()
    await asyncio.wait_for(first_task, timeout=1.0)
    await asyncio.sleep(0)
    assert session.durable_renew_task is None
    assert first_task not in service._proxy_cleanup_tasks

    service._schedule_durable_http_bridge_session_refresh(session)
    await asyncio.sleep(0)
    assert refresh_calls == 1

    session.durable_renew_after = 0.0
    service._schedule_durable_http_bridge_session_refresh(session)
    second_task = session.durable_renew_task
    assert second_task is not None
    assert second_task in service._proxy_cleanup_tasks
    await asyncio.wait_for(second_task, timeout=1.0)
    await asyncio.sleep(0)
    assert second_task not in service._proxy_cleanup_tasks
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
async def test_durable_http_bridge_refresh_lost_cas_detaches_and_schedules_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-refresh-lost", None),
        account=SimpleNamespace(id="acc-refresh-lost", status=AccountStatus.ACTIVE),
    )
    session.durable_session_id = "durable-refresh-lost"
    session.durable_owner_epoch = 3
    session.durable_lease_expires_at = proxy_service.utcnow() - timedelta(seconds=1)
    service._http_bridge_sessions[session.key] = session
    detached = asyncio.get_running_loop().create_future()
    detached.set_result(None)
    schedule_close = Mock(return_value=detached)
    monkeypatch.setattr(service._durable_bridge, "renew_live_session", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_schedule_http_bridge_session_close", schedule_close)

    await service._refresh_durable_http_bridge_session(session)

    assert session.durable_ownership_lost is True
    assert session.durable_ownership_retirement_scheduled is True
    assert session.closed is True
    assert session.key not in service._http_bridge_sessions
    schedule_close.assert_called_once_with(
        session,
        reason="durable-refresh-lost",
        error_code="stream_incomplete",
        error_message="HTTP bridge durable ownership was lost during renewal",
    )


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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
            request_deadline_at=time.monotonic() + 7200.0,
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
async def test_forward_http_bridge_request_to_owner_suppresses_replay_on_relay_timeout_before_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-123", None),
    )
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-relay-timeout",
        key_id="key-relay-timeout",
        model="gpt-5.4",
    )

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
    release_reservation = AsyncMock()
    release_unclaimed_reservation = AsyncMock()
    monkeypatch.setattr(service, "_release_websocket_reservation", release_reservation)
    monkeypatch.setattr(
        service,
        "_release_unclaimed_websocket_reservation",
        release_unclaimed_reservation,
    )

    chunks = [
        chunk
        async for chunk in service._forward_http_bridge_request_to_owner(
            owner_forward=owner_forward,
            payload=payload,
            headers={"x-codex-session-id": "sid-123"},
            api_key_reservation=reservation,
            codex_session_affinity=True,
            downstream_turn_state="http_turn_generated",
            request_started_at=10.0,
            request_deadline_at=time.monotonic() + 7200.0,
            proxy_api_authorization=None,
        )
    ]

    assert chunks == ["data: ignored\n\n"]
    await service.close_proxy_cleanup_tasks()
    release_reservation.assert_not_awaited()
    release_unclaimed_reservation.assert_awaited_once_with(reservation)


@pytest.mark.asyncio
async def test_forwarded_owner_stream_rejects_expired_deadline_before_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    stream = service._stream_via_http_bridge(
        payload,
        headers={"x-codex-session-id": "sid-owner-claim"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=None,
        suppress_text_done_events=False,
        idle_ttl_seconds=120.0,
        codex_idle_ttl_seconds=1800.0,
        max_sessions=8,
        queue_limit=4,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() - 1.0) * 1000),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await anext(stream)

    assert exc_info.value.status_code == 504
    assert exc_info.value.payload["error"]["code"] == "upstream_request_timeout"


@pytest.mark.asyncio
async def test_forwarded_owner_stream_acknowledges_before_dashboard_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    dashboard_lookup_started = asyncio.Event()
    allow_dashboard_lookup = asyncio.Event()
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-owner-ack-close",
        key_id="key-owner-ack-close",
        model="gpt-5.4",
    )
    schedule_release = Mock()

    async def blocked_dashboard_settings():
        dashboard_lookup_started.set()
        await allow_dashboard_lookup.wait()
        raise AssertionError("dashboard lookup must not complete before owner acceptance")

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", blocked_dashboard_settings)
    monkeypatch.setattr(service, "_schedule_websocket_reservation_release", schedule_release)
    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-owner-claim"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    first = await anext(stream)

    assert "codex.bridge_owner.accepted" in first
    assert dashboard_lookup_started.is_set() is False
    await stream.aclose()
    schedule_release.assert_called_once_with(
        reservation,
        reason="http-bridge-startup-interrupted",
    )


@pytest.mark.asyncio
async def test_forwarded_owner_stream_converts_post_acceptance_startup_error_to_terminal_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-owner-startup-error",
        key_id="key-owner-startup-error",
        model="gpt-5.4",
    )
    schedule_release = Mock()

    async def failed_dashboard_settings():
        raise ProxyResponseError(
            503,
            proxy_service.openai_error(
                "bridge_owner_unreachable",
                "connect to 10.0.0.7:5432 failed from /srv/private.sock",
                error_type="server_error",
            ),
        )

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", failed_dashboard_settings)
    monkeypatch.setattr(service, "_schedule_websocket_reservation_release", schedule_release)
    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-owner-claim"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    accepted = await anext(stream)
    terminal = await anext(stream)

    assert "codex.bridge_owner.accepted" in accepted
    assert '"type":"response.failed"' in terminal
    assert '"code":"bridge_owner_unreachable"' in terminal
    assert "10.0.0.7" not in terminal
    assert "/srv/private.sock" not in terminal
    assert "HTTP bridge owner stream failed after acceptance" in terminal
    schedule_release.assert_called_once_with(
        reservation,
        reason="http-bridge-startup-proxy-failure",
    )
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_local_http_bridge_startup_failure_releases_reservation_before_request_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-local-startup-failure",
        key_id="key-local-startup-failure",
        model="gpt-5.4",
    )
    schedule_release = Mock()
    dashboard_settings = SimpleNamespace(
        sticky_threads_enabled=False,
        openai_cache_affinity_max_age_seconds=1800,
        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
        http_responses_session_bridge_gateway_safe_mode=False,
    )

    async def failed_lookup(**_kwargs: Any) -> None:
        raise ProxyResponseError(
            503,
            proxy_service.openai_error("bridge_owner_unreachable", "owner lookup unavailable"),
        )

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", AsyncMock(return_value=dashboard_settings))
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", failed_lookup)
    monkeypatch.setattr(service, "_schedule_websocket_reservation_release", schedule_release)

    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-local-startup-failure"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
    )
    with pytest.raises(ProxyResponseError):
        await anext(stream)

    schedule_release.assert_called_once_with(
        reservation,
        reason="http-bridge-startup-proxy-failure",
    )


@pytest.mark.asyncio
async def test_forwarded_owner_stream_converts_unexpected_post_acceptance_error_to_terminal_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )

    async def failed_dashboard_settings():
        raise RuntimeError("database connection disappeared")

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", failed_dashboard_settings)
    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-owner-claim"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=None,
        suppress_text_done_events=False,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    accepted = await anext(stream)
    terminal = await anext(stream)

    assert "codex.bridge_owner.accepted" in accepted
    assert '"type":"response.failed"' in terminal
    assert '"code":"upstream_error"' in terminal
    assert "database connection disappeared" not in terminal
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_forwarded_owner_stream_sanitizes_post_acceptance_code_and_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )

    async def failed_dashboard_settings():
        raise ProxyResponseError(
            503,
            proxy_service.openai_error(
                "connect-to-10.0.0.7:5432",
                "connect to 10.0.0.7:5432 failed from /srv/private.sock",
                error_type="/srv/private.sock",
            ),
        )

    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", failed_dashboard_settings)
    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-owner-sanitized-fields"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=None,
        suppress_text_done_events=False,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    assert "codex.bridge_owner.accepted" in await anext(stream)
    terminal = await anext(stream)
    assert '"code":"upstream_error"' in terminal
    assert '"type":"server_error"' in terminal
    assert "10.0.0.7" not in terminal
    assert "private.sock" not in terminal


@pytest.mark.asyncio
async def test_http_bridge_detach_transfers_release_to_tracked_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-detach-release", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "detach-release", None),
        account=account,
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-detach-release",
        key_id="key-detach-release",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-detach-release",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    settle_or_release = AsyncMock()
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        settle_or_release,
    )

    removed = await asyncio.wait_for(
        service._detach_http_bridge_request(session, request_state=request_state),
        timeout=0.1,
    )

    assert removed is True
    assert request_state.api_key_reservation is None
    await service.close_proxy_cleanup_tasks()
    settle_or_release.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge request detached before response.completed",
    )


@pytest.mark.asyncio
async def test_http_bridge_unsubmitted_detach_cancellation_keeps_registered_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "detach-cancel-unsubmitted", None),
        account=cast(Any, SimpleNamespace(id="acc-detach-cancel-unsubmitted", status=AccountStatus.ACTIVE)),
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-detach-cancel-unsubmitted",
        key_id="key-detach-cancel-unsubmitted",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-detach-cancel-unsubmitted",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    lock_exit_started = asyncio.Event()
    never_finish_lock_exit = asyncio.Event()

    class CancellationCheckpointOnExit:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            lock_exit_started.set()
            await never_finish_lock_exit.wait()

    session.pending_lock = cast(Any, CancellationCheckpointOnExit())
    settle_or_release = AsyncMock()
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        settle_or_release,
    )

    detach_task = asyncio.create_task(
        service._detach_http_bridge_request(session, request_state=request_state)
    )
    await asyncio.wait_for(lock_exit_started.wait(), timeout=0.1)
    assert request_state.api_key_reservation is None
    detach_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await detach_task
    await service.close_proxy_cleanup_tasks()

    settle_or_release.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge request detached before response.completed",
    )


@pytest.mark.asyncio
async def test_http_bridge_submitted_anonymous_detach_cancellation_keeps_shared_accounting_owner() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "detach-cancel-anonymous", None),
        account=cast(Any, SimpleNamespace(id="acc-detach-cancel-anonymous", status=AccountStatus.ACTIVE)),
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-detach-cancel-anonymous",
        key_id="key-detach-cancel-anonymous",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-detach-cancel-anonymous",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
        http_bridge_send_started_at=time.monotonic(),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    global_lock_entered = asyncio.Event()
    never_acquire_global_lock = asyncio.Event()

    class BlockingGlobalLock:
        async def __aenter__(self) -> None:
            global_lock_entered.set()
            await never_acquire_global_lock.wait()

        async def __aexit__(self, *_args: object) -> None:
            return None

    service._http_bridge_lock = cast(Any, BlockingGlobalLock())
    detach_task = asyncio.create_task(
        service._detach_http_bridge_request(session, request_state=request_state)
    )
    await asyncio.wait_for(global_lock_entered.wait(), timeout=0.1)

    accounting = session.anonymous_discarded_request_accounting[request_state.request_id]
    assert request_state.api_key_reservation is None
    assert accounting.api_key_reservation is reservation
    detach_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await detach_task

    accounting = session.anonymous_discarded_request_accounting[request_state.request_id]
    assert accounting.api_key_reservation is reservation


@pytest.mark.asyncio
async def test_http_bridge_detach_preserves_lifecycle_transferred_reservation() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-lifecycle-transfer-detach-race",
        key_id="key-lifecycle-transfer-detach-race",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-lifecycle-transfer-detach-race",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        response_id="resp-lifecycle-transfer-detach-race",
        event_queue=asyncio.Queue(),
        transport="http",
        http_bridge_send_started_at=time.monotonic(),
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "transfer-detach-race", None),
        account=cast(Any, SimpleNamespace(id="acc-transfer-detach-race", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1

    await service._transfer_submitted_http_bridge_pending_accounting(session)
    transferred = session.discarded_request_accounting[request_state.response_id]
    assert transferred.api_key_reservation is reservation
    assert request_state.api_key_reservation is None

    assert await service._detach_http_bridge_request(session, request_state=request_state)

    preserved = session.discarded_request_accounting[request_state.response_id]
    assert preserved is transferred
    assert preserved.api_key_reservation is reservation


@pytest.mark.asyncio
async def test_http_bridge_detach_after_reconciliation_seal_uses_local_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-detach-after-reconciliation-seal",
        key_id="key-detach-after-reconciliation-seal",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-detach-after-reconciliation-seal",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        response_id="resp-detach-after-reconciliation-seal",
        event_queue=asyncio.Queue(),
        transport="http",
        http_bridge_send_started_at=time.monotonic(),
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "detach-after-seal", None),
        account=cast(Any, SimpleNamespace(id="acc-detach-after-seal", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    session.discarded_accounting_reconciliation_owned = True
    settle_or_release = AsyncMock()
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        settle_or_release,
    )

    assert await service._detach_http_bridge_request(session, request_state=request_state)
    await service.close_proxy_cleanup_tasks()

    assert session.discarded_request_accounting == {}
    settle_or_release.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge request detached before response.completed",
    )


@pytest.mark.asyncio
async def test_http_bridge_detach_hard_bounds_blocked_lifecycle_lock_and_transfers_reservation_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "detach-hard-timeout", None),
        account=cast(Any, SimpleNamespace(id="acc-detach-hard-timeout", status=AccountStatus.ACTIVE)),
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-detach-hard-timeout",
        key_id="key-detach-hard-timeout",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-detach-hard-timeout",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    settle_or_release = AsyncMock()
    monkeypatch.setattr(bridge_request_submit, "_HTTP_BRIDGE_DETACH_OBSERVATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        settle_or_release,
    )
    await session.lifecycle_lock.acquire()

    started_at = time.monotonic()
    removed = await service._detach_http_bridge_request(session, request_state=request_state)

    assert removed is False
    assert time.monotonic() - started_at < 0.2
    assert request_state.api_key_reservation is None
    assert request_state.event_queue is None
    await service.close_proxy_cleanup_tasks()
    settle_or_release.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge request detached before response.completed",
    )
    session.lifecycle_lock.release()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_idle_pruning_detaches_reader_before_close_without_holding_global_lock() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-prune-terminal-lock", status=AccountStatus.ACTIVE))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "prune-terminal-lock", None)
    session = _make_http_bridge_session(key=key, account=account)
    session.last_used_at = time.monotonic() - 10.0
    session.idle_ttl_seconds = 0.01
    reader_started = asyncio.Event()
    reader_terminal_cleanup_finished = asyncio.Event()
    upstream_close = cast(AsyncMock, session.upstream.close)

    async def cancellation_shielded_terminal_delivery() -> None:
        reader_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert upstream_close.await_count == 0
            await service._register_http_bridge_previous_response_id(session, "resp-prune-terminal-lock")
            reader_terminal_cleanup_finished.set()
            raise

    reader = asyncio.create_task(cancellation_shielded_terminal_delivery())
    session.upstream_reader = reader
    service._http_bridge_sessions[key] = session
    await reader_started.wait()

    async with service._http_bridge_lock:
        stale_sessions = await asyncio.wait_for(
            service._prune_http_bridge_sessions_locked(),
            timeout=0.2,
        )
        assert reader.cancelled() is False
        assert reader.done() is False

    assert stale_sessions == [session]
    assert key not in service._http_bridge_sessions
    await service._detach_http_bridge_session_for_background_close(session)
    await asyncio.wait_for(service._close_http_bridge_session(session), timeout=0.5)

    assert reader_terminal_cleanup_finished.is_set()
    assert reader.cancelled()
    upstream_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_pruning_atomically_detaches_aliases_before_same_key_replacement() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-alias-aba", status=AccountStatus.ACTIVE))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "alias-aba", None)
    stale = _make_http_bridge_session(key=key, account=account)
    stale.last_used_at = time.monotonic() - 10.0
    stale.idle_ttl_seconds = 0.01
    stale.downstream_turn_state_aliases.add("turn-alias-aba")
    stale.previous_response_ids.add("resp-alias-aba")
    turn_alias_key = proxy_service._http_bridge_turn_state_alias_key("turn-alias-aba", None)
    response_alias_key = proxy_service._http_bridge_previous_response_alias_key("resp-alias-aba", None)
    service._http_bridge_sessions[key] = stale
    service._http_bridge_turn_state_index[turn_alias_key] = key
    service._http_bridge_previous_response_index[response_alias_key] = key

    async with service._http_bridge_lock:
        stale_sessions = await service._prune_http_bridge_sessions_locked()
        assert stale_sessions == [stale]
        assert turn_alias_key not in service._http_bridge_turn_state_index
        assert response_alias_key not in service._http_bridge_previous_response_index

        replacement = _make_http_bridge_session(key=key, account=account)
        replacement.downstream_turn_state_aliases.add("turn-alias-aba")
        replacement.previous_response_ids.add("resp-alias-aba")
        service._http_bridge_sessions[key] = replacement
        service._http_bridge_turn_state_index[turn_alias_key] = key
        service._http_bridge_previous_response_index[response_alias_key] = key

    # A delayed close owner from the retired session cannot delete aliases
    # installed by a same-key successor.
    await service._detach_http_bridge_session_for_background_close(stale)

    assert service._http_bridge_sessions[key] is replacement
    assert service._http_bridge_turn_state_index[turn_alias_key] == key
    assert service._http_bridge_previous_response_index[response_alias_key] == key


@pytest.mark.asyncio
async def test_pressure_capacity_database_wait_inherits_original_bridge_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    cancellation_seen = asyncio.Event()
    allow_late_resolution = asyncio.Event()
    owner_resolution = AsyncMock()

    async def cancellation_suppressing_pressure_resolution(**_kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_resolution.wait()

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: settings)
    monkeypatch.setattr(service, "_http_bridge_should_wait_for_registration_compatible", AsyncMock(return_value=False))
    monkeypatch.setattr(
        service,
        "_resolve_http_bridge_pressure_capacity_hint",
        cancellation_suppressing_pressure_resolution,
    )
    monkeypatch.setattr(service, "_resolve_http_bridge_owner", owner_resolution)

    started_at = time.monotonic()
    with pytest.raises(ProxyResponseError):
        await service._get_or_create_http_bridge_session(
            proxy_service._HTTPBridgeSessionKey("session_header", "pressure-resolution-hard-timeout", None),
            headers={"x-codex-session-id": "pressure-resolution-hard-timeout"},
            affinity=proxy_service._AffinityPolicy(
                key="pressure-resolution-hard-timeout",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=0,
            request_deadline_at=time.monotonic() + 0.05,
        )

    assert time.monotonic() - started_at < 0.2
    await asyncio.sleep(0)
    assert cancellation_seen.is_set()
    owner_resolution.assert_not_awaited()
    allow_late_resolution.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_submit_time_pressure_capacity_resolution_has_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    cancellation_seen = asyncio.Event()
    allow_late_resolution = asyncio.Event()

    async def cancellation_suppressing_pressure_resolution(**_kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_resolution.wait()

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: settings)
    monkeypatch.setattr(
        service,
        "_resolve_http_bridge_pressure_capacity_hint",
        cancellation_suppressing_pressure_resolution,
    )

    started_at = time.monotonic()
    with pytest.raises(ProxyResponseError) as exc_info:
        await service._evict_http_bridge_pressure(
            max_sessions=0,
            protected_key=proxy_service._HTTPBridgeSessionKey(
                "session_header",
                "submit-pressure-resolution-hard-timeout",
                None,
            ),
            request_model="gpt-5.4",
            request_deadline_at=time.monotonic() + 0.05,
            api_key=None,
        )

    assert time.monotonic() - started_at < 0.2
    assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
    await asyncio.sleep(0)
    assert cancellation_seen.is_set()
    allow_late_resolution.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_owner_resolution_database_wait_never_holds_global_bridge_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    cancellation_seen = asyncio.Event()
    allow_late_resolution = asyncio.Event()

    async def cancellation_suppressing_resolution(**_kwargs: Any) -> SimpleNamespace:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_resolution.wait()
        return SimpleNamespace(owner_check_required=False, owner_forward=None, force_durable_takeover=False)

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: settings)
    monkeypatch.setattr(service, "_http_bridge_should_wait_for_registration_compatible", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_resolve_http_bridge_owner", cancellation_suppressing_resolution)
    monkeypatch.setattr(
        service,
        "_resolve_http_bridge_pressure_capacity_hint",
        AsyncMock(return_value=SimpleNamespace(effective_max_sessions=8, account_headroom=None)),
    )

    started_at = time.monotonic()
    with pytest.raises(ProxyResponseError):
        await service._get_or_create_http_bridge_session(
            proxy_service._HTTPBridgeSessionKey("session_header", "owner-resolution-hard-timeout", None),
            headers={"x-codex-session-id": "owner-resolution-hard-timeout"},
            affinity=proxy_service._AffinityPolicy(
                key="owner-resolution-hard-timeout",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=8,
            request_deadline_at=time.monotonic() + 0.01,
        )

    assert time.monotonic() - started_at < 0.2
    await asyncio.sleep(0)
    assert cancellation_seen.is_set()
    with anyio.fail_after(0.1):
        async with service._http_bridge_lock:
            pass
    allow_late_resolution.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_scheduled_stale_close_survives_caller_cancellation_during_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-stale-close-cancel", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "stale-close-cancel", None),
        account=account,
    )
    detach_started = asyncio.Event()
    allow_detach = asyncio.Event()
    close_session = AsyncMock()

    async def blocked_detach(_session: proxy_service._HTTPBridgeSession) -> None:
        assert _session is session
        detach_started.set()
        await allow_detach.wait()

    monkeypatch.setattr(service, "_detach_http_bridge_session_for_background_close", blocked_detach)
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)

    async def cancelling_caller() -> None:
        service._schedule_http_bridge_session_close(session, reason="cancellation-regression")
        await asyncio.Event().wait()

    caller = asyncio.create_task(cancelling_caller())
    await detach_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    allow_detach.set()
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))

    close_session.assert_awaited_once_with(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("removal_path", ["upstream_disconnect", "local_terminal_reset"])
async def test_registry_removal_enrolls_blocked_bridge_close_before_return(
    monkeypatch: pytest.MonkeyPatch,
    removal_path: str,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"tracked-{removal_path}", None)
    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id=f"acc-{removal_path}", status=AccountStatus.ACTIVE)),
    )
    session.downstream_turn_state_aliases.add("turn-tracked-removal")
    session.previous_response_ids.add("resp-tracked-removal")
    service._http_bridge_sessions[key] = session
    service._http_bridge_turn_state_index[
        proxy_service._http_bridge_turn_state_alias_key("turn-tracked-removal", key.api_key_id)
    ] = key
    service._http_bridge_previous_response_index[
        proxy_service._http_bridge_previous_response_alias_key("resp-tracked-removal", key.api_key_id)
    ] = key
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def blocked_close(*args: object, **kwargs: object) -> None:
        del args, kwargs
        close_started.set()
        await allow_close.wait()

    monkeypatch.setattr(service, "_close_http_bridge_session", blocked_close)
    if removal_path == "upstream_disconnect":
        await service._evict_http_bridge_session_after_upstream_disconnect(
            session,
            error_message="Upstream websocket disconnected",
        )
    else:
        await service._reset_http_bridge_session_after_local_terminal_error(
            session,
            error_code="stream_incomplete",
            error_message="Upstream websocket disconnected",
            request_deadline_at=time.monotonic() + 1.0,
        )

    await asyncio.wait_for(close_started.wait(), timeout=0.1)
    assert key not in service._http_bridge_sessions
    assert not service._http_bridge_turn_state_index
    assert not service._http_bridge_previous_response_index
    assert service._http_bridge_background_close_tasks
    allow_close.set()
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))


@pytest.mark.asyncio
async def test_stale_detach_cancellation_clears_reserved_inflight_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "stale-inflight-cancel", None)
    account = cast(Any, SimpleNamespace(id="acc-stale-inflight-cancel", status=AccountStatus.ACTIVE))
    stale_session = _make_http_bridge_session(key=key, account=account)
    stale_session.last_used_at = time.monotonic() - stale_session.idle_ttl_seconds - 1.0
    service._http_bridge_sessions[key] = stale_session
    detach_started = asyncio.Event()
    allow_detach = asyncio.Event()
    close_session = AsyncMock()
    create_session = AsyncMock()

    async def blocked_detach(_session: proxy_service._HTTPBridgeSession) -> None:
        assert _session is stale_session
        detach_started.set()
        await allow_detach.wait()

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: settings)
    monkeypatch.setattr(service, "_detach_http_bridge_session_for_background_close", blocked_detach)
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", create_session)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        proxy_service,
        "_http_bridge_owner_instance",
        AsyncMock(return_value=settings.http_responses_session_bridge_instance_id),
    )
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(
            return_value=(
                settings.http_responses_session_bridge_instance_id,
                [settings.http_responses_session_bridge_instance_id],
            )
        ),
    )

    caller = asyncio.create_task(
        service._get_or_create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key=key.affinity_key),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=8,
        )
    )
    await detach_started.wait()
    inflight_future = service._http_bridge_inflight_sessions[key]

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert key not in service._http_bridge_inflight_sessions
    assert inflight_future.cancelled()
    create_session.assert_not_awaited()
    allow_detach.set()
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))
    close_session.assert_awaited_once_with(stale_session)


@pytest.mark.asyncio
async def test_disabled_bridge_fallback_uses_direct_stream_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings(bridge_enabled=False)
    dashboard_settings = SimpleNamespace(
        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=120,
        http_responses_session_bridge_gateway_safe_mode=False,
    )
    captured: dict[str, object] = {}

    async def fake_stream_with_retry(*args: object, **kwargs: object):
        del args
        captured.update(kwargs)
        yield 'data: {"type":"response.completed","response":{"id":"resp-fallback"}}\n\n'

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: settings)
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", AsyncMock(return_value=dashboard_settings))
    monkeypatch.setattr(service, "_stream_with_retry", fake_stream_with_retry)
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )

    chunks = [
        chunk
        async for chunk in service._stream_http_bridge_or_retry(
            payload,
            headers={},
            codex_session_affinity=False,
            propagate_http_errors=True,
            openai_cache_affinity=False,
            api_key=None,
            api_key_reservation=None,
            suppress_text_done_events=False,
        )
    ]

    request_started_at = cast(float, captured["request_started_at"])
    request_deadline_at = cast(float, captured["request_deadline_at"])
    assert chunks
    assert request_deadline_at - request_started_at == pytest.approx(
        settings.http_responses_stream_request_budget_seconds
    )


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
            request_deadline_at=time.monotonic() + 7200.0,
            proxy_api_authorization=None,
        )
    ]

    assert chunks == ["data: first\n\n", "data: terminal\n\n"]


@pytest.mark.asyncio
async def test_forward_http_bridge_request_to_owner_releases_reservation_on_definitive_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-definitive", None),
    )
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-definitive",
        key_id="key-definitive",
        model="gpt-5.4",
    )

    async def reject_before_acceptance(**kwargs: object):
        del kwargs
        raise ProxyResponseError(
            503,
            proxy_service.openai_error("bridge_owner_unreachable", "owner rejected request"),
        )
        yield ""

    release_reservation = AsyncMock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_http_bridge_owner_client",
        cast(Any, SimpleNamespace(stream_responses=reject_before_acceptance)),
    )
    monkeypatch.setattr(
        service,
        "_release_unclaimed_websocket_reservation",
        release_reservation,
    )

    with pytest.raises(ProxyResponseError):
        async for _ in service._forward_http_bridge_request_to_owner(
            owner_forward=owner_forward,
            payload=payload,
            headers={},
            api_key_reservation=reservation,
            codex_session_affinity=True,
            downstream_turn_state="http_turn_definitive",
            request_started_at=time.monotonic(),
            request_deadline_at=time.monotonic() + 60.0,
            proxy_api_authorization=None,
        ):
            pass

    await service.close_proxy_cleanup_tasks()
    release_reservation.assert_awaited_once_with(reservation)


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_forward_http_bridge_request_to_owner_suppresses_replay_when_handoff_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner_forward = proxy_service._HTTPBridgeOwnerForward(
        owner_instance="instance-b",
        owner_endpoint="http://instance-b",
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-ambiguous", None),
    )
    payload = proxy_service.ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-ambiguous",
        key_id="key-ambiguous",
        model="gpt-5.4",
    )

    async def disconnect_around_acceptance(**kwargs: object):
        del kwargs
        if accepted:
            yield 'data: {"type":"codex.bridge_owner.accepted"}\n\n'
        raise aiohttp.ClientPayloadError("owner connection dropped")

    release_reservation = AsyncMock()
    release_unclaimed_reservation = AsyncMock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_http_bridge_owner_client",
        cast(Any, SimpleNamespace(stream_responses=disconnect_around_acceptance)),
    )
    monkeypatch.setattr(service, "_release_websocket_reservation", release_reservation)
    monkeypatch.setattr(
        service,
        "_release_unclaimed_websocket_reservation",
        release_unclaimed_reservation,
    )

    chunks = [
        chunk
        async for chunk in service._forward_http_bridge_request_to_owner(
            owner_forward=owner_forward,
            payload=payload,
            headers={},
            api_key_reservation=reservation,
            codex_session_affinity=True,
            downstream_turn_state="http_turn_ambiguous",
            request_started_at=time.monotonic(),
            request_deadline_at=time.monotonic() + 60.0,
            proxy_api_authorization=None,
        )
    ]

    assert len(chunks) == 1
    assert '"type":"response.failed"' in chunks[0]
    assert "local replay was suppressed" in chunks[0] if not accepted else "after request acceptance" in chunks[0]
    assert "codex.bridge_owner.accepted" not in chunks[0]
    await service.close_proxy_cleanup_tasks()
    release_reservation.assert_not_awaited()
    if accepted:
        release_unclaimed_reservation.assert_not_awaited()
    else:
        release_unclaimed_reservation.assert_awaited_once_with(reservation)


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

    initial_started_at = time.monotonic()
    request_state_initial = proxy_service._WebSocketRequestState(
        request_id="req-initial",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=initial_reservation,
        started_at=initial_started_at,
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
        started_at=initial_started_at + 10.0,
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
    streamed_request_states: list[proxy_service._WebSocketRequestState] = []

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
        del text_data, queue_limit, propagate_http_errors, downstream_turn_state, submit_lease_held
        streamed_request_states.append(request_state)
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
    assert streamed_request_states == [request_state_initial, request_state_retry]
    assert request_state_initial.request_deadline_at == pytest.approx(initial_started_at + 7200.0)
    assert request_state_retry.started_at == request_state_initial.started_at
    assert request_state_retry.request_budget_seconds == 7200.0
    assert request_state_retry.request_deadline_at == request_state_initial.request_deadline_at


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

    initial_started_at = time.monotonic()
    request_state_initial = proxy_service._WebSocketRequestState(
        request_id="req-initial",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=initial_reservation,
        started_at=initial_started_at,
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
        started_at=initial_started_at + 10.0,
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
    submitted_request_states: list[proxy_service._WebSocketRequestState] = []

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
        submitted_request_states.append(request_state)
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
    assert submitted_request_states == [request_state_retry]
    assert request_state_initial.request_deadline_at == pytest.approx(initial_started_at + 7200.0)
    assert request_state_retry.started_at == request_state_initial.started_at
    assert request_state_retry.request_budget_seconds == 7200.0
    assert request_state_retry.request_deadline_at == request_state_initial.request_deadline_at


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
        started_at=time.monotonic(),
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
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))

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
        started_at=time.monotonic(),
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
        started_at=time.monotonic(),
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
            started_at=time.monotonic(),
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
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))
    close_session.assert_awaited_once_with(existing_session)


@pytest.mark.asyncio
async def test_get_or_create_retires_same_instance_older_epoch_before_reacquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    current_instance = settings.http_responses_session_bridge_instance_id
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-same-instance-aba", None)
    stale_send = AsyncMock()
    stale_session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-stale-epoch", status=AccountStatus.ACTIVE)),
    )
    stale_session.headers = {"x-codex-session-id": key.affinity_key}
    stale_session.affinity = proxy_service._AffinityPolicy(
        key=key.affinity_key,
        kind=proxy_service.StickySessionKind.CODEX_SESSION,
    )
    stale_session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(send_text=stale_send, close=AsyncMock()),
    )
    stale_session.durable_session_id = "durable-same-instance-aba"
    stale_session.durable_owner_epoch = 1
    stale_session.durable_lease_expires_at = proxy_service.utcnow() + timedelta(seconds=60)
    replacement = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-replacement-epoch", status=AccountStatus.ACTIVE)),
    )
    replacement.headers = {"x-codex-session-id": key.affinity_key}
    replacement.affinity = stale_session.affinity
    service._http_bridge_sessions[key] = stale_session
    scheduled_close = Mock()

    def schedule_close(*_args: object, **_kwargs: object) -> asyncio.Future[None]:
        scheduled_close(*_args, **_kwargs)
        detached = asyncio.get_running_loop().create_future()
        detached.set_result(None)
        return detached

    async def claim_replacement(
        session: proxy_service._HTTPBridgeSession,
        *,
        allow_takeover: bool,
    ) -> None:
        assert session is replacement
        assert allow_takeover is False
        session.durable_session_id = "durable-same-instance-aba"
        session.durable_owner_epoch = 3
        session.durable_lease_expires_at = proxy_service.utcnow() + timedelta(seconds=60)

    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_evict_http_bridge_parallel_prompt_cache_pressure_locked",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(service, "_create_http_bridge_session", AsyncMock(return_value=replacement))
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", claim_replacement)
    monkeypatch.setattr(service, "_schedule_http_bridge_session_close", schedule_close)
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value=current_instance))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=(current_instance, (current_instance,))),
    )

    acquired = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-session-id": key.affinity_key},
        affinity=stale_session.affinity,
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-same-instance-aba",
            canonical_kind=key.affinity_kind,
            canonical_key=key.affinity_key,
            api_key_scope="__anonymous__",
            account_id=stale_session.account.id,
            owner_instance_id=current_instance,
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state=None,
            latest_response_id=None,
        ),
    )

    assert acquired is replacement
    assert replacement.submit_lease_count == 1
    assert service._http_bridge_sessions[key] is replacement
    assert stale_session.closed is True
    assert stale_session.durable_ownership_lost is True
    assert stale_session.durable_ownership_retirement_scheduled is True
    assert stale_session.durable_lease_expires_at is None
    scheduled_close.assert_called_once_with(stale_session, reason="get_or_create_stale")
    with pytest.raises(ProxyResponseError) as exc_info:
        await service._fence_durable_http_bridge_session_before_submit(stale_session)
    assert exc_info.value.status_code == 409
    assert exc_info.value.payload["error"]["code"] == "bridge_instance_mismatch"
    stale_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_fences_when_lookup_predates_newer_local_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    current_instance = settings.http_responses_session_bridge_instance_id
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-local-newer-epoch", None)
    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-local-newer-epoch", status=AccountStatus.ACTIVE)),
    )
    session.headers = {"x-codex-session-id": key.affinity_key}
    session.affinity = proxy_service._AffinityPolicy(
        key=key.affinity_key,
        kind=proxy_service.StickySessionKind.CODEX_SESSION,
    )
    session.durable_session_id = "durable-local-newer-epoch"
    session.durable_owner_epoch = 3
    session.durable_lease_expires_at = proxy_service.utcnow() + timedelta(seconds=60)
    service._http_bridge_sessions[key] = session
    scheduled_refresh = Mock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_evict_http_bridge_parallel_prompt_cache_pressure_locked",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(service, "_schedule_durable_http_bridge_session_refresh", scheduled_refresh)
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value=current_instance))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=(current_instance, (current_instance,))),
    )

    acquired = await service._get_or_create_http_bridge_session(
        key,
        headers={"x-codex-session-id": key.affinity_key},
        affinity=session.affinity,
        api_key=None,
        request_model="gpt-5.4",
        idle_ttl_seconds=120.0,
        max_sessions=8,
        durable_lookup=proxy_service.DurableBridgeLookup(
            session_id="durable-local-newer-epoch",
            canonical_kind=key.affinity_kind,
            canonical_key=key.affinity_key,
            api_key_scope="__anonymous__",
            account_id=session.account.id,
            owner_instance_id=current_instance,
            owner_epoch=2,
            lease_expires_at=proxy_service.utcnow() + timedelta(seconds=60),
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state=None,
            latest_response_id=None,
        ),
    )

    assert acquired is session
    assert session.durable_lease_expires_at is None
    scheduled_refresh.assert_not_called()
    renewed_expiry = proxy_service.utcnow() + timedelta(seconds=60)
    renew_live_session = AsyncMock(
        return_value=proxy_service.DurableBridgeLookup(
            session_id="durable-local-newer-epoch",
            canonical_kind=key.affinity_kind,
            canonical_key=key.affinity_key,
            api_key_scope="__anonymous__",
            account_id=session.account.id,
            owner_instance_id=current_instance,
            owner_epoch=3,
            lease_expires_at=renewed_expiry,
            state=HttpBridgeSessionState.ACTIVE,
            latest_turn_state=None,
            latest_response_id=None,
        )
    )
    monkeypatch.setattr(service._durable_bridge, "renew_live_session", renew_live_session)

    await service._fence_durable_http_bridge_session_before_submit(session)

    renew_live_session.assert_awaited_once()
    assert renew_live_session.await_args.kwargs["owner_epoch"] == 3
    assert session.durable_lease_expires_at == renewed_expiry
    assert session.closed is False
    await service._release_http_bridge_submit_lease(session)


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
async def test_mark_http_bridge_draining_hard_bounds_cancellation_suppressing_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    cancellation_seen = asyncio.Event()
    allow_late_mark = asyncio.Event()

    async def cancellation_suppressing_mark(**_kwargs: object) -> int:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_mark.wait()
        return 1

    monkeypatch.setattr(
        bridge_lifecycle,
        "_HTTP_BRIDGE_SHUTDOWN_MARK_DRAINING_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(service._durable_bridge, "mark_instance_draining", cancellation_suppressing_mark)

    started_at = time.monotonic()
    await service.mark_http_bridge_draining()

    assert time.monotonic() - started_at < 0.2
    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    assert service._proxy_cleanup_tasks
    allow_late_mark.set()
    await service.close_proxy_cleanup_tasks()
    assert not service._proxy_cleanup_tasks


@pytest.mark.asyncio
async def test_close_all_http_bridge_sessions_hard_bounds_cancellation_suppressing_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-shutdown-hard-timeout", None)
    account = cast(Any, SimpleNamespace(id="acc-shutdown-hard-timeout", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(key=key, account=account)
    close_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    allow_close = asyncio.Event()

    async def cancellation_suppressing_close() -> None:
        close_started.set()
        try:
            await allow_close.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_close.wait()

    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(close=cancellation_suppressing_close),
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(bridge_lifecycle, "_HTTP_BRIDGE_SHUTDOWN_CLOSE_TIMEOUT_SECONDS", 0.01)

    started_at = time.monotonic()
    await service.close_all_http_bridge_sessions()

    assert time.monotonic() - started_at < 0.2
    assert close_started.is_set()
    assert cancellation_seen.is_set()
    assert service._http_bridge_background_close_tasks
    allow_close.set()
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))


@pytest.mark.asyncio
async def test_bridge_shutdown_enrolls_pending_persistence_before_reader_timeout_and_cleanup_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-shutdown-persistence-order", None)
    account = cast(Any, SimpleNamespace(id="acc-shutdown-persistence-order", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(key=key, account=account)
    session.pending_requests.append(
        proxy_service._WebSocketRequestState(
            request_id="req-shutdown-persistence-order",
            model="gpt-5.4",
            service_tier=None,
            reasoning_effort=None,
            api_key_reservation=None,
            started_at=time.monotonic(),
            event_queue=asyncio.Queue(),
            transport="http",
        )
    )
    session.queued_request_count = 1
    allow_reader_exit = asyncio.Event()
    reader_cancelled = asyncio.Event()
    persistence_enrolled = asyncio.Event()
    persistence_finished = asyncio.Event()
    database_closed = False

    async def cancellation_suppressing_reader() -> None:
        try:
            await allow_reader_exit.wait()
        except asyncio.CancelledError:
            reader_cancelled.set()
            await allow_reader_exit.wait()

    async def fail_pending_requests(**kwargs: Any) -> None:
        pending_requests = kwargs["pending_requests"]
        if not pending_requests:
            return
        pending_requests.clear()

        async def persist() -> None:
            await asyncio.sleep(0)
            assert database_closed is False
            persistence_finished.set()

        task = asyncio.create_task(persist())
        service._proxy_cleanup_tasks.add(task)
        task.add_done_callback(service._proxy_cleanup_tasks.discard)
        persistence_enrolled.set()

    session.upstream_reader = asyncio.create_task(cancellation_suppressing_reader())
    session.upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(service, "_fail_pending_websocket_requests", fail_pending_requests)
    monkeypatch.setattr(bridge_lifecycle, "_HTTP_BRIDGE_SHUTDOWN_CLOSE_TIMEOUT_SECONDS", 0.01)

    await service.close_all_http_bridge_sessions()

    assert persistence_enrolled.is_set()
    assert reader_cancelled.is_set()
    await service.close_proxy_cleanup_tasks()
    assert persistence_finished.is_set()
    assert not service._proxy_cleanup_tasks
    database_closed = True
    allow_reader_exit.set()
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks), return_exceptions=True)
    assert not service._proxy_cleanup_tasks


@pytest.mark.asyncio
async def test_close_all_http_bridge_sessions_closes_socket_after_durable_release_accepts_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-shutdown-durable-cancel", None)
    account = cast(Any, SimpleNamespace(id="acc-shutdown-durable-cancel", status=AccountStatus.ACTIVE))
    upstream_close = AsyncMock()
    session = _make_http_bridge_session(key=key, account=account)
    session.upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=upstream_close))
    session.durable_session_id = "durable-shutdown-cancel"
    session.durable_owner_epoch = 1
    service._http_bridge_sessions[key] = session
    durable_release_started = asyncio.Event()
    durable_release_cancelled = asyncio.Event()
    release_calls = 0

    async def cancellation_accepting_release(_session: object) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls > 1:
            return
        durable_release_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            durable_release_cancelled.set()
            raise

    monkeypatch.setattr(service, "_release_durable_http_bridge_session_ownership", cancellation_accepting_release)
    monkeypatch.setattr(bridge_lifecycle, "_HTTP_BRIDGE_SHUTDOWN_CLOSE_TIMEOUT_SECONDS", 0.01)

    await service.close_all_http_bridge_sessions()

    assert durable_release_started.is_set()
    assert durable_release_cancelled.is_set()
    upstream_close.assert_awaited_once()
    assert not service._http_bridge_background_close_tasks


@pytest.mark.asyncio
async def test_background_bridge_close_reaches_socket_when_pending_failure_accepts_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "pending-failure-cancel", None),
        account=cast(Any, SimpleNamespace(id="acc-pending-failure-cancel", status=AccountStatus.ACTIVE)),
    )
    upstream_close = AsyncMock()
    session.upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=upstream_close))
    failure_started = asyncio.Event()
    failure_cancelled = asyncio.Event()
    failure_calls = 0

    async def cancellation_accepting_failure(**_kwargs: object) -> None:
        nonlocal failure_calls
        failure_calls += 1
        if failure_calls > 1:
            return
        failure_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            failure_cancelled.set()
            raise

    monkeypatch.setattr(service, "_fail_pending_websocket_requests", cancellation_accepting_failure)
    detached = service._schedule_http_bridge_session_close(
        session,
        reason="test-pending-failure-cancel",
        error_code="stream_incomplete",
        error_message="test failure",
    )
    await asyncio.wait_for(failure_started.wait(), timeout=0.1)
    close_task = next(iter(service._http_bridge_background_close_tasks))
    close_task.cancel()
    await asyncio.gather(close_task, return_exceptions=True)

    assert failure_cancelled.is_set()
    assert detached.done()
    upstream_close.assert_awaited_once()


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
    capacity_wait_started = asyncio.Event()
    reserve_creation_slot = service._reserve_http_bridge_creation_slot_locked

    async def reserve_creation_slot_with_signal(**kwargs: Any) -> Any:
        reservation = await reserve_creation_slot(**kwargs)
        if reservation.capacity_wait_future is not None:
            capacity_wait_started.set()
        return reservation

    monkeypatch.setattr(service, "_reserve_http_bridge_creation_slot_locked", reserve_creation_slot_with_signal)

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
    await asyncio.wait_for(capacity_wait_started.wait(), timeout=0.2)

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
        started_at=time.monotonic(),
        previous_response_id="resp_prev_1",
        transport="http",
    )
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: SimpleNamespace(http_responses_session_bridge_codex_prewarm_enabled=True),
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

    await service._maybe_prewarm_http_bridge_session(
        session,
        request_state=request_state,
        text_data='{"model":"gpt-5.4","input":"hello"}',
    )

    assert session.prewarmed is False
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_http_bridge_request_rejects_expired_state_before_send_and_releases_submit_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    send_text = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "sid-expired-submit", None),
        headers={"x-codex-session-id": "sid-expired-submit"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-expired-submit",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-expired-submit", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
        submit_lease_count=1,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-expired-submit",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic() - 7201.0,
        request_budget_seconds=7200.0,
        request_deadline_at=time.monotonic() - 1.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    release_submit_lease = AsyncMock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_release_http_bridge_submit_lease", release_submit_lease)

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request(
            session,
            request_state=request_state,
            text_data='{"type":"response.create","input":"hello"}',
            queue_limit=0,
        )

    assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
    release_submit_lease.assert_awaited_once_with(session)
    send_text.assert_not_awaited()
    assert session.queued_request_count == 0
    assert not session.pending_requests


@pytest.mark.asyncio
async def test_submit_expired_durable_lease_fails_closed_when_renew_cas_loses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    settings = _make_app_settings()
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-expired-durable", None)
    send_text = AsyncMock()
    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-expired-durable", status=AccountStatus.ACTIVE)),
    )
    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(send_text=send_text, close=AsyncMock()),
    )
    session.submit_lease_count = 1
    session.durable_session_id = "durable-expired"
    session.durable_owner_epoch = 7
    session.durable_lease_expires_at = proxy_service.utcnow() - timedelta(seconds=1)
    service._http_bridge_sessions[key] = session
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-expired-durable",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    renew_live_session = AsyncMock(return_value=None)
    release_submit_lease = AsyncMock()
    detached = asyncio.get_running_loop().create_future()
    detached.set_result(None)
    schedule_close = Mock(return_value=detached)
    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: settings)
    monkeypatch.setattr(service._durable_bridge, "renew_live_session", renew_live_session)
    monkeypatch.setattr(service, "_release_http_bridge_submit_lease", release_submit_lease)
    monkeypatch.setattr(service, "_schedule_http_bridge_session_close", schedule_close)

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request(
            session,
            request_state=request_state,
            text_data='{"type":"response.create","input":"hello"}',
            queue_limit=4,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.payload["error"]["code"] == "bridge_instance_mismatch"
    renew_live_session.assert_awaited_once()
    assert renew_live_session.await_args.kwargs["session_id"] == "durable-expired"
    assert renew_live_session.await_args.kwargs["owner_epoch"] == 7
    assert renew_live_session.await_args.kwargs["instance_id"] == settings.http_responses_session_bridge_instance_id
    release_submit_lease.assert_awaited_once_with(session)
    schedule_close.assert_called_once_with(
        session,
        reason="durable-submit-fence-lost",
        error_code="stream_incomplete",
        error_message="HTTP bridge durable ownership was lost before submission",
    )
    send_text.assert_not_awaited()
    assert session.closed is True
    assert session.durable_ownership_lost is True
    assert key not in service._http_bridge_sessions
    assert session.queued_request_count == 0
    assert not session.pending_requests


@pytest.mark.asyncio
async def test_submit_http_bridge_request_rechecks_deadline_after_admission_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "sid-admission-deadline", None)
    send_text = AsyncMock()
    sibling_state = proxy_service._WebSocketRequestState(
        request_id="req-admission-sibling",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={"x-codex-session-id": "sid-admission-deadline"},
        affinity=proxy_service._AffinityPolicy(
            key="sid-admission-deadline",
            kind=proxy_service.StickySessionKind.CODEX_SESSION,
        ),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-admission-deadline", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([sibling_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
        submit_lease_count=1,
    )
    service._http_bridge_sessions[key] = session
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-admission-deadline",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_budget_seconds=7200.0,
        request_deadline_at=time.monotonic() + 60.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )

    async def expire_during_admission(
        state: proxy_service._WebSocketRequestState,
        *,
        response_create_gate: asyncio.Semaphore | None,
        compact: bool = False,
    ) -> None:
        del response_create_gate, compact
        state.request_deadline_at = time.monotonic() - 1.0

    release_submit_lease = AsyncMock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_acquire_request_state_response_create_admission", expire_during_admission)
    monkeypatch.setattr(service, "_release_http_bridge_submit_lease", release_submit_lease)

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request(
            session,
            request_state=request_state,
            text_data='{"type":"response.create","input":"hello"}',
            queue_limit=4,
        )

    assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
    send_text.assert_not_awaited()
    release_submit_lease.assert_awaited_once_with(session)
    assert session.queued_request_count == 1
    assert list(session.pending_requests) == [sibling_state]
    assert request_state.account_model_concurrency is None


@pytest.mark.asyncio
async def test_http_bridge_retry_does_not_mutate_transport_when_sibling_is_pending() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    close_upstream = AsyncMock()
    current_request = proxy_service._WebSocketRequestState(
        request_id="req-retry-current",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        response_id="resp-retry-current",
        awaiting_response_created=False,
        request_text='{"type":"response.create","input":"hello"}',
        event_queue=asyncio.Queue(),
        transport="http",
    )
    sibling_request = proxy_service._WebSocketRequestState(
        request_id="req-retry-sibling",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="resp-retry-sibling",
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "retry-sibling", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="retry-sibling"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-retry-sibling", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=close_upstream)),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([current_request, sibling_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=2,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session,
        request_state=current_request,
        text_data=current_request.request_text or "",
        reset_response_state=True,
        prefer_same_account=False,
    )

    assert recovered is False
    assert current_request.response_id == "resp-retry-current"
    assert current_request.awaiting_response_created is False
    assert list(session.pending_requests) == [current_request, sibling_request]
    close_upstream.assert_not_awaited()


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
        started_at=time.monotonic(),
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
    original_deadline = time.monotonic() + 60.0
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
    session.closed = True
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-1",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_budget_seconds=7200.0,
        request_deadline_at=original_deadline,
        previous_response_id="resp_prev_1",
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

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
        require_exclusive_request=False,
    )
    send_text.assert_not_awaited()
    assert request_state.request_budget_seconds == 7200.0
    assert request_state.request_deadline_at == original_deadline


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
        started_at=time.monotonic(),
        previous_response_id="resp_prev_1",
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

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
    original_deadline = time.monotonic() + 60.0
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
        started_at=time.monotonic(),
        request_budget_seconds=7200.0,
        request_deadline_at=original_deadline,
        response_id="resp_created_no_text",
        awaiting_response_created=False,
        request_text='{"type":"response.create","input":"hello"}',
        transport="http",
        http_bridge_send_completed_at=1.0,
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

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
        require_exclusive_request=True,
    )
    send_text.assert_awaited_once_with('{"type":"response.create","input":"hello"}')
    assert request_state.http_bridge_send_completed_at is not None
    assert request_state.http_bridge_send_completed_at > 1.0
    assert session.last_used_at == request_state.http_bridge_send_completed_at
    assert request_state.request_budget_seconds == 7200.0
    assert request_state.request_deadline_at == original_deadline
    proxy_service._release_websocket_response_create_gate(request_state, session.response_create_gate)
    service._release_request_account_model_concurrency(request_state)


@pytest.mark.asyncio
async def test_replay_send_hard_timeout_does_not_wait_for_cancellation_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    cancellation_seen = asyncio.Event()
    allow_late_send = asyncio.Event()

    async def cancellation_suppressing_send(_text: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_send.wait()

    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "replay-hard-timeout", None),
        account=cast(Any, SimpleNamespace(id="acc-replay-hard-timeout", status=AccountStatus.ACTIVE)),
    )
    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(send_text=cancellation_suppressing_send, close=AsyncMock()),
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-replay-hard-timeout",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 0.02,
        request_text='{"type":"response.create","input":"hello"}',
        event_queue=asyncio.Queue(),
        transport="http",
    )
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", AsyncMock())
    monkeypatch.setattr(service, "_rebind_request_account_model_concurrency", lambda *_args: None)

    started_at = time.monotonic()
    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session,
        request_state=request_state,
        text_data=request_state.request_text,
        send_request=True,
    )

    assert recovered is False
    assert time.monotonic() - started_at < 0.2
    assert cancellation_seen.is_set()
    assert session.closed is True
    proxy_service._release_websocket_response_create_gate(request_state, session.response_create_gate)
    service._release_request_account_model_concurrency(request_state)
    allow_late_send.set()
    await service.close_proxy_cleanup_tasks()
    if service._http_bridge_background_close_tasks:
        await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))


@pytest.mark.asyncio
async def test_ambiguous_replay_send_retires_replacement_before_sibling_can_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "ambiguous-replay", None)
    account = cast(Any, SimpleNamespace(id="acc-replay", status=AccountStatus.ACTIVE))
    send_started = asyncio.Event()
    allow_send_failure = asyncio.Event()
    sibling_prepared = asyncio.Event()
    sent_text: list[str] = []
    close = AsyncMock()

    async def ambiguous_send(text: str) -> None:
        sent_text.append(text)
        send_started.set()
        await allow_send_failure.wait()
        raise ConnectionResetError("connection reset after replay write")

    request_state = proxy_service._WebSocketRequestState(
        request_id="req-ambiguous-replay",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        response_id="resp-before-replay",
        awaiting_response_created=False,
        request_text='{"type":"response.create","input":"replay"}',
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key=key.affinity_key),
        request_model="gpt-5.4",
        account=account,
        upstream=cast(
            UpstreamResponsesWebSocket,
            SimpleNamespace(send_text=ambiguous_send, close=close),
        ),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", AsyncMock())
    monkeypatch.setattr(service, "_rebind_request_account_model_concurrency", lambda *_args: None)
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock(return_value=[]))

    async def sibling_prewarm(*_args: object, **_kwargs: object) -> None:
        sibling_prepared.set()

    monkeypatch.setattr(service, "_maybe_prewarm_http_bridge_session", sibling_prewarm)

    replay_task = asyncio.create_task(
        service._retry_http_bridge_request_on_fresh_upstream(
            session,
            request_state=request_state,
            text_data=request_state.request_text or "",
            reset_response_state=True,
        )
    )
    await asyncio.wait_for(send_started.wait(), timeout=1.0)

    sibling_state = proxy_service._WebSocketRequestState(
        request_id="req-sibling-after-ambiguous-replay",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    sibling_task = asyncio.create_task(
        service._submit_http_bridge_request(
            session,
            request_state=sibling_state,
            text_data='{"type":"response.create","input":"sibling"}',
            queue_limit=4,
        )
    )
    await asyncio.wait_for(sibling_prepared.wait(), timeout=1.0)
    await asyncio.sleep(0)
    allow_send_failure.set()

    assert await replay_task is False
    with pytest.raises(ProxyResponseError):
        await sibling_task
    assert session.closed is True
    assert key not in service._http_bridge_sessions
    assert sent_text == [request_state.request_text]
    assert list(session.pending_requests) == []
    assert session.queued_request_count == 0
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_bridge_no_text_replay_moves_capacity_to_selected_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-replay-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-replay-new", status=AccountStatus.ACTIVE))
    send_text = AsyncMock()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-replay-account-switch",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        response_id="resp-replay-account-switch",
        request_text='{"type":"response.create","input":"hello"}',
        event_queue=asyncio.Queue(),
        transport="http",
        account_model_concurrency=AccountModelConcurrencyLease(None, old_account.id, "gpt-5.6-sol"),
    )
    old_request_lease = request_state.account_model_concurrency
    assert old_request_lease is not None
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "replay-account-switch", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="replay-account-switch"),
        request_model="gpt-5.6-sol",
        account=old_account,
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(send_text=send_text, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=asyncio.Semaphore(1),
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )

    async def reconnect_to_new_account(*_args: Any, **_kwargs: Any) -> None:
        session.account = new_account

    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect_to_new_account)

    recovered = await service._retry_http_bridge_request_on_fresh_upstream(
        session,
        request_state=request_state,
        text_data=request_state.request_text or "",
        reset_response_state=True,
        prefer_same_account=False,
    )

    assert recovered is True
    assert old_request_lease._released is True
    assert request_state.account_model_concurrency is not None
    assert request_state.account_model_concurrency.account_id == new_account.id
    assert request_state.response_create_admission is not None
    send_text.assert_awaited_once()
    proxy_service._release_websocket_response_create_gate(request_state, session.response_create_gate)
    service._release_request_account_model_concurrency(request_state)


@pytest.mark.asyncio
async def test_reconnect_releases_new_session_lease_when_refresh_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-reconnect-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-reconnect-new", status=AccountStatus.ACTIVE))
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "reconnect-cancel", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="reconnect-cancel"),
        request_model="gpt-5.6-sol",
        account=old_account,
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reconnect-cancel",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        transport="http",
    )
    new_session_lease = AccountModelConcurrencyLease(None, new_account.id, "gpt-5.6-sol")
    monkeypatch.setattr(
        service,
        "_http_bridge_dashboard_settings",
        AsyncMock(return_value=SimpleNamespace(prefer_earlier_reset_accounts=False, routing_strategy=None)),
    )
    monkeypatch.setattr(
        service,
        "_select_account_with_budget_compatible",
        AsyncMock(return_value=SimpleNamespace(account=new_account, error_code=None, error_message=None)),
    )
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_session_account_model_concurrency",
        lambda **_kwargs: new_session_lease,
    )
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await service._reconnect_http_bridge_session(session, request_state=request_state)

    assert new_session_lease._released is True
    assert session.account is old_account


@pytest.mark.asyncio
async def test_reconnect_releases_new_session_lease_when_refresh_raises_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-reconnect-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-reconnect-new", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "reconnect-exception", None),
        account=old_account,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reconnect-exception",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        transport="http",
    )
    new_session_lease = AccountModelConcurrencyLease(None, new_account.id, "gpt-5.4")
    monkeypatch.setattr(
        service,
        "_http_bridge_dashboard_settings",
        AsyncMock(return_value=SimpleNamespace(prefer_earlier_reset_accounts=False, routing_strategy=None)),
    )
    monkeypatch.setattr(
        service,
        "_select_account_with_budget_compatible",
        AsyncMock(
            return_value=proxy_service.AccountSelection(
                account=new_account,
                error_code=None,
                error_message=None,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_session_account_model_concurrency",
        lambda **_kwargs: new_session_lease,
    )
    monkeypatch.setattr(
        service,
        "_ensure_fresh_with_budget",
        AsyncMock(side_effect=RuntimeError("unexpected refresh failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected refresh failure"):
        await service._reconnect_http_bridge_session(session, request_state=request_state)

    assert new_session_lease._released is True
    assert session.account is old_account


@pytest.mark.asyncio
async def test_reconnect_releases_unpublished_lease_when_socket_close_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-reconnect-close-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-reconnect-close-new", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "reconnect-close-cancel", None),
        account=old_account,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reconnect-close-cancel",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 0.05,
        transport="http",
    )
    new_upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    new_session_lease = AccountModelConcurrencyLease(None, new_account.id, "gpt-5.4")
    close_started = asyncio.Event()

    async def open_expired_socket(*_args: object, **_kwargs: object) -> UpstreamResponsesWebSocket:
        await asyncio.sleep(0.06)
        return new_upstream

    async def blocked_close(upstream: object, **_kwargs: object) -> None:
        if upstream is session.upstream:
            return
        close_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        service,
        "_http_bridge_dashboard_settings",
        AsyncMock(return_value=SimpleNamespace(prefer_earlier_reset_accounts=False, routing_strategy=None)),
    )
    monkeypatch.setattr(
        service,
        "_select_account_with_budget_compatible",
        AsyncMock(
            return_value=proxy_service.AccountSelection(
                account=new_account,
                error_code=None,
                error_message=None,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_session_account_model_concurrency",
        lambda **_kwargs: new_session_lease,
    )
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=new_account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_expired_socket)
    monkeypatch.setattr(bridge_request_submit, "_close_http_bridge_upstream_before_deadline", blocked_close)

    reconnect = asyncio.create_task(service._reconnect_http_bridge_session(session, request_state=request_state))
    await asyncio.wait_for(close_started.wait(), timeout=1.0)
    reconnect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reconnect

    assert new_session_lease._released is True
    assert session.account is old_account


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
        started_at=time.monotonic(),
        previous_response_id="resp_prev_safe",
        proxy_injected_previous_response_id=True,
        fresh_upstream_request_text='{"type":"response.create","input":"full-history-fallback"}',
        fresh_upstream_request_is_retry_safe=True,
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

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
    proxy_service._release_websocket_response_create_gate(request_state, session.response_create_gate)
    service._release_request_account_model_concurrency(request_state)


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
        started_at=time.monotonic(),
        previous_response_id="resp_prev_unsafe",
        proxy_injected_previous_response_id=True,
        fresh_upstream_request_text='{"type":"response.create","input":"single-item-followup"}',
        fresh_upstream_request_is_retry_safe=False,
        transport="http",
    )
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

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
@pytest.mark.parametrize("abort_kind", ["exception", "cancellation"])
async def test_create_http_bridge_session_releases_unpublished_lease_on_abort(
    monkeypatch: pytest.MonkeyPatch,
    abort_kind: str,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-create-abort", status=AccountStatus.ACTIVE))
    session_lease = AccountModelConcurrencyLease(None, account.id, "gpt-5.4")
    connect_lease = AccountModelConcurrencyLease(None, account.id, "gpt-5.4")
    abort: BaseException
    expected_error: type[BaseException]
    if abort_kind == "cancellation":
        abort = asyncio.CancelledError()
        expected_error = asyncio.CancelledError
    else:
        abort = RuntimeError("unexpected refresh failure")
        expected_error = RuntimeError

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: _make_app_settings())
    monkeypatch.setattr(
        service,
        "_http_bridge_dashboard_settings",
        AsyncMock(return_value=SimpleNamespace(prefer_earlier_reset_accounts=False, routing_strategy=None)),
    )
    monkeypatch.setattr(
        service,
        "_select_account_with_budget_compatible",
        AsyncMock(
            return_value=proxy_service.AccountSelection(
                account=account,
                error_message=None,
                error_code=None,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_session_account_model_concurrency",
        lambda **_kwargs: session_lease,
    )
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_connect_account_model_concurrency",
        lambda **_kwargs: connect_lease,
    )
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(side_effect=abort))

    with pytest.raises(expected_error):
        await service._create_http_bridge_session(
            proxy_service._HTTPBridgeSessionKey("prompt_cache", f"create-{abort_kind}", None),
            headers={},
            affinity=proxy_service._AffinityPolicy(key=f"create-{abort_kind}"),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
        )

    assert session_lease._released is True
    assert connect_lease._released is True


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
    persistence_started = asyncio.Event()
    allow_persistence = asyncio.Event()

    async def blocked_record_error(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persistence_started.set()
        await allow_persistence.wait()

    record_error = AsyncMock(side_effect=blocked_record_error)

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

    session = await asyncio.wait_for(
        service._create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-forbidden"),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
        ),
        timeout=0.5,
    )
    await asyncio.wait_for(persistence_started.wait(), timeout=0.1)

    assert session.account is second_account
    assert session.upstream is upstream
    assert select_account.await_count == 2
    second_call = select_account.await_args_list[1]
    assert second_call.kwargs["exclude_account_ids"] == {"acc-forbidden-a"}
    allow_persistence.set()
    await service.close_proxy_cleanup_tasks()
    record_error.assert_awaited_once_with(first_account)
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_create_http_bridge_session_fails_over_on_nested_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-connect-timeout", None)
    first_account = cast(Any, SimpleNamespace(id="acc-timeout-a", status=AccountStatus.ACTIVE))
    second_account = cast(Any, SimpleNamespace(id="acc-timeout-b", status=AccountStatus.ACTIVE))
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=first_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=second_account, error_message=None, error_code=None),
        ]
    )
    upstream = cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))
    open_upstream = AsyncMock(side_effect=[TimeoutError("timed out during opening handshake"), upstream])
    persistence_started = asyncio.Event()
    allow_persistence = asyncio.Event()

    async def blocked_record_error(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persistence_started.set()
        await allow_persistence.wait()

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
    monkeypatch.setattr(service._load_balancer, "record_error", blocked_record_error)
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    session = await asyncio.wait_for(
        service._create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-connect-timeout"),
            api_key=None,
            request_model="gpt-5.6-sol",
            idle_ttl_seconds=120.0,
        ),
        timeout=0.2,
    )
    await asyncio.wait_for(persistence_started.wait(), timeout=0.1)

    assert session.account is second_account
    assert open_upstream.await_count == 2
    assert select_account.await_args_list[1].kwargs["exclude_account_ids"] == {first_account.id}
    allow_persistence.set()
    await service.close_proxy_cleanup_tasks()
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_create_http_bridge_session_permanent_refresh_failure_fails_over_without_waiting_for_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-permanent-refresh", None)
    first_account = cast(Any, SimpleNamespace(id="acc-refresh-a", status=AccountStatus.ACTIVE))
    second_account = cast(Any, SimpleNamespace(id="acc-refresh-b", status=AccountStatus.ACTIVE))
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=first_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=second_account, error_message=None, error_code=None),
        ]
    )
    upstream = cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))
    persistence_started = asyncio.Event()
    allow_persistence = asyncio.Event()

    async def blocked_permanent_failure(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persistence_started.set()
        await allow_persistence.wait()

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
    monkeypatch.setattr(
        service,
        "_ensure_fresh_with_budget",
        AsyncMock(
            side_effect=[
                RefreshError("invalid_grant", "connect to 10.0.0.9:8443", True),
                second_account,
            ]
        ),
    )
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=upstream))
    monkeypatch.setattr(service._load_balancer, "mark_permanent_failure", blocked_permanent_failure)
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    session = await asyncio.wait_for(
        service._create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-permanent-refresh"),
            api_key=None,
            request_model="gpt-5.6-sol",
            idle_ttl_seconds=120.0,
        ),
        timeout=0.2,
    )

    await asyncio.wait_for(persistence_started.wait(), timeout=0.1)
    assert session.account is second_account
    assert select_account.await_count == 2
    assert select_account.await_args_list[1].kwargs["exclude_account_ids"] == {first_account.id}
    allow_persistence.set()
    await service.close_proxy_cleanup_tasks()
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_create_http_bridge_session_surfaces_latest_mixed_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-mixed-connect-failures", None)
    first_account = cast(Any, SimpleNamespace(id="acc-mixed-a", status=AccountStatus.ACTIVE))
    second_account = cast(Any, SimpleNamespace(id="acc-mixed-b", status=AccountStatus.ACTIVE))
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=first_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=second_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(
                account=None,
                error_message="No active accounts available",
                error_code="no_accounts",
            ),
        ]
    )
    first_error = ProxyResponseError(
        502,
        proxy_service.openai_error("upstream_unavailable", "first structured connection failure"),
    )
    open_upstream = AsyncMock(
        side_effect=[
            first_error,
            TimeoutError("connect to 10.0.0.9:8443 from /srv/internal.sock"),
        ]
    )

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
    monkeypatch.setattr(
        service,
        "_ensure_fresh_with_budget",
        AsyncMock(side_effect=[first_account, second_account]),
    )
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_upstream)
    monkeypatch.setattr(
        service,
        "_handle_websocket_connect_error",
        AsyncMock(return_value=cast(Any, {"failure_class": "retryable_transient"})),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-mixed-connect-failures"),
            api_key=None,
            request_model="gpt-5.6-sol",
            idle_ttl_seconds=120.0,
        )

    await service.close_proxy_cleanup_tasks()

    assert exc_info.value.status_code == 502
    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    assert exc_info.value.payload["error"]["message"] == "HTTP bridge upstream connection timed out"
    assert open_upstream.await_count == 2
    assert select_account.await_count == 3


@pytest.mark.asyncio
async def test_create_http_bridge_session_does_not_rebind_required_account_after_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "required-owner", None)
    preferred_account = cast(Any, SimpleNamespace(id="acc-required", status=AccountStatus.ACTIVE))
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=preferred_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=preferred_account, error_message=None, error_code=None),
        ]
    )

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
    monkeypatch.setattr(
        service,
        "_ensure_fresh_with_budget",
        AsyncMock(side_effect=[preferred_account, preferred_account]),
    )
    monkeypatch.setattr(
        service,
        "_open_upstream_websocket_with_budget",
        AsyncMock(side_effect=TimeoutError("timed out during opening handshake")),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._create_http_bridge_session(
            key,
            headers={"x-codex-session-id": "required-owner"},
            affinity=proxy_service._AffinityPolicy(
                key="required-owner",
                kind=proxy_service.StickySessionKind.CODEX_SESSION,
            ),
            api_key=None,
            request_model="gpt-5.6-sol",
            idle_ttl_seconds=120.0,
            request_stage="reattach",
            preferred_account_id=preferred_account.id,
            require_preferred_account=True,
        )

    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    assert exc_info.value.payload["error"]["message"] == "HTTP bridge upstream connection timed out"
    assert "opening handshake" not in exc_info.value.payload["error"]["message"]
    assert select_account.await_count == 2


@pytest.mark.asyncio
async def test_reconnect_http_bridge_session_can_avoid_current_account_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    original_deadline = time.monotonic() + 60.0
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
        request_budget_seconds=7200.0,
        request_deadline_at=original_deadline,
        transport="http",
    )
    select_calls: list[dict[str, object]] = []
    select_deadlines: list[float] = []

    async def fake_select_account_with_budget(deadline: float, **kwargs: object) -> proxy_service.AccountSelection:
        select_deadlines.append(deadline)
        select_calls.append(kwargs)
        assert kwargs["preferred_account_id"] is None
        assert kwargs["exclude_account_ids"] == {"acc-old"}
        return proxy_service.AccountSelection(account=new_account, error_message=None, error_code=None)

    class _Lease:
        def release(self) -> None:
            return None

    open_upstream = AsyncMock(return_value=new_upstream)
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
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_upstream)

    await service._reconnect_http_bridge_session(
        session,
        request_state=request_state,
        prefer_same_account=False,
    )

    assert select_calls
    assert select_deadlines == [pytest.approx(original_deadline)]
    assert request_state.request_deadline_at == original_deadline
    assert request_state.request_budget_seconds == 7200.0
    assert session.account is new_account
    assert session.upstream is new_upstream
    old_upstream.close.assert_awaited_once()
    open_timeout = open_upstream.await_args.kwargs["timeout_seconds"]
    assert 0 < open_timeout <= 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connect_failure", "persistence_method"),
    [
        (RefreshError("invalid_grant", "connect to 10.0.0.9:8443", True), "mark_permanent_failure"),
        (asyncio.TimeoutError("connect to 10.0.0.9:8443"), "record_error"),
    ],
)
async def test_reconnect_http_bridge_session_account_failure_fails_over_without_waiting_for_persistence(
    monkeypatch: pytest.MonkeyPatch,
    connect_failure: BaseException,
    persistence_method: str,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-reconnect-refresh-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-reconnect-refresh-new", status=AccountStatus.ACTIVE))
    old_upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    new_upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()),
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "reconnect-permanent-refresh", None),
        account=old_account,
    )
    session.upstream = old_upstream
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reconnect-permanent-refresh",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        transport="http",
    )
    select_account = AsyncMock(
        side_effect=[
            proxy_service.AccountSelection(account=old_account, error_message=None, error_code=None),
            proxy_service.AccountSelection(account=new_account, error_message=None, error_code=None),
        ]
    )
    persistence_started = asyncio.Event()
    allow_persistence = asyncio.Event()

    async def blocked_persistence(*args: object, **kwargs: object) -> None:
        del args, kwargs
        persistence_started.set()
        await allow_persistence.wait()

    class _Lease:
        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            self.released = True

    leases = [_Lease(), _Lease()]
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
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_session_account_model_concurrency",
        Mock(side_effect=leases),
    )
    monkeypatch.setattr(
        service,
        "_ensure_fresh_with_budget",
        AsyncMock(
            side_effect=[
                connect_failure,
                new_account,
            ]
        ),
    )
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=new_upstream))
    monkeypatch.setattr(service._load_balancer, persistence_method, blocked_persistence)

    await asyncio.wait_for(
        service._reconnect_http_bridge_session(
            session,
            request_state=request_state,
            prefer_same_account=True,
        ),
        timeout=0.2,
    )

    await asyncio.wait_for(persistence_started.wait(), timeout=0.1)
    assert session.account is new_account
    assert session.upstream is new_upstream
    assert select_account.await_count == 2
    assert leases[0].released is True
    assert session.account_model_session_lease is leases[1]
    allow_persistence.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_reconnect_http_bridge_session_rejects_expired_request_before_mutating_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    account = cast(Any, SimpleNamespace(id="acc-expired-reconnect", status=AccountStatus.ACTIVE))
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-expired-reconnect", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="cache-expired-reconnect"),
        request_model="gpt-5.6-sol",
        account=account,
        upstream=old_upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-expired-reconnect",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic() - 7201.0,
        request_budget_seconds=7200.0,
        request_deadline_at=time.monotonic() - 1.0,
        transport="http",
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    old_reader = asyncio.create_task(wait_forever())
    session.upstream_reader = old_reader
    cancel_reader = AsyncMock(return_value=True)
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_cancel_http_bridge_upstream_reader", cancel_reader)

    try:
        with pytest.raises(ProxyResponseError) as exc_info:
            await service._reconnect_http_bridge_session(
                session,
                request_state=request_state,
                restart_reader=True,
            )

        assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
        cancel_reader.assert_not_awaited()
        old_upstream.close.assert_not_awaited()
        assert session.upstream is old_upstream
        assert session.upstream_reader is old_reader
        assert not old_reader.done()
    finally:
        old_reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_reader


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_is_current_task", [True, False])
async def test_reconnect_http_bridge_session_preserves_single_reader_owner(
    monkeypatch: pytest.MonkeyPatch,
    reader_is_current_task: bool,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-reader-owner", status=AccountStatus.ACTIVE))
    old_upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    new_upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()),
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-reader-owner", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-reader-owner",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.4",
        account=account,
        upstream=old_upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=1.0,
        idle_ttl_seconds=120.0,
        account_model_session_lease=cast(Any, SimpleNamespace(release=lambda: None)),
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reader-owner",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        transport="http",
    )

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
    monkeypatch.setattr(
        service,
        "_select_account_with_budget_compatible",
        AsyncMock(return_value=proxy_service.AccountSelection(account=account, error_message=None, error_code=None)),
    )
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", AsyncMock(return_value=new_upstream))

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def relay(_session: proxy_service._HTTPBridgeSession) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", relay)

    current_task = asyncio.current_task()
    assert current_task is not None
    old_reader: asyncio.Task[None]
    if reader_is_current_task:
        old_reader = cast(asyncio.Task[None], current_task)
    else:

        async def wait_for_cancellation() -> None:
            await asyncio.Event().wait()

        old_reader = asyncio.create_task(wait_for_cancellation())
    session.upstream_reader = old_reader

    try:
        await service._reconnect_http_bridge_session(
            session,
            request_state=request_state,
            restart_reader=True,
        )

        assert session.upstream is new_upstream
        if reader_is_current_task:
            assert session.upstream_reader is current_task
            assert relay_started.is_set() is False
        else:
            assert old_reader.cancelled()
            assert session.upstream_reader is not None
            assert session.upstream_reader is not old_reader
            await asyncio.wait_for(relay_started.wait(), timeout=1.0)
    finally:
        if not reader_is_current_task:
            release_relay.set()
            if session.upstream_reader is not None:
                await session.upstream_reader
            if not old_reader.done():
                old_reader.cancel()


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

    await service.close_proxy_cleanup_tasks()

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
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))
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
    next_receive_timeout = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "_write_request_log", write_request_log)
    monkeypatch.setattr(service, "_next_websocket_receive_timeout", next_receive_timeout)

    await service._relay_http_bridge_upstream_messages(session)
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))

    failed_event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
    assert failed_event is not None
    assert '"code":"stream_incomplete"' in failed_event
    assert "Upstream websocket closed before response.completed" in failed_event
    assert "keepalive ping timeout" not in failed_event
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    assert key not in service._http_bridge_sessions
    assert not service._http_bridge_turn_state_index
    assert not service._http_bridge_previous_response_index
    assert session.downstream_turn_state_aliases == set()
    assert session.previous_response_ids == set()
    assert session.closed is True
    upstream.close.assert_awaited_once()
    write_request_log.assert_awaited_once()
    assert next_receive_timeout.await_args.kwargs["proxy_request_budget_seconds"] == 7200.0


@pytest.mark.asyncio
async def test_http_bridge_reader_expires_only_elapsed_request_on_shared_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-mixed-deadlines", None)
    now = time.monotonic()
    expired_queue: asyncio.Queue[str | None] = asyncio.Queue()
    newer_queue: asyncio.Queue[str | None] = asyncio.Queue()
    expired_request = proxy_service._WebSocketRequestState(
        request_id="req-hard-deadline-expired",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=now - 10.0,
        request_budget_seconds=7200.0,
        request_deadline_at=now - 0.01,
        response_id="resp-hard-deadline-expired",
        event_queue=expired_queue,
        transport="http",
    )
    newer_request = proxy_service._WebSocketRequestState(
        request_id="req-hard-deadline-newer",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=now,
        request_budget_seconds=7200.0,
        request_deadline_at=now + 60.0,
        awaiting_response_created=True,
        event_queue=newer_queue,
        request_text='{"type":"response.create","model":"gpt-5.6-sol","input":[]}',
        transport="http",
    )
    receive_started = asyncio.Event()
    release_receive = asyncio.Event()

    async def receive() -> UpstreamWebSocketMessage:
        receive_started.set()
        await release_receive.wait()
        return UpstreamWebSocketMessage(kind="closed")

    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=receive, close=AsyncMock()),
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key="bridge-mixed-deadlines"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-mixed-deadlines", status=AccountStatus.ACTIVE)),
        upstream=upstream,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([expired_request, newer_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=2,
        last_used_at=now,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_write_request_log", AsyncMock())

    original_fail_pending = service._fail_pending_websocket_requests
    finalization_started = asyncio.Event()
    release_finalization = asyncio.Event()

    async def slow_fail_pending(**kwargs: Any) -> None:
        finalization_started.set()
        await release_finalization.wait()
        await original_fail_pending(**kwargs)

    monkeypatch.setattr(service, "_fail_pending_websocket_requests", slow_fail_pending)

    reader = asyncio.create_task(service._relay_http_bridge_upstream_messages(session))
    try:
        await asyncio.wait_for(finalization_started.wait(), timeout=1.0)
        assert list(session.pending_requests) == [newer_request]
        assert session.queued_request_count == 1
        assert session.discarded_response_ids == {"resp-hard-deadline-expired"}

        release_finalization.set()
        expired_event = await asyncio.wait_for(expired_queue.get(), timeout=1.0)
        assert expired_event is not None and '"code":"upstream_request_timeout"' in expired_event
        assert await asyncio.wait_for(expired_queue.get(), timeout=0.1) is None
        await asyncio.wait_for(receive_started.wait(), timeout=1.0)

        await service._process_http_bridge_upstream_text(
            session,
            '{"type":"response.failed","response":{"id":"resp-hard-deadline-expired",'
            '"status":"failed","error":{"code":"server_error","message":"late failure"}}}',
        )

        assert list(session.pending_requests) == [newer_request]
        assert session.queued_request_count == 1
        assert session.discarded_response_ids == set()
        assert newer_queue.empty()
        assert session.closed is False
        assert service._http_bridge_sessions[key] is session
    finally:
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader


@pytest.mark.asyncio
async def test_http_bridge_expiry_finishes_detached_cleanup_before_propagating_task_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    expired_request = proxy_service._WebSocketRequestState(
        request_id="req-expiry-cancelled-cleanup",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=now - 10.0,
        request_deadline_at=now - 0.01,
        response_id="resp-expiry-cancelled-cleanup",
        event_queue=event_queue,
        transport="http",
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "expiry-cancelled-cleanup", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="expiry-cancelled-cleanup"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-expiry-cancelled-cleanup", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([expired_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1,
        last_used_at=now,
        idle_ttl_seconds=120.0,
    )
    monkeypatch.setattr(service, "_write_request_log", AsyncMock())
    original_fail_pending = service._fail_pending_websocket_requests
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_fail_pending(**kwargs: Any) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        await original_fail_pending(**kwargs)

    monkeypatch.setattr(service, "_fail_pending_websocket_requests", slow_fail_pending)
    expiry_task = asyncio.create_task(
        service._fail_http_bridge_expired_requests(
            session,
            request_budget_seconds=7200.0,
            error_code="upstream_request_timeout",
            error_message="Proxy request budget exhausted",
        )
    )

    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    assert list(session.pending_requests) == []
    assert session.queued_request_count == 0
    expiry_task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await expiry_task
    failed_event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
    assert failed_event is not None and '"code":"upstream_request_timeout"' in failed_event
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None


@pytest.mark.asyncio
async def test_http_bridge_unidentified_expiry_retires_ambiguous_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    expired_queue: asyncio.Queue[str | None] = asyncio.Queue()
    sibling_queue: asyncio.Queue[str | None] = asyncio.Queue()
    expired_request = proxy_service._WebSocketRequestState(
        request_id="req-unidentified-expired",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=now - 10.0,
        request_deadline_at=now - 0.01,
        awaiting_response_created=True,
        request_text='{"type":"response.create","input":[]}',
        event_queue=expired_queue,
        transport="http",
    )
    sibling_request = proxy_service._WebSocketRequestState(
        request_id="req-unidentified-sibling",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=now,
        request_deadline_at=now + 60.0,
        response_id="resp-unidentified-sibling",
        event_queue=sibling_queue,
        transport="http",
    )
    close_upstream = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "unidentified-expiry", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="unidentified-expiry"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-unidentified-expiry", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=close_upstream)),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([expired_request, sibling_request]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=2,
        last_used_at=now,
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[session.key] = session
    monkeypatch.setattr(service, "_write_request_log", AsyncMock())

    result = await service._fail_http_bridge_expired_requests(
        session,
        request_budget_seconds=7200.0,
        error_code="upstream_request_timeout",
        error_message="Proxy request budget exhausted",
    )

    assert result.expired_requests == (expired_request,)
    assert result.retire_ambiguous_transport is True
    assert session.closed is True
    assert list(session.pending_requests) == []
    assert session.key not in service._http_bridge_sessions
    expired_event = await expired_queue.get()
    sibling_event = await sibling_queue.get()
    assert expired_event is not None and '"code":"upstream_request_timeout"' in expired_event
    assert sibling_event is not None and '"code":"stream_incomplete"' in sibling_event
    assert await expired_queue.get() is None
    assert await sibling_queue.get() is None
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))
    close_upstream.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_http_bridge_reader_wakes_for_later_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    receive_started = asyncio.Event()
    release_receive = asyncio.Event()
    receive_calls = 0

    async def receive() -> UpstreamWebSocketMessage:
        nonlocal receive_calls
        receive_calls += 1
        receive_started.set()
        await release_receive.wait()
        return UpstreamWebSocketMessage(kind="close")

    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "idle-reader-wake", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="idle-reader-wake"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="acc-idle-reader", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(receive=receive, close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    service._http_bridge_sessions[session.key] = session
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_write_request_log", AsyncMock())

    reader = asyncio.create_task(service._relay_http_bridge_upstream_messages(session))
    try:
        await asyncio.wait_for(receive_started.wait(), timeout=1.0)
        event_queue: asyncio.Queue[str | None] = asyncio.Queue()
        request_state = proxy_service._WebSocketRequestState(
            request_id="req-idle-reader-deadline",
            model="gpt-5.6-sol",
            service_tier=None,
            reasoning_effort="max",
            api_key_reservation=None,
            started_at=time.monotonic(),
            request_deadline_at=time.monotonic() + 0.03,
            response_id="resp-idle-reader-deadline",
            event_queue=event_queue,
            transport="http",
        )
        async with session.pending_lock:
            session.pending_requests.append(request_state)
            session.queued_request_count = 1
            session.pending_changed.set()

        failed_event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
        assert failed_event is not None and '"code":"upstream_request_timeout"' in failed_event
        assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
        assert receive_calls == 1
        assert session.closed is False
    finally:
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader


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
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

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
async def test_http_bridge_terminal_delivery_does_not_wait_for_global_alias_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-terminal-global-lock",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=event_queue,
        transport="http",
        response_id="resp-terminal-global-lock",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "terminal-global-lock", None),
        account=cast(Any, SimpleNamespace(id="acc-terminal-global-lock", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    monkeypatch.setattr(service, "_schedule_websocket_request_finalization", Mock())
    await service._http_bridge_lock.acquire()
    try:
        await asyncio.wait_for(
            service._process_http_bridge_upstream_text(
                session,
                '{"type":"response.completed","response":{"id":"resp-terminal-global-lock"}}',
            ),
            timeout=0.1,
        )
        assert await asyncio.wait_for(event_queue.get(), timeout=0.1) == (
            'data: {"type":"response.completed","response":{"id":"resp-terminal-global-lock"}}\n\n'
        )
        assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    finally:
        service._http_bridge_lock.release()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_http_bridge_terminal_queue_closes_without_propagating_background_finalization_failure(
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

    finalization_failed = asyncio.Event()

    async def fail_finalization(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        finalization_failed.set()
        raise RuntimeError("finalization failed")

    monkeypatch.setattr(service, "_finalize_websocket_request_state", fail_finalization)

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.completed","response":{"id":"resp-terminal-order"}}',
    )

    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) == (
        'data: {"type":"response.completed","response":{"id":"resp-terminal-order"}}\n\n'
    )
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    await asyncio.wait_for(finalization_failed.wait(), timeout=0.1)
    await service.close_proxy_cleanup_tasks()


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


@pytest.mark.asyncio
async def test_direct_websocket_reader_timeout_is_hard_when_receive_suppresses_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-ws-hard-receive-timeout",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        transport="websocket",
    )
    pending_requests: deque[proxy_service._WebSocketRequestState] = deque([request_state])
    pending_lock = anyio.Lock()
    cancellation_seen = asyncio.Event()
    allow_late_receive = asyncio.Event()

    async def cancellation_suppressing_receive() -> UpstreamWebSocketMessage:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_receive.wait()
        return UpstreamWebSocketMessage(kind="closed")

    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=cancellation_suppressing_receive, close=AsyncMock()),
    )
    websocket = cast(
        WebSocket,
        SimpleNamespace(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock()),
    )
    fail_expired = AsyncMock(return_value=True)
    fail_pending = AsyncMock()
    monkeypatch.setattr(
        service,
        "_next_websocket_receive_timeout",
        AsyncMock(
            return_value=SimpleNamespace(
                timeout_seconds=0.01,
                error_code="upstream_request_timeout",
                error_message="Proxy request budget exhausted",
                fail_all_pending=False,
            )
        ),
    )
    monkeypatch.setattr(service, "_fail_expired_pending_websocket_requests", fail_expired)
    monkeypatch.setattr(service, "_fail_pending_websocket_requests", fail_pending)

    started_at = time.monotonic()
    await service._relay_upstream_websocket_messages(
        websocket,
        upstream,
        account=cast(Any, SimpleNamespace(id="acc-ws-hard-receive", status=AccountStatus.ACTIVE)),
        account_id_value="acc-ws-hard-receive",
        pending_requests=pending_requests,
        pending_lock=pending_lock,
        client_send_lock=anyio.Lock(),
        api_key=None,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        response_create_gate=None,
        proxy_request_budget_seconds=60.0,
        stream_idle_timeout_seconds=60.0,
        downstream_activity=proxy_service._DownstreamWebSocketActivity(),
    )

    assert time.monotonic() - started_at < 0.2
    assert cancellation_seen.is_set()
    fail_expired.assert_awaited_once()
    fail_pending.assert_awaited_once()
    cast(AsyncMock, upstream.close).assert_not_awaited()
    assert service._proxy_cleanup_tasks
    allow_late_receive.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_direct_websocket_replay_close_is_hard_bounded_and_late_close_remains_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-ws-replay-close-hard-bound",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        request_text='{"type":"response.create","model":"gpt-5.4"}',
        transport="websocket",
    )
    pending_requests: deque[proxy_service._WebSocketRequestState] = deque([request_state])
    pending_lock = anyio.Lock()
    close_cancelled = asyncio.Event()
    allow_late_close = asyncio.Event()

    async def cancellation_suppressing_close() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            close_cancelled.set()
            await allow_late_close.wait()

    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(
            receive=AsyncMock(return_value=UpstreamWebSocketMessage(kind="text", text='{"type":"error"}')),
            close=cancellation_suppressing_close,
        ),
    )
    websocket = cast(
        WebSocket,
        SimpleNamespace(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock()),
    )
    upstream_control = proxy_service._WebSocketUpstreamControl()

    async def request_replay(*args: object, **kwargs: object) -> str:
        del args, kwargs
        async with pending_lock:
            pending_requests.remove(request_state)
        upstream_control.reconnect_requested = True
        upstream_control.replay_request_state = request_state
        upstream_control.suppress_downstream_event = True
        return '{"type":"error"}'

    monkeypatch.setattr(service, "_process_upstream_websocket_text", request_replay)
    monkeypatch.setattr(proxy_websocket_relay, "_DIRECT_WEBSOCKET_CLOSE_OBSERVATION_SECONDS", 0.01)

    started_at = time.monotonic()
    await service._relay_upstream_websocket_messages(
        websocket,
        upstream,
        account=cast(Any, SimpleNamespace(id="acc-ws-replay-close", status=AccountStatus.ACTIVE)),
        account_id_value="acc-ws-replay-close",
        pending_requests=pending_requests,
        pending_lock=pending_lock,
        client_send_lock=anyio.Lock(),
        api_key=None,
        upstream_control=upstream_control,
        response_create_gate=None,
        proxy_request_budget_seconds=60.0,
        stream_idle_timeout_seconds=60.0,
        downstream_activity=proxy_service._DownstreamWebSocketActivity(),
    )

    assert time.monotonic() - started_at < 0.2
    assert close_cancelled.is_set()
    assert upstream_control.upstream_close_owned is True
    assert upstream_control.replay_request_state is request_state
    assert service._proxy_cleanup_tasks
    allow_late_close.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_direct_websocket_idle_reader_wakes_for_new_request_deadline_without_second_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    pending_requests: deque[proxy_service._WebSocketRequestState] = deque()
    pending_lock = anyio.Lock()
    pending_changed = asyncio.Event()
    receive_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    allow_late_receive = asyncio.Event()
    receive_calls = 0

    async def cancellation_suppressing_receive() -> UpstreamWebSocketMessage:
        nonlocal receive_calls
        receive_calls += 1
        receive_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_receive.wait()
        return UpstreamWebSocketMessage(kind="closed")

    upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=cancellation_suppressing_receive, close=AsyncMock()),
    )
    websocket = cast(
        WebSocket,
        SimpleNamespace(send_text=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock()),
    )
    fail_expired = AsyncMock(return_value=True)
    fail_pending = AsyncMock()
    monkeypatch.setattr(service, "_fail_expired_pending_websocket_requests", fail_expired)
    monkeypatch.setattr(service, "_fail_pending_websocket_requests", fail_pending)

    reader = asyncio.create_task(
        service._relay_upstream_websocket_messages(
            websocket,
            upstream,
            account=cast(Any, SimpleNamespace(id="acc-idle-wake", status=AccountStatus.ACTIVE)),
            account_id_value="acc-idle-wake",
            pending_requests=pending_requests,
            pending_lock=pending_lock,
            client_send_lock=anyio.Lock(),
            api_key=None,
            upstream_control=proxy_service._WebSocketUpstreamControl(),
            response_create_gate=None,
            proxy_request_budget_seconds=60.0,
            stream_idle_timeout_seconds=60.0,
            downstream_activity=proxy_service._DownstreamWebSocketActivity(),
            pending_changed=pending_changed,
        )
    )
    await asyncio.wait_for(receive_started.wait(), timeout=0.1)

    request_state = proxy_service._WebSocketRequestState(
        request_id="req-idle-reader-new-deadline",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 0.02,
        transport="websocket",
    )
    async with pending_lock:
        pending_requests.append(request_state)
        pending_changed.set()

    await asyncio.wait_for(reader, timeout=0.2)

    assert receive_calls == 1
    assert cancellation_seen.is_set()
    fail_expired.assert_awaited_once()
    fail_pending.assert_awaited_once()
    cast(AsyncMock, upstream.close).assert_not_awaited()
    allow_late_receive.set()
    await service.close_proxy_cleanup_tasks()
    cast(AsyncMock, upstream.close).assert_awaited_once()
    cast(AsyncMock, upstream.close).assert_awaited_once()


@pytest.mark.asyncio
async def test_precreated_replay_deadline_expires_while_waiting_for_lifecycle_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-precreated-lock-deadline",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=now,
        request_budget_seconds=7200.0,
        request_deadline_at=now + 0.03,
        awaiting_response_created=True,
        event_queue=asyncio.Queue(),
        request_text='{"type":"response.create","input":[]}',
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "precreated-lock-deadline", None),
        account=cast(Any, SimpleNamespace(id="acc-precreated-lock", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    reconnect = AsyncMock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

    await session.lifecycle_lock.acquire()
    try:
        replayed = await service._retry_http_bridge_precreated_request(session)
    finally:
        session.lifecycle_lock.release()

    assert replayed is False
    assert request_state.replay_count == 0
    reconnect.assert_not_awaited()
    assert list(session.pending_requests) == [request_state]


@pytest.mark.asyncio
async def test_reconnect_closes_unpublished_socket_when_deadline_expires_after_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-reconnect-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-reconnect-new", status=AccountStatus.ACTIVE))
    old_upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock()))
    late_close_cancelled = asyncio.Event()
    allow_late_close = asyncio.Event()
    late_close_calls = 0

    async def cancellation_suppressing_late_close() -> None:
        nonlocal late_close_calls
        late_close_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            late_close_cancelled.set()
            await allow_late_close.wait()

    new_upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(close=cancellation_suppressing_late_close),
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "reconnect-unpublished", None),
        account=old_account,
    )
    session.upstream = old_upstream
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reconnect-unpublished",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 0.05,
        transport="http",
    )

    class Lease:
        def __init__(self) -> None:
            self.release = Mock()

    lease = Lease()

    async def open_then_expire(*args: object, **kwargs: object) -> UpstreamResponsesWebSocket:
        del args, kwargs
        await asyncio.sleep(0.06)
        return new_upstream

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
    monkeypatch.setattr(
        service,
        "_select_account_with_budget_compatible",
        AsyncMock(
            return_value=proxy_service.AccountSelection(
                account=new_account,
                error_message=None,
                error_code=None,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_try_acquire_http_bridge_session_account_model_concurrency",
        lambda **kwargs: lease,
    )
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=new_account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_then_expire)

    with pytest.raises(ProxyResponseError, match="Proxy response error") as exc_info:
        await service._reconnect_http_bridge_session(
            session,
            request_state=request_state,
            prefer_same_account=False,
        )

    assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
    old_upstream.close.assert_awaited_once()
    await asyncio.wait_for(late_close_cancelled.wait(), timeout=0.1)
    assert late_close_calls == 1
    lease.release.assert_called_once()
    assert session.account is old_account
    assert session.upstream is old_upstream
    assert session.closed is True
    allow_late_close.set()
    await service.close_proxy_cleanup_tasks()
    assert late_close_calls == 1


@pytest.mark.asyncio
async def test_terminal_failure_after_replay_attempt_is_finalized_against_original_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    old_account = cast(Any, SimpleNamespace(id="acc-event-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-event-new", status=AccountStatus.ACTIVE))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-event-account",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        awaiting_response_created=True,
        event_queue=asyncio.Queue(),
        request_text='{"type":"response.create","input":[]}',
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "event-account", None),
        account=old_account,
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1

    async def fail_replay(*args: object, **kwargs: object):
        del args, kwargs
        session.account = new_account
        return False, request_state, False, None

    finalize = AsyncMock()
    monkeypatch.setattr(service, "_retry_http_bridge_terminal_failure", fail_replay)
    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalize)

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.failed","response":{"id":"resp-event-account","status":"failed",'
        '"error":{"code":"rate_limit_exceeded","message":"rate limited"}}}',
    )

    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs["account"] is old_account
    assert finalize.await_args.kwargs["account_id_value"] == old_account.id


@pytest.mark.asyncio
async def test_http_bridge_sanitizes_provider_emitted_error_event_before_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-provider-error", status=AccountStatus.ACTIVE))
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-provider-error",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=event_queue,
        transport="http",
    )
    request_state.response_id = "resp-provider-error"
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "provider-error", None),
        account=account,
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    schedule_finalization = Mock()
    monkeypatch.setattr(service, "_schedule_websocket_request_finalization", schedule_finalization)

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.failed","response":{"id":"resp-provider-error","status":"failed",'
        '"error":{"code":"connect-to-10.0.0.8:8443","type":"/srv/private.sock",'
        '"message":"connect to 10.0.0.8:8443 failed from /srv/private.sock",'
        '"param":"/srv/private.sock"}}}',
    )

    terminal = await asyncio.wait_for(event_queue.get(), timeout=1.0)
    assert terminal is not None
    assert '"code":"upstream_error"' in terminal
    assert '"message":"Upstream request failed"' in terminal
    assert '"type":"server_error"' in terminal
    assert "10.0.0.8" not in terminal
    assert "private.sock" not in terminal
    assert await asyncio.wait_for(event_queue.get(), timeout=1.0) is None
    schedule_finalization.assert_called_once()


@pytest.mark.asyncio
async def test_submit_cancellation_before_pending_registration_releases_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-submit-cancel", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "submit-cancel", None),
        account=account,
    )
    session.submit_lease_count = 1
    service._http_bridge_sessions[session.key] = session
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-submit-cancel",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    admission_started = asyncio.Event()

    async def block_admission(*args: object, **kwargs: object) -> None:
        del args, kwargs
        admission_started.set()
        await asyncio.Event().wait()

    class RequestLease:
        account_id = account.id

        def __init__(self) -> None:
            self.release = Mock()

    request_lease = RequestLease()
    release_submit_lease = AsyncMock()
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_maybe_prewarm_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_acquire_request_state_response_create_admission", block_admission)
    monkeypatch.setattr(service, "_try_acquire_account_model_concurrency", lambda **kwargs: request_lease)
    monkeypatch.setattr(service, "_release_http_bridge_submit_lease", release_submit_lease)

    submit_task = asyncio.create_task(
        service._submit_http_bridge_request(
            session,
            request_state=request_state,
            text_data='{"type":"response.create","input":[]}',
            queue_limit=4,
        )
    )
    await asyncio.wait_for(admission_started.wait(), timeout=1.0)
    submit_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit_task

    release_submit_lease.assert_awaited_once_with(session)
    request_lease.release.assert_called_once()
    assert session.queued_request_count == 0
    assert list(session.pending_requests) == []
    assert request_state.response_create_admission is None
    assert request_state.account_model_concurrency is None


@pytest.mark.asyncio
async def test_terminal_request_finalization_survives_reader_task_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-terminal-cancel", status=AccountStatus.ACTIVE))
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "terminal-cancel", None),
        account=account,
    )
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-terminal-cancel",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="resp-terminal-cancel",
        event_queue=event_queue,
        transport="http",
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    finalization_started = asyncio.Event()
    allow_finalization = asyncio.Event()
    finalization_completed = asyncio.Event()

    async def finalize(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        finalization_started.set()
        await allow_finalization.wait()
        finalization_completed.set()

    monkeypatch.setattr(service, "_register_http_bridge_previous_response_id", AsyncMock())
    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalize)

    process_task = asyncio.create_task(
        service._process_http_bridge_upstream_text(
            session,
            '{"type":"response.completed","response":{"id":"resp-terminal-cancel",'
            '"status":"completed","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}',
        )
    )
    await asyncio.wait_for(finalization_started.wait(), timeout=1.0)
    process_task.cancel()
    try:
        await process_task
    except asyncio.CancelledError:
        pass
    assert not finalization_completed.is_set()

    allow_finalization.set()
    await service.close_proxy_cleanup_tasks()

    assert finalization_completed.is_set()
    assert '"type":"response.completed"' in await asyncio.wait_for(event_queue.get(), timeout=1.0)
    assert await asyncio.wait_for(event_queue.get(), timeout=1.0) is None
    assert request_state not in session.pending_requests
    assert session.queued_request_count == 0


@pytest.mark.asyncio
async def test_http_bridge_send_exception_retires_transport_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-ambiguous-send", status=AccountStatus.ACTIVE))
    send_text = AsyncMock(side_effect=ConnectionResetError("10.0.0.8:443 reset /srv/private.sock after write"))
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_calls = 0

    async def cancellation_suppressing_close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_started.set()
        try:
            await allow_close.wait()
        except asyncio.CancelledError:
            await allow_close.wait()

    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "ambiguous-send", None),
        account=account,
    )
    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(send_text=send_text, close=cancellation_suppressing_close),
    )
    session.submit_lease_count = 1
    service._http_bridge_sessions[session.key] = session
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-ambiguous-send",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    retry = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_maybe_prewarm_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_acquire_request_state_response_create_admission", AsyncMock())
    monkeypatch.setattr(
        service,
        "_try_acquire_account_model_concurrency",
        lambda **_kwargs: AccountModelConcurrencyLease(None, account.id, "gpt-5.6-sol"),
    )
    monkeypatch.setattr(service, "_retry_http_bridge_request_on_fresh_upstream", retry)

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request(
            session,
            request_state=request_state,
            text_data='{"type":"response.create","input":"hello"}',
            queue_limit=4,
        )

    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    assert exc_info.value.payload["error"]["message"] == "HTTP bridge upstream send failed"
    send_text.assert_awaited_once()
    retry.assert_not_awaited()
    assert request_state.http_bridge_send_started_at is not None
    assert session.closed is True
    assert list(session.pending_requests) == []
    assert session.queued_request_count == 0
    await asyncio.wait_for(close_started.wait(), timeout=0.1)
    assert close_calls == 1
    assert service._http_bridge_background_close_tasks
    allow_close.set()
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))
    assert close_calls == 1


@pytest.mark.asyncio
async def test_http_bridge_reader_cannot_replay_completed_send_without_response_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-reader-send-race", status=AccountStatus.ACTIVE))
    sent_at = time.monotonic()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-reader-send-race",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="max",
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        request_text='{"type":"response.create","input":"hello"}',
        http_bridge_send_started_at=sent_at,
        http_bridge_send_completed_at=sent_at,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "reader-send-race", None),
        account=account,
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    reconnect = AsyncMock()
    monkeypatch.setattr(service, "_reconnect_http_bridge_session_locked", reconnect)

    retried = await service._retry_http_bridge_precreated_request(session)

    assert retried is False
    reconnect.assert_not_awaited()
    assert list(session.pending_requests) == [request_state]
    assert request_state.replay_count == 0


@pytest.mark.asyncio
async def test_http_bridge_recovery_reservation_inherits_deadline_and_releases_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    api_key = _make_api_key(key_id="key-recovery-deadline", assigned_account_ids=[])
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-recovery-deadline",
        key_id=api_key.id,
        model="gpt-5.4",
    )
    cancellation_seen = asyncio.Event()
    allow_late_reservation = asyncio.Event()
    release = AsyncMock()

    async def cancellation_suppressing_reserve(*args: object, **kwargs: object):
        del args, kwargs
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_reservation.wait()
        return reservation

    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", cancellation_suppressing_reserve)
    monkeypatch.setattr(service, "_release_websocket_reservation", release)

    started_at = time.monotonic()
    request_deadline_at = started_at + 0.2
    with pytest.raises(ProxyResponseError) as exc_info:
        await bridge_stream._reserve_http_bridge_recovery_usage_before_deadline(
            service,
            api_key,
            request_model="gpt-5.4",
            request_service_tier=None,
            # Leave enough time for the deterministic acquisition preflight to
            # reach the cancellation-suppressing create operation under loaded
            # full-suite runs; the create itself still outlives this deadline.
            request_deadline_at=request_deadline_at,
            request_id="req-recovery-deadline",
        )

    # asyncio.wait() can resume a few scheduler ticks after the exact timeout.
    # Keep the assertion tight enough to catch cancellation waits while allowing
    # bounded event-loop scheduling jitter around the inherited deadline.
    assert time.monotonic() <= request_deadline_at + 0.05
    assert exc_info.value.payload["error"]["message"] == "Proxy request budget exhausted"
    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.2)
    allow_late_reservation.set()
    await service.close_proxy_cleanup_tasks()
    release.assert_awaited_once_with(reservation)


@pytest.mark.asyncio
async def test_http_bridge_late_recovery_reservation_reconciliation_owns_release_without_successor_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    api_key = _make_api_key(key_id="key-recovery-owned-release", assigned_account_ids=[])
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-recovery-owned-release",
        key_id=api_key.id,
        model="gpt-5.4",
    )
    cancellation_seen = asyncio.Event()
    allow_late_reservation = asyncio.Event()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    settle_or_release = AsyncMock()

    async def cancellation_suppressing_reserve(*args: object, **kwargs: object):
        del args, kwargs
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_reservation.wait()
        return reservation

    async def owned_release(
        actual: proxy_service.ApiKeyUsageReservationData,
        *,
        reason: str,
    ) -> None:
        assert actual is reservation
        assert reason == "late-http-bridge-recovery-req-recovery-owned-release"
        release_started.set()
        await allow_release.wait()

    monkeypatch.setattr(service, "_reserve_websocket_api_key_usage", cancellation_suppressing_reserve)
    monkeypatch.setattr(service, "_release_websocket_reservation_with_retry", owned_release)
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        settle_or_release,
    )

    with pytest.raises(ProxyResponseError):
        await bridge_stream._reserve_http_bridge_recovery_usage_before_deadline(
            service,
            api_key,
            request_model="gpt-5.4",
            request_service_tier=None,
            request_deadline_at=time.monotonic() + 0.01,
            request_id="req-recovery-owned-release",
        )

    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    allow_late_reservation.set()
    await asyncio.wait_for(release_started.wait(), timeout=0.1)
    settle_or_release.assert_not_awaited()
    assert service._proxy_cleanup_tasks
    allow_release.set()
    await service.close_proxy_cleanup_tasks()
    assert not service._proxy_cleanup_tasks


@pytest.mark.asyncio
async def test_http_bridge_reader_defers_socket_close_until_detached_receive_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "detached-reader-close", None)
    cancellation_seen = asyncio.Event()
    allow_late_receive = asyncio.Event()
    upstream_close = AsyncMock()

    async def cancellation_suppressing_receive() -> UpstreamWebSocketMessage:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_receive.wait()
        return UpstreamWebSocketMessage(kind="closed")

    session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-detached-reader", status=AccountStatus.ACTIVE)),
    )
    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=cancellation_suppressing_receive, close=upstream_close),
    )
    service._http_bridge_sessions[key] = session
    monkeypatch.setattr(
        service,
        "_next_websocket_receive_timeout",
        AsyncMock(
            return_value=SimpleNamespace(
                timeout_seconds=0.01,
                error_code="upstream_request_timeout",
                error_message="Proxy request budget exhausted",
                response_created_request_ids=frozenset(),
                fail_all_pending=True,
            )
        ),
    )
    monkeypatch.setattr(service, "_retry_http_bridge_precreated_request", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_fail_pending_websocket_requests", AsyncMock())
    monkeypatch.setattr(service, "_release_durable_http_bridge_session_ownership", AsyncMock())

    await service._relay_http_bridge_upstream_messages(session)

    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    assert session.detached_upstream_receive is not None
    upstream_close.assert_not_awaited()
    close_tasks = tuple(service._http_bridge_background_close_tasks)
    assert close_tasks
    allow_late_receive.set()
    await asyncio.gather(*close_tasks)
    upstream_close.assert_awaited_once()
    assert session.detached_upstream_receive is None


@pytest.mark.asyncio
async def test_http_bridge_replay_close_is_hard_bounded_outside_lifecycle_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-bridge-replay-close-hard",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        request_text='{"type":"response.create","model":"gpt-5.4"}',
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "bridge-replay-close-hard", None),
        account=cast(Any, SimpleNamespace(id="acc-bridge-replay-close-hard", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    close_cancelled = asyncio.Event()
    allow_close = asyncio.Event()

    async def cancellation_suppressing_close() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            close_cancelled.set()
            await allow_close.wait()

    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(
            receive=AsyncMock(return_value=UpstreamWebSocketMessage(kind="text", text='{"type":"error"}')),
            close=cancellation_suppressing_close,
        ),
    )

    async def request_replay(_session: object, _text: str) -> None:
        async with session.pending_lock:
            session.pending_requests.remove(request_state)
            session.queued_request_count = 0
        session.upstream_control.reconnect_requested = True
        session.upstream_control.replay_request_state = request_state

    monkeypatch.setattr(service, "_process_http_bridge_upstream_text", request_replay)
    monkeypatch.setattr(bridge_upstream_events, "_HTTP_BRIDGE_RECONNECT_CLOSE_OBSERVATION_SECONDS", 0.01)

    started_at = time.monotonic()
    await service._relay_http_bridge_upstream_messages(session)

    assert time.monotonic() - started_at < 0.2
    await asyncio.wait_for(close_cancelled.wait(), timeout=0.1)
    assert session.upstream_close_owned is True
    session.lifecycle_lock.acquire_nowait()
    session.lifecycle_lock.release()
    assert service._proxy_cleanup_tasks
    allow_close.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_fail_pending_websocket_requests_retries_transient_reservation_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-batch-release-retry",
        key_id="key-batch-release-retry",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-batch-release-retry",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        transport="websocket",
        skip_request_log=True,
    )
    release = AsyncMock(side_effect=[RuntimeError("transient database failure"), None])
    monkeypatch.setattr(service, "_release_websocket_reservation", release)

    await service._fail_pending_websocket_requests(
        account_id_value=None,
        pending_requests=deque([request_state]),
        pending_lock=anyio.Lock(),
        error_code="stream_incomplete",
        error_message="stream ended",
        api_key=None,
    )
    await service.close_proxy_cleanup_tasks()

    assert release.await_count == 2
    assert request_state.api_key_reservation is None


@pytest.mark.asyncio
async def test_late_successful_http_bridge_acquisition_is_closed_without_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "late-success-not-published", None)
    created_session = _make_http_bridge_session(
        key=key,
        account=cast(Any, SimpleNamespace(id="acc-late-success", status=AccountStatus.ACTIVE)),
    )
    create_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    allow_late_success = asyncio.Event()
    close_session = AsyncMock()

    async def cancellation_suppressing_create(*args: object, **kwargs: object):
        del args, kwargs
        create_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_success.wait()
        return created_session

    monkeypatch.setattr(service, "_http_bridge_should_wait_for_registration_compatible", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_prune_http_bridge_sessions_locked", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", cancellation_suppressing_create)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())
    monkeypatch.setattr(service, "_close_http_bridge_session", close_session)
    monkeypatch.setattr(proxy_service, "_http_bridge_owner_instance", AsyncMock(return_value="codex-lb"))
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(return_value=("codex-lb", ("codex-lb",))),
    )
    monkeypatch.setattr(proxy_service, "get_settings", lambda: _make_app_settings())

    with pytest.raises(ProxyResponseError):
        await service._get_or_create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key=key.affinity_key),
            api_key=None,
            request_model="gpt-5.4",
            idle_ttl_seconds=120.0,
            max_sessions=8,
            request_deadline_at=time.monotonic() + 0.02,
        )

    assert create_started.is_set()
    await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
    assert key not in service._http_bridge_sessions
    allow_late_success.set()
    await service.close_proxy_cleanup_tasks()
    close_session.assert_awaited_once_with(created_session)
    assert key not in service._http_bridge_sessions


@pytest.mark.asyncio
async def test_http_bridge_post_request_state_startup_failure_keeps_outer_reservation_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-post-state-startup-failure",
        key_id="key-post-state-startup-failure",
        model="gpt-5.4",
    )
    schedule_release = Mock()
    dashboard_settings = SimpleNamespace(
        sticky_threads_enabled=False,
        openai_cache_affinity_max_age_seconds=1800,
        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
        http_responses_session_bridge_gateway_safe_mode=False,
    )
    startup_error = ProxyResponseError(
        503,
        proxy_service.openai_error("bridge_owner_unreachable", "owner lookup unavailable"),
    )

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", AsyncMock(return_value=dashboard_settings))
    monkeypatch.setattr(service._durable_bridge, "lookup_request_targets", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_get_or_create_http_bridge_session", AsyncMock(side_effect=startup_error))
    monkeypatch.setattr(service, "_schedule_websocket_reservation_release", schedule_release)

    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-post-state-startup-failure"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
    )

    with pytest.raises(ProxyResponseError):
        await anext(stream)

    schedule_release.assert_called_once_with(
        reservation,
        reason="http-bridge-startup-proxy-failure",
    )


@pytest.mark.asyncio
async def test_owner_forward_attempt_transfers_outer_reservation_to_conditional_pre_ack_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    payload = proxy_service.ResponsesRequest.model_validate(
        {"model": "gpt-5.4", "instructions": "hi", "input": "hello"}
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-owner-pre-ack-ambiguous",
        key_id="key-owner-pre-ack-ambiguous",
        model="gpt-5.4",
    )
    unconditional_release = Mock()
    conditional_release = AsyncMock()
    dashboard_settings = SimpleNamespace(
        sticky_threads_enabled=False,
        openai_cache_affinity_max_age_seconds=1800,
        http_responses_session_bridge_prompt_cache_idle_ttl_seconds=3600,
        http_responses_session_bridge_gateway_safe_mode=False,
    )

    async def interrupted_owner_forward(*args: object, **kwargs: object):
        del args
        handoff = cast(Any, kwargs["on_reservation_handoff"])
        handoff()
        service._schedule_unclaimed_websocket_reservation_release(
            reservation,
            reason="test-owner-pre-ack-ambiguous",
        )
        raise ProxyResponseError(504, proxy_service.openai_error("upstream_request_timeout", "timed out"))
        yield ""

    monkeypatch.setattr(service, "_http_bridge_runtime_settings", lambda: _make_app_settings())
    monkeypatch.setattr(service, "_http_bridge_dashboard_settings", AsyncMock(return_value=dashboard_settings))
    monkeypatch.setattr(
        bridge_stream._HTTPBridgeStreamMixin,
        "_stream_via_http_bridge",
        interrupted_owner_forward,
    )
    monkeypatch.setattr(service, "_schedule_websocket_reservation_release", unconditional_release)
    monkeypatch.setattr(service, "_release_unclaimed_websocket_reservation", conditional_release)

    stream = service._stream_http_bridge_or_retry(
        payload,
        headers={"x-codex-session-id": "sid-owner-pre-ack-ambiguous"},
        codex_session_affinity=True,
        propagate_http_errors=True,
        openai_cache_affinity=False,
        api_key=None,
        api_key_reservation=reservation,
        suppress_text_done_events=False,
    )
    with pytest.raises(ProxyResponseError):
        await anext(stream)

    await service.close_proxy_cleanup_tasks()
    unconditional_release.assert_not_called()
    conditional_release.assert_awaited_once_with(reservation)


@pytest.mark.asyncio
async def test_http_bridge_local_handoff_owns_reservation_during_submit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-local-submit-handoff",
        key_id="key-local-submit-handoff",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-local-submit-handoff",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        request_deadline_at=time.monotonic() + 60.0,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("session_header", "local-submit-handoff", None),
        account=cast(Any, SimpleNamespace(id="acc-local-submit-handoff", status=AccountStatus.ACTIVE)),
    )
    handoff = Mock()
    settle_or_release = AsyncMock()
    monkeypatch.setattr(service, "_submit_http_bridge_request", AsyncMock(side_effect=RuntimeError("submit failed")))
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        settle_or_release,
    )
    stream = service._stream_http_bridge_session_events(
        session,
        request_state=request_state,
        text_data='{"type":"response.create","input":"hello"}',
        queue_limit=4,
        propagate_http_errors=False,
        downstream_turn_state=None,
        on_started=handoff,
    )

    with pytest.raises(RuntimeError, match="submit failed"):
        await anext(stream)

    handoff.assert_called_once_with()
    assert request_state.api_key_reservation is None
    await service.close_proxy_cleanup_tasks()
    settle_or_release.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge request detached before response.completed",
    )


@pytest.mark.asyncio
async def test_http_bridge_terminal_finalization_claims_reservation_before_queue_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-terminal-atomic-handoff",
        key_id="key-terminal-atomic-handoff",
        model="gpt-5.4",
    )
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-terminal-atomic-handoff",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        response_id="resp-terminal-atomic-handoff",
        event_queue=event_queue,
        transport="http",
        skip_request_log=True,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "terminal-atomic-handoff", None),
        account=cast(Any, SimpleNamespace(id="acc-terminal-atomic-handoff", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    settlement_started = asyncio.Event()
    allow_settlement = asyncio.Event()
    settled_reservations: list[proxy_service.ApiKeyUsageReservationData | None] = []
    schedule_release = Mock()

    async def blocked_settlement(
        _api_key: object,
        passed_reservation: proxy_service.ApiKeyUsageReservationData | None,
        *_args: object,
    ) -> bool:
        settled_reservations.append(passed_reservation)
        settlement_started.set()
        await allow_settlement.wait()
        return True

    monkeypatch.setattr(service, "_settle_stream_api_key_usage_with_fallback", blocked_settlement)
    monkeypatch.setattr(service, "_schedule_websocket_reservation_release", schedule_release)
    monkeypatch.setattr(service, "_register_http_bridge_previous_response_id", AsyncMock())
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.completed","response":{"id":"resp-terminal-atomic-handoff",'
        '"status":"completed","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}',
    )
    await asyncio.wait_for(settlement_started.wait(), timeout=0.1)

    assert request_state.api_key_reservation is None
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is not None
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    await service._detach_http_bridge_request(session, request_state=request_state)
    schedule_release.assert_not_called()
    assert settled_reservations == [reservation]

    allow_settlement.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_http_bridge_retry_terminal_cancellation_before_claim_keeps_reservation_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-retry-terminal-cancel",
        key_id="key-retry-terminal-cancel",
        model="gpt-5.4",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-retry-terminal-cancel",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        response_id="resp-retry-terminal-cancel",
        event_queue=asyncio.Queue(),
        request_text='{"type":"response.create","input":"hello"}',
        transport="http",
        skip_request_log=True,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "retry-terminal-cancel", None),
        account=cast(Any, SimpleNamespace(id="acc-retry-terminal-cancel", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    claim_acquire_started = asyncio.Event()
    never_acquire_claim = asyncio.Event()

    class BlockThirdAcquire:
        def __init__(self) -> None:
            self.acquire_count = 0

        async def __aenter__(self) -> None:
            self.acquire_count += 1
            if self.acquire_count == 3:
                claim_acquire_started.set()
                await never_acquire_claim.wait()

        async def __aexit__(self, *_args: object) -> None:
            return None

    session.pending_lock = cast(Any, BlockThirdAcquire())
    monkeypatch.setattr(
        service,
        "_classify_and_schedule_stream_error",
        Mock(
            return_value={
                "failure_class": "retryable_transient",
                "phase": "first_event",
                "error_code": "server_is_overloaded",
                "error": {"message": "overloaded"},
                "http_status": 503,
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_retry_http_bridge_request_on_fresh_upstream",
        AsyncMock(return_value=False),
    )

    process_task = asyncio.create_task(
        service._process_http_bridge_upstream_text(
            session,
            '{"type":"response.failed","response":{"id":"resp-retry-terminal-cancel",'
            '"status":"failed","error":{"type":"server_error",'
            '"code":"server_is_overloaded","message":"overloaded"}}}',
        )
    )
    await asyncio.wait_for(claim_acquire_started.wait(), timeout=0.1)
    assert request_state in session.pending_requests
    assert request_state.api_key_reservation is reservation

    process_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await process_task
    assert request_state in session.pending_requests
    assert request_state.api_key_reservation is reservation


@pytest.mark.asyncio
async def test_http_bridge_retry_false_uses_public_error_and_does_not_reclassify_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-retry-public-error",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="resp-retry-public-error",
        event_queue=asyncio.Queue(),
        request_text='{"type":"response.create","input":"hello"}',
        transport="http",
        skip_request_log=True,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "retry-public-error", None),
        account=cast(Any, SimpleNamespace(id="acc-retry-public-error", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    classify = Mock(
        return_value={
            "failure_class": "retryable_transient",
            "phase": "first_event",
            "error_code": "server_is_overloaded",
            "error": {"message": "private upstream failed"},
            "http_status": 503,
        }
    )
    handle_stream_error = AsyncMock()
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_classify_and_schedule_stream_error", classify)
    monkeypatch.setattr(
        service,
        "_retry_http_bridge_request_on_fresh_upstream",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(service, "_handle_stream_error", handle_stream_error)
    monkeypatch.setattr(service, "_settle_stream_api_key_usage_with_fallback", settle)

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"error","status":503,"response":{"id":"resp-retry-public-error"},'
        '"error":{"type":"server_error","code":"server_is_overloaded",'
        '"message":"connect to 10.0.0.8 failed from /srv/private.sock"}}',
    )
    await service.close_proxy_cleanup_tasks()

    classify.assert_called_once()
    handle_stream_error.assert_not_awaited()
    settle.assert_awaited_once()
    settlement = settle.await_args.args[2]
    assert "10.0.0.8" not in (settlement.error_message or "")
    assert "private.sock" not in (settlement.error_message or "")
    terminal = await asyncio.wait_for(request_state.event_queue.get(), timeout=0.1)
    assert terminal is not None and '"type":"response.failed"' in terminal
    assert "10.0.0.8" not in terminal
    assert "private.sock" not in terminal


@pytest.mark.asyncio
async def test_http_bridge_anonymous_authoritative_terminal_charges_sole_discarded_owner_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-anonymous-authoritative-terminal",
        key_id="key-anonymous-authoritative-terminal",
        model="gpt-5.6-sol",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-anonymous-authoritative-terminal",
        model="gpt-5.6-sol",
        service_tier="default",
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="resp-original-discarded",
        event_queue=None,
        transport="http",
        skip_request_log=True,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey(
            "prompt_cache",
            "anonymous-authoritative-terminal",
            None,
        ),
        account=cast(
            Any,
            SimpleNamespace(id="acc-anonymous-authoritative-terminal", status=AccountStatus.ACTIVE),
        ),
    )
    session.discarded_response_ids.add(request_state.response_id)
    session.discarded_request_accounting[request_state.response_id] = _DiscardedRequestAccounting(
        request_state=request_state,
        api_key_reservation=reservation,
    )
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_settle_stream_api_key_usage_with_fallback", settle)
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())
    anonymous_terminal = (
        '{"type":"response.completed","response":{"id":"","status":"completed",'
        '"usage":{"input_tokens":96,"output_tokens":4,"total_tokens":100,'
        '"input_tokens_details":{"cached_tokens":32,"cache_write_tokens":64}}}}'
    )

    await service._process_http_bridge_upstream_text(session, anonymous_terminal)
    await service._process_http_bridge_upstream_text(session, anonymous_terminal)
    await service.close_proxy_cleanup_tasks()

    settle.assert_awaited_once()
    assert settle.await_args.args[1] is reservation
    settlement = settle.await_args.args[2]
    assert settlement.input_tokens == 96
    assert settlement.output_tokens == 4
    assert settlement.cached_input_tokens == 32
    assert settlement.cache_write_tokens == 64
    assert session.discarded_response_ids == set()
    assert session.discarded_request_accounting == {}


@pytest.mark.asyncio
async def test_http_bridge_anonymous_terminal_with_two_identified_pending_retires_without_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    first_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-http-anonymous-ambiguous-a",
        key_id="key-http-anonymous-ambiguous",
        model="gpt-5.6-sol",
    )
    second_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-http-anonymous-ambiguous-b",
        key_id="key-http-anonymous-ambiguous",
        model="gpt-5.6-sol",
    )
    first_queue: asyncio.Queue[str | None] = asyncio.Queue()
    second_queue: asyncio.Queue[str | None] = asyncio.Queue()
    first_state = proxy_service._WebSocketRequestState(
        request_id="req-http-anonymous-ambiguous-a",
        model="gpt-5.6-sol",
        service_tier="default",
        reasoning_effort="medium",
        api_key_reservation=first_reservation,
        started_at=1.0,
        response_id="resp-http-anonymous-ambiguous-a",
        event_queue=first_queue,
        transport="http",
    )
    second_state = proxy_service._WebSocketRequestState(
        request_id="req-http-anonymous-ambiguous-b",
        model="gpt-5.6-sol",
        service_tier="default",
        reasoning_effort="medium",
        api_key_reservation=second_reservation,
        started_at=2.0,
        response_id="resp-http-anonymous-ambiguous-b",
        event_queue=second_queue,
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey(
            "prompt_cache",
            "http-anonymous-ambiguous",
            None,
        ),
        account=cast(
            Any,
            SimpleNamespace(id="acc-http-anonymous-ambiguous", status=AccountStatus.ACTIVE),
        ),
    )
    session.pending_requests.extend([first_state, second_state])
    session.queued_request_count = 2
    schedule_finalization = Mock()
    monkeypatch.setattr(
        service,
        "_schedule_websocket_request_finalization",
        schedule_finalization,
    )

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.completed","response":{"id":"","status":"completed",'
        '"output":[{"type":"message","id":"wrong-owner-marker"}],'
        '"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}',
    )

    schedule_finalization.assert_not_called()
    assert list(session.pending_requests) == [first_state, second_state]
    assert session.queued_request_count == 2
    assert first_state.api_key_reservation is first_reservation
    assert second_state.api_key_reservation is second_reservation
    assert first_queue.empty()
    assert second_queue.empty()
    assert session.upstream_control.retire_ambiguous_transport is True


@pytest.mark.asyncio
async def test_http_bridge_anonymous_terminal_with_discarded_tombstone_and_live_sibling_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    discarded_response_id = "resp-http-anonymous-tombstone"
    discarded_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-http-anonymous-tombstone",
        key_id="key-http-anonymous-tombstone",
        model="gpt-5.6-sol",
    )
    discarded_state = proxy_service._WebSocketRequestState(
        request_id="req-http-anonymous-tombstone",
        model="gpt-5.6-sol",
        service_tier="default",
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=1.0,
        response_id=discarded_response_id,
        event_queue=None,
        transport="http",
    )
    sibling_queue: asyncio.Queue[str | None] = asyncio.Queue()
    sibling_state = proxy_service._WebSocketRequestState(
        request_id="req-http-anonymous-tombstone-sibling",
        model="gpt-5.6-sol",
        service_tier="default",
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=2.0,
        response_id="resp-http-anonymous-tombstone-sibling",
        event_queue=sibling_queue,
        transport="http",
    )
    accounting = _DiscardedRequestAccounting(
        request_state=discarded_state,
        api_key_reservation=discarded_reservation,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey(
            "prompt_cache",
            "http-anonymous-tombstone",
            None,
        ),
        account=cast(
            Any,
            SimpleNamespace(id="acc-http-anonymous-tombstone", status=AccountStatus.ACTIVE),
        ),
    )
    session.pending_requests.append(sibling_state)
    session.queued_request_count = 1
    session.discarded_response_ids.add(discarded_response_id)
    session.discarded_request_accounting[discarded_response_id] = accounting
    schedule_finalization = Mock()
    monkeypatch.setattr(
        service,
        "_schedule_websocket_request_finalization",
        schedule_finalization,
    )

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.completed","response":{"id":"","status":"completed",'
        '"output":[{"type":"message","id":"wrong-owner-marker"}],'
        '"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}',
    )

    schedule_finalization.assert_not_called()
    assert list(session.pending_requests) == [sibling_state]
    assert session.queued_request_count == 1
    assert sibling_queue.empty()
    assert session.discarded_response_ids == {discarded_response_id}
    assert session.discarded_request_accounting == {discarded_response_id: accounting}
    assert accounting.api_key_reservation is discarded_reservation
    assert session.upstream_control.retire_ambiguous_transport is True


@pytest.mark.asyncio
async def test_http_bridge_close_transferred_pending_terminal_is_delivered_without_false_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-close-transferred-terminal",
        key_id="key-close-transferred-terminal",
        model="gpt-5.6-sol",
    )
    event_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-close-transferred-terminal",
        model="gpt-5.6-sol",
        service_tier="default",
        reasoning_effort="medium",
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        response_id="resp-close-transferred-terminal",
        event_queue=event_queue,
        transport="http",
        http_bridge_send_started_at=time.monotonic(),
        skip_request_log=True,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "close-transferred-terminal", None),
        account=cast(Any, SimpleNamespace(id="acc-close-transferred-terminal", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_settle_stream_api_key_usage_with_fallback", settle)
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())

    await service._transfer_submitted_http_bridge_pending_accounting(session)
    assert request_state.api_key_reservation is None
    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.completed","response":{"id":"resp-close-transferred-terminal",'
        '"status":"completed","usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}',
    )
    await service._close_http_bridge_session(session)
    await service.close_proxy_cleanup_tasks()

    terminal = await asyncio.wait_for(event_queue.get(), timeout=0.1)
    assert terminal is not None and '"type":"response.completed"' in terminal
    assert await asyncio.wait_for(event_queue.get(), timeout=0.1) is None
    assert event_queue.empty()
    settle.assert_awaited_once()
    assert settle.await_args.args[1] is reservation
    assert request_state not in session.pending_requests
    assert session.discarded_request_accounting == {}


@pytest.mark.asyncio
async def test_http_bridge_close_transferred_anonymous_created_restores_intended_pending_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-close-transferred-created",
        key_id="key-close-transferred-created",
        model="gpt-5.6-sol",
    )
    intended_queue: asyncio.Queue[str | None] = asyncio.Queue()
    sibling_queue: asyncio.Queue[str | None] = asyncio.Queue()
    intended_state = proxy_service._WebSocketRequestState(
        request_id="req-close-transferred-created",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        awaiting_response_created=True,
        event_queue=intended_queue,
        transport="http",
        http_bridge_send_started_at=time.monotonic(),
        skip_request_log=True,
    )
    sibling_state = proxy_service._WebSocketRequestState(
        request_id="req-close-transferred-created-sibling",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=time.monotonic(),
        awaiting_response_created=True,
        event_queue=sibling_queue,
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "close-transferred-created", None),
        account=cast(Any, SimpleNamespace(id="acc-close-transferred-created", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.extend([intended_state, sibling_state])
    session.queued_request_count = 2
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_settle_stream_api_key_usage_with_fallback", settle)
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())

    await service._transfer_submitted_http_bridge_pending_accounting(session)
    accounting = session.anonymous_discarded_request_accounting[intended_state.request_id]
    resolution_future = asyncio.get_running_loop().create_future()
    accounting.resolution_future = resolution_future
    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.created","response":{"id":"resp-close-transferred-created",'
        '"status":"in_progress"}}',
    )

    assert intended_state.response_id == "resp-close-transferred-created"
    assert sibling_state.response_id is None
    created = await asyncio.wait_for(intended_queue.get(), timeout=0.1)
    assert created is not None and '"type":"response.created"' in created
    assert sibling_queue.empty()
    assert intended_state.api_key_reservation is None
    assert session.anonymous_discarded_request_accounting == {}
    rebound_accounting = session.discarded_request_accounting[
        "resp-close-transferred-created"
    ]
    assert rebound_accounting is accounting
    assert rebound_accounting.api_key_reservation is reservation
    assert resolution_future.done() is False

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.completed","response":{"id":"resp-close-transferred-created",'
        '"status":"completed","usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
    )

    completed = await asyncio.wait_for(intended_queue.get(), timeout=0.1)
    assert completed is not None and '"type":"response.completed"' in completed
    assert await asyncio.wait_for(intended_queue.get(), timeout=0.1) is None
    assert resolution_future.done()
    settle.assert_not_awaited()
    resolution = resolution_future.result()
    assert callable(resolution)
    await resolution()
    settle.assert_awaited_once()
    assert settle.await_args.args[1] is reservation


@pytest.mark.asyncio
async def test_http_bridge_late_response_created_binds_sole_anonymous_discard_before_sibling() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    discarded_state = proxy_service._WebSocketRequestState(
        request_id="req-late-created-discarded",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=None,
        transport="http",
    )
    sibling_queue: asyncio.Queue[str | None] = asyncio.Queue()
    sibling_state = proxy_service._WebSocketRequestState(
        request_id="req-late-created-sibling",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        awaiting_response_created=True,
        event_queue=sibling_queue,
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "late-created-discarded", None),
        account=cast(Any, SimpleNamespace(id="acc-late-created-discarded", status=AccountStatus.ACTIVE)),
    )
    session.pending_requests.append(sibling_state)
    session.queued_request_count = 1
    session.anonymous_discarded_request_accounting[discarded_state.request_id] = (
        _DiscardedRequestAccounting(
            request_state=discarded_state,
            api_key_reservation=None,
        )
    )

    await service._process_http_bridge_upstream_text(
        session,
        '{"type":"response.created","response":{"id":"resp-late-created",'
        '"status":"in_progress"}}',
    )

    assert sibling_state.response_id is None
    assert sibling_queue.empty()
    assert session.anonymous_discarded_request_accounting == {}
    assert session.discarded_response_ids == {"resp-late-created"}
    assert session.discarded_request_accounting["resp-late-created"].request_state is discarded_state


@pytest.mark.asyncio
async def test_http_bridge_cancelled_receive_reconciles_suppressed_terminal_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    cancellation_observed = asyncio.Event()

    async def cancellation_suppressing_receive() -> UpstreamWebSocketMessage:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observed.set()
            return UpstreamWebSocketMessage(
                kind="text",
                text=(
                    '{"type":"response.completed","response":{"id":"resp-cancelled-receive",'
                    '"status":"completed","usage":{"input_tokens":2,"output_tokens":1,'
                    '"total_tokens":3}}}'
                ),
            )

    request_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-cancelled-receive",
        model="gpt-5.4",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="resp-cancelled-receive",
        event_queue=request_queue,
        transport="http",
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "cancelled-receive", None),
        account=cast(Any, SimpleNamespace(id="acc-cancelled-receive", status=AccountStatus.ACTIVE)),
    )
    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=cancellation_suppressing_receive, close=AsyncMock()),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    timeout_call_count = 0

    async def receive_timeout(*_args: object, **_kwargs: object) -> _WebSocketReceiveTimeout:
        nonlocal timeout_call_count
        timeout_call_count += 1
        return _WebSocketReceiveTimeout(
            timeout_seconds=0.01 if timeout_call_count == 1 else 0.0,
            error_code="stream_idle_timeout",
            error_message="timed out",
            fail_all_pending=True,
        )

    schedule_finalization = Mock()
    monkeypatch.setattr(service, "_next_websocket_receive_timeout", receive_timeout)
    monkeypatch.setattr(
        service,
        "_schedule_websocket_request_finalization",
        schedule_finalization,
    )
    monkeypatch.setattr(
        service,
        "_evict_http_bridge_session_after_upstream_disconnect",
        AsyncMock(),
    )

    await asyncio.wait_for(service._relay_http_bridge_upstream_messages(session), timeout=0.2)

    assert cancellation_observed.is_set()
    schedule_finalization.assert_called_once()
    terminal = await asyncio.wait_for(request_queue.get(), timeout=0.1)
    assert terminal is not None and '"type":"response.completed"' in terminal
    assert await asyncio.wait_for(request_queue.get(), timeout=0.1) is None


@pytest.mark.asyncio
async def test_http_bridge_detached_receive_retains_submitted_reservation_until_late_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="res-detached-receive-late-terminal",
        key_id="key-detached-receive-late-terminal",
        model="gpt-5.6-sol",
    )
    allow_late_terminal = asyncio.Event()
    receive_cancelled = asyncio.Event()

    async def cancellation_suppressing_receive() -> UpstreamWebSocketMessage:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            receive_cancelled.set()
            await allow_late_terminal.wait()
            return UpstreamWebSocketMessage(
                kind="text",
                text=(
                    '{"type":"response.completed","response":{"id":"",'
                    '"status":"completed","usage":{"input_tokens":8,"output_tokens":2,'
                    '"total_tokens":10}}}'
                ),
            )

    public_queue: asyncio.Queue[str | None] = asyncio.Queue()
    request_state = proxy_service._WebSocketRequestState(
        request_id="req-detached-receive-late-terminal",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        event_queue=public_queue,
        request_text='{"type":"response.create","input":"hello"}',
        transport="http",
        http_bridge_send_started_at=time.monotonic(),
        skip_request_log=True,
    )
    session = _make_http_bridge_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "detached-receive-late-terminal", None),
        account=cast(
            Any,
            SimpleNamespace(id="acc-detached-receive-late-terminal", status=AccountStatus.ACTIVE),
        ),
    )
    session.upstream = cast(
        UpstreamResponsesWebSocket,
        SimpleNamespace(receive=cancellation_suppressing_receive, close=AsyncMock()),
    )
    session.pending_requests.append(request_state)
    session.queued_request_count = 1
    original_await_cancelled_task = bridge_upstream_events._await_cancelled_task

    async def detach_receive_owner(
        task: asyncio.Task[Any],
        *,
        timeout_seconds: float = 1.0,
        label: str,
    ) -> bool:
        if label == "HTTP bridge upstream receive":
            task.cancel()
            await asyncio.wait_for(receive_cancelled.wait(), timeout=0.1)
            return False
        return await original_await_cancelled_task(
            task,
            timeout_seconds=timeout_seconds,
            label=label,
        )

    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(bridge_upstream_events, "_await_cancelled_task", detach_receive_owner)
    monkeypatch.setattr(
        service,
        "_next_websocket_receive_timeout",
        AsyncMock(
            return_value=_WebSocketReceiveTimeout(
                timeout_seconds=0.01,
                error_code="stream_idle_timeout",
                error_message="timed out",
                fail_all_pending=True,
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_evict_http_bridge_session_after_upstream_disconnect",
        AsyncMock(),
    )
    monkeypatch.setattr(service, "_settle_stream_api_key_usage_with_fallback", settle)
    monkeypatch.setattr(service._load_balancer, "record_success", AsyncMock())

    await service._relay_http_bridge_upstream_messages(session)

    accounting = session.anonymous_discarded_request_accounting[request_state.request_id]
    assert accounting.api_key_reservation is reservation
    settle.assert_not_awaited()
    failed = await asyncio.wait_for(public_queue.get(), timeout=0.1)
    assert failed is not None and '"type":"response.failed"' in failed
    assert await asyncio.wait_for(public_queue.get(), timeout=0.1) is None

    allow_late_terminal.set()
    await service._close_http_bridge_session(session)
    await service.close_proxy_cleanup_tasks()

    settle.assert_awaited_once()
    assert settle.await_args.args[1] is reservation
    settlement = settle.await_args.args[2]
    assert settlement.input_tokens == 8
    assert settlement.output_tokens == 2
    assert session.anonymous_discarded_request_accounting == {}
