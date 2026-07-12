from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest

from app.core.config.settings import Settings
from app.db.models import AccountStatus
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import runtime_collection
from app.modules.proxy._service.support import _WebSocketRequestState


def _request_state(
    request_id: str,
    *,
    response_id: str | None = None,
    previous_response_id: str | None = None,
) -> _WebSocketRequestState:
    return _WebSocketRequestState(
        request_id=request_id,
        model="gpt-test",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic() - 1.0,
        response_id=response_id,
        previous_response_id=previous_response_id,
        event_queue=asyncio.Queue(),
        transport="http",
    )


def _session(
    *,
    key: str,
    account_id: str,
    pending_requests: deque[_WebSocketRequestState] | None = None,
    queued_request_count: int = 0,
    codex_session: bool = False,
    reconnect_requested: bool = False,
    closed: bool = False,
) -> proxy_service._HTTPBridgeSession:
    upstream_control = proxy_service._WebSocketUpstreamControl()
    upstream_control.reconnect_requested = reconnect_requested
    return proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", key, None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key=key),
        request_model="gpt-test",
        account=cast(
            Any,
            SimpleNamespace(
                id=account_id,
                email=f"{account_id}@example.com",
                status=AccountStatus.ACTIVE,
            ),
        ),
        upstream=cast(Any, SimpleNamespace()),
        upstream_control=upstream_control,
        pending_requests=pending_requests or deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=queued_request_count,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
        codex_session=codex_session,
        closed=closed,
    )


@pytest.mark.asyncio
async def test_request_status_prefers_direct_request_id_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    previous_match = _request_state("request-previous", previous_response_id="target")
    direct_match = _request_state("target", response_id="response-direct")
    session = _session(
        key="status",
        account_id="account-1",
        pending_requests=deque([previous_match, direct_match]),
        queued_request_count=2,
    )
    service._http_bridge_sessions[session.key] = session
    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings(stream_idle_timeout_seconds=480))

    assert await service.get_http_bridge_request_status("   ") is None
    snapshot = await service.get_http_bridge_request_status("target")

    assert snapshot is not None
    assert snapshot.matched_by == "request_id"
    assert snapshot.matched_request_id == "target"
    assert snapshot.matched_response_id == "response-direct"
    assert snapshot.pending_request_count == 2
    assert snapshot.queued_request_count == 2


@pytest.mark.asyncio
async def test_account_runtime_snapshot_aggregates_live_sessions() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    busy = _session(
        key="busy",
        account_id="account-1",
        pending_requests=deque([_request_state("request-1")]),
        queued_request_count=2,
        codex_session=True,
    )
    reconnecting = _session(
        key="reconnecting",
        account_id="account-1",
        queued_request_count=1,
        reconnect_requested=True,
    )
    closed = _session(key="closed", account_id="account-1", closed=True)
    service._http_bridge_sessions = {
        busy.key: busy,
        reconnecting.key: reconnecting,
        closed.key: closed,
    }

    snapshots = await service.get_http_bridge_account_runtime_snapshot()

    assert snapshots == {
        "account-1": proxy_service.HTTPBridgeAccountRuntimeSnapshot(
            account_id="account-1",
            sessions=2,
            pending_requests=1,
            queued_requests=3,
            busy_sessions=1,
            codex_sessions=1,
            reconnect_requested_sessions=1,
        )
    }


class _FailingRepoContext:
    async def __aenter__(self) -> Any:
        raise RuntimeError("database unavailable")

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.asyncio
async def test_runtime_health_snapshot_falls_back_when_repository_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    service._repo_factory = cast(Any, lambda: _FailingRepoContext())
    monkeypatch.setattr(runtime_collection.time, "monotonic", lambda: 10.5)

    snapshot = await service._http_bridge_runtime_health_snapshot(snapshot_started_at=10.0)

    assert snapshot.endpoint_ping_ms == 500
    assert snapshot.status == "unknown"
    assert snapshot.success_count == 0
    assert snapshot.request_count == 0
    assert snapshot.history == []


@pytest.mark.asyncio
async def test_capacity_snapshot_falls_back_to_observed_accounts() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    service._repo_factory = cast(Any, lambda: _FailingRepoContext())

    snapshot = await service._http_bridge_account_model_capacity_snapshot(
        active_session_counts_by_account={"account-1": 2},
        idle_session_counts_by_account={"account-1": 1},
        account_model_session_limit=20,
    )

    assert snapshot.account_model_session_capacity == 20
    assert snapshot.free_account_model_session_slots == 18
    assert snapshot.reclaimable_idle_sessions == 1
    assert snapshot.available_parallel_capacity == 19


def test_egress_snapshot_uses_configured_route_before_runtime_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_collection, "get_upstream_egress_runtime", lambda: None)
    settings = Settings(
        upstream_egress_mode="proxy",
        upstream_proxy_url="http://127.0.0.1:7897",
    )

    snapshot = runtime_collection._upstream_egress_runtime_snapshot(now=10.0, settings=settings)

    assert snapshot.mode == "proxy"
    assert snapshot.selected_route == "proxy"
    assert snapshot.proxy_configured is True
    assert snapshot.direct_ok is None
    assert snapshot.proxy_ok is None
