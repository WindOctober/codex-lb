from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket, UpstreamWebSocketMessage
from app.db.models import AccountStatus
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import keys as bridge_keys
from app.modules.proxy._service.http_bridge import lifecycle as bridge_lifecycle
from app.modules.proxy._service.support import _DiscardedRequestAccounting

pytestmark = pytest.mark.unit


def _make_discarded_session(
    *,
    key: proxy_service._HTTPBridgeSessionKey,
    response_id: str,
) -> tuple[
    proxy_service._HTTPBridgeSession,
    proxy_service._WebSocketRequestState,
    proxy_service.ApiKeyUsageReservationData,
]:
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id=f"reservation-{response_id}",
        key_id=f"key-{response_id}",
        model="gpt-5.6-sol",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id=f"request-{response_id}",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=time.monotonic() - 10.0,
        response_id=response_id,
        event_queue=asyncio.Queue(),
        transport="http",
    )
    session = proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(key=key.affinity_key),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="account-discarded", status=AccountStatus.ACTIVE)),
        upstream=cast(
            UpstreamResponsesWebSocket,
            SimpleNamespace(close=AsyncMock()),
        ),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=0.0,
        idle_ttl_seconds=0.01,
    )
    session.discarded_response_ids.add(response_id)
    session.discarded_request_accounting[response_id] = _DiscardedRequestAccounting(
        request_state=request_state,
        api_key_reservation=reservation,
    )
    return session, request_state, reservation


@pytest.mark.asyncio
async def test_discarded_accounting_remains_busy_for_capacity_and_idle_paths() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    base_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-capacity", None)
    session, _request_state, _reservation = _make_discarded_session(
        key=base_key,
        response_id="response-discarded-capacity",
    )
    service._http_bridge_sessions[base_key] = session
    capacity_service = cast(Any, service)

    assert await capacity_service._http_bridge_pending_count(session) == 1
    assert await capacity_service._http_bridge_replacement_busy_count(session) == 1

    async with service._http_bridge_lock:
        assert await service._prune_http_bridge_sessions_locked() == []
        with pytest.raises(ProxyResponseError) as exc_info:
            await capacity_service._reserve_http_bridge_creation_slot_locked(
                key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "new-capacity", None),
                max_sessions=1,
                request_model="gpt-5.6-sol",
            )
        selected_shard = await capacity_service._select_http_bridge_soft_shard_key_locked(
            base_key,
            api_key=None,
            request_model="gpt-5.6-sol",
            pending_limit=1,
            max_shards=2,
        )

    assert exc_info.value.status_code == 429
    assert service._http_bridge_sessions[base_key] is session
    assert selected_shard == bridge_keys._http_bridge_soft_shard_key(base_key, 1)

    parallel_key = bridge_keys._http_bridge_busy_parallel_key(base_key, 1)
    parallel_session, _parallel_state, _parallel_reservation = _make_discarded_session(
        key=parallel_key,
        response_id="response-discarded-parallel",
    )
    service._http_bridge_sessions[parallel_key] = parallel_session
    assert capacity_service._select_http_bridge_busy_parallel_key_locked(base_key, max_sessions=2) is None


@pytest.mark.asyncio
async def test_close_waits_for_reader_to_reconcile_buffered_discarded_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, request_state, reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-reader-close", None),
        response_id="response-discarded-reader-close",
    )
    finalization = AsyncMock()
    fallback_settlement = AsyncMock()
    reader_started = asyncio.Event()

    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalization)
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        fallback_settlement,
    )

    async def buffered_terminal_reader() -> None:
        reader_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await service._process_http_bridge_upstream_text(
                session,
                '{"type":"response.completed","response":'
                '{"id":"response-discarded-reader-close","status":"completed",'
                '"usage":{"input_tokens":8,"output_tokens":2,"total_tokens":10}}}',
            )

    session.upstream_reader = asyncio.create_task(buffered_terminal_reader())
    await asyncio.wait_for(reader_started.wait(), timeout=0.2)

    await asyncio.wait_for(service._close_http_bridge_session(session), timeout=0.5)

    finalization.assert_awaited_once()
    assert finalization.call_args.args[0] is request_state
    assert finalization.call_args.kwargs["api_key_reservation"] is reservation
    fallback_settlement.assert_not_awaited()
    assert session.discarded_request_accounting == {}
    assert session.discarded_response_ids == set()


@pytest.mark.asyncio
async def test_close_processes_detached_receive_before_discarded_fallback_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, request_state, reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-detached-close", None),
        response_id="response-discarded-detached-close",
    )
    finalization = AsyncMock()
    fallback_settlement = AsyncMock()
    receive_started = asyncio.Event()
    release_receive = asyncio.Event()

    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalization)
    monkeypatch.setattr(
        service,
        "_settle_or_release_failed_websocket_reservation",
        fallback_settlement,
    )

    async def buffered_detached_receive() -> UpstreamWebSocketMessage:
        receive_started.set()
        await release_receive.wait()
        text = (
            '{"type":"response.failed","response":'
            '{"id":"response-discarded-detached-close","status":"failed",'
            '"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6},'
            '"error":{"code":"server_error","message":"late failure"}}}'
        )
        return UpstreamWebSocketMessage(kind="text", text=text, raw_text=text)

    session.detached_upstream_receive = asyncio.create_task(buffered_detached_receive())
    await asyncio.wait_for(receive_started.wait(), timeout=0.2)
    close_task = asyncio.create_task(service._close_http_bridge_session(session))
    await asyncio.sleep(0)

    assert close_task.done() is False
    assert session.discarded_request_accounting
    fallback_settlement.assert_not_awaited()

    release_receive.set()
    await asyncio.wait_for(close_task, timeout=0.5)

    finalization.assert_awaited_once()
    assert finalization.call_args.args[0] is request_state
    assert finalization.call_args.kwargs["api_key_reservation"] is reservation
    fallback_settlement.assert_not_awaited()
    assert session.detached_upstream_receive is None
    assert session.discarded_request_accounting == {}


@pytest.mark.asyncio
async def test_close_cancellation_hands_suppressing_reader_to_tracked_reconciler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, request_state, reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-suppressing-close", None),
        response_id="response-discarded-suppressing-close",
    )
    reader_cancelled = asyncio.Event()
    release_reader = asyncio.Event()
    finalization = AsyncMock()
    fallback = AsyncMock()
    monkeypatch.setattr(
        bridge_lifecycle,
        "_HTTP_BRIDGE_CLOSE_RECEIVE_OBSERVATION_TIMEOUT_SECONDS",
        0.2,
    )
    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalization)
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", fallback)

    async def cancellation_suppressing_reader() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            reader_cancelled.set()
            while not release_reader.is_set():
                try:
                    await release_reader.wait()
                except asyncio.CancelledError:
                    reader_cancelled.set()
            await service._process_http_bridge_upstream_text(
                session,
                '{"type":"response.completed","response":'
                '{"id":"response-discarded-suppressing-close","status":"completed",'
                '"usage":{"input_tokens":9,"output_tokens":1,"total_tokens":10}}}',
            )

    session.upstream_reader = asyncio.create_task(cancellation_suppressing_reader())
    close_task = asyncio.create_task(service._close_http_bridge_session(session))
    await asyncio.wait_for(reader_cancelled.wait(), timeout=0.1)

    started_at = time.monotonic()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close_task, timeout=0.2)
    assert time.monotonic() - started_at < 0.2

    reconciliation_task = next(
        task
        for task in service._proxy_cleanup_tasks
        if task.get_name().startswith("http-bridge-discarded-reconcile-")
    )
    fallback.assert_not_awaited()
    reconciliation_task.cancel()
    await asyncio.sleep(0)
    assert reconciliation_task.done() is False
    assert session.discarded_request_accounting
    fallback.assert_not_awaited()

    release_reader.set()
    await asyncio.wait_for(reconciliation_task, timeout=0.2)
    finalization.assert_awaited_once()
    assert finalization.call_args.args[0] is request_state
    assert finalization.call_args.kwargs["api_key_reservation"] is reservation
    fallback.assert_not_awaited()
    assert session.discarded_request_accounting == {}
    cast(Any, session.upstream.close).assert_awaited_once()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_tracked_reconciler_preserves_accounting_until_suppressing_reader_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, request_state, reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-late-usage", None),
        response_id="response-discarded-late-usage",
    )
    reader_cancelled = asyncio.Event()
    release_reader = asyncio.Event()
    finalization = AsyncMock()
    fallback = AsyncMock()

    monkeypatch.setattr(
        bridge_lifecycle,
        "_HTTP_BRIDGE_CLOSE_RECEIVE_OBSERVATION_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalization)
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", fallback)

    async def close_upstream() -> None:
        upstream_reader = session.upstream_reader
        if upstream_reader is not None and not upstream_reader.done():
            upstream_reader.cancel()

    upstream_close = AsyncMock(side_effect=close_upstream)
    session.upstream = cast(UpstreamResponsesWebSocket, SimpleNamespace(close=upstream_close))

    async def late_terminal_reader() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            reader_cancelled.set()
            await release_reader.wait()
            await service._process_http_bridge_upstream_text(
                session,
                '{"type":"response.completed","response":'
                '{"id":"response-discarded-late-usage","status":"completed",'
                '"usage":{"input_tokens":11,"output_tokens":2,"total_tokens":13}}}',
            )

    session.upstream_reader = asyncio.create_task(late_terminal_reader())
    started_at = time.monotonic()
    await asyncio.wait_for(service._close_http_bridge_session(session), timeout=0.2)
    assert time.monotonic() - started_at < 0.2
    await asyncio.wait_for(reader_cancelled.wait(), timeout=0.1)

    await asyncio.sleep(0.03)
    assert session.discarded_request_accounting
    fallback.assert_not_awaited()
    upstream_close.assert_not_awaited()

    release_reader.set()
    await service.close_proxy_cleanup_tasks()

    finalization.assert_awaited_once()
    assert finalization.call_args.args[0] is request_state
    assert finalization.call_args.kwargs["api_key_reservation"] is reservation
    fallback.assert_not_awaited()
    assert session.discarded_request_accounting == {}
    upstream_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_handoff_recovers_reconciler_cancelled_before_first_task_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, request_state, reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-prestart-cancel", None),
        response_id="response-discarded-prestart-cancel",
    )
    release_reader = asyncio.Event()
    reader_started = asyncio.Event()
    reader_cancelled = asyncio.Event()
    finalization = AsyncMock()
    fallback = AsyncMock()

    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalization)
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", fallback)

    async def late_terminal_reader() -> None:
        reader_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            reader_cancelled.set()
            await release_reader.wait()
            await service._process_http_bridge_upstream_text(
                session,
                '{"type":"response.completed","response":'
                '{"id":"response-discarded-prestart-cancel","status":"completed",'
                '"usage":{"input_tokens":6,"output_tokens":1,"total_tokens":7}}}',
            )

    session.upstream_reader = asyncio.create_task(late_terminal_reader())
    await asyncio.wait_for(reader_started.wait(), timeout=0.1)
    session.upstream_reader.cancel()
    await asyncio.wait_for(reader_cancelled.wait(), timeout=0.1)

    lifecycle_service = cast(Any, service)
    await lifecycle_service._handoff_http_bridge_discarded_accounting_reconciliation(session)
    first_owner = next(
        task
        for task in service._proxy_cleanup_tasks
        if task.get_name().startswith("http-bridge-discarded-reconcile-")
    )
    first_owner.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    successor = next(
        task
        for task in service._proxy_cleanup_tasks
        if task is not first_owner
        and task.get_name().startswith("http-bridge-discarded-reconcile-")
    )
    assert successor.done() is False
    assert session.discarded_request_accounting
    fallback.assert_not_awaited()

    release_reader.set()
    await service.close_proxy_cleanup_tasks()

    finalization.assert_awaited_once()
    assert finalization.call_args.args[0] is request_state
    assert finalization.call_args.kwargs["api_key_reservation"] is reservation
    fallback.assert_not_awaited()
    cast(Any, session.upstream.close).assert_awaited_once()


@pytest.mark.asyncio
async def test_guardian_cancellation_settles_fallback_before_late_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, request_state, reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey(
            "prompt_cache",
            "discarded-guardian-cancel",
            None,
        ),
        response_id="response-discarded-guardian-cancel",
    )
    release_reader = asyncio.Event()
    reader_started = asyncio.Event()
    finalization = AsyncMock()
    fallback = AsyncMock()

    monkeypatch.setattr(service, "_finalize_websocket_request_state", finalization)
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", fallback)

    async def late_terminal_reader() -> None:
        reader_started.set()
        await release_reader.wait()
        await service._process_http_bridge_upstream_text(
            session,
            '{"type":"response.completed","response":'
            '{"id":"response-discarded-guardian-cancel","status":"completed",'
            '"usage":{"input_tokens":6,"output_tokens":1,"total_tokens":7}}}',
        )

    session.upstream_reader = asyncio.create_task(late_terminal_reader())
    await asyncio.wait_for(reader_started.wait(), timeout=0.1)

    lifecycle_service = cast(Any, service)
    await lifecycle_service._handoff_http_bridge_discarded_accounting_reconciliation(session)
    await asyncio.sleep(0)
    guardian = next(
        task
        for task in service._proxy_cleanup_tasks
        if task.get_name().startswith("http-bridge-discarded-accounting-guardian-")
    )

    guardian.cancel()
    await asyncio.wait_for(guardian, timeout=0.2)

    fallback.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge session closed before discarded response completed",
    )
    finalization.assert_not_awaited()

    release_reader.set()
    await service.close_proxy_cleanup_tasks()

    fallback.assert_awaited_once()
    finalization.assert_not_awaited()
    assert session.discarded_request_accounting == {}
    cast(Any, session.upstream.close).assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciler_enrolls_all_claimed_settlements_before_any_can_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, first_state, _first_reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-multi-enroll", None),
        response_id="response-discarded-first",
    )
    second_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-discarded-second",
        key_id="key-discarded-second",
        model="gpt-5.6-sol",
    )
    second_state = proxy_service._WebSocketRequestState(
        request_id="request-discarded-second",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=time.monotonic(),
        response_id="response-discarded-second",
        transport="http",
    )
    session.discarded_response_ids.add("response-discarded-second")
    session.discarded_request_accounting["response-discarded-second"] = (
        _DiscardedRequestAccounting(
            request_state=second_state,
            api_key_reservation=second_reservation,
        )
    )
    release_first_settlement = asyncio.Event()
    first_settlement_started = asyncio.Event()
    second_settlement_started = asyncio.Event()
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()

    async def settle_discarded(*, request_state: object, **_kwargs: object) -> None:
        if request_state is first_state:
            first_settlement_started.set()
            await release_first_settlement.wait()
        elif request_state is second_state:
            second_settlement_started.set()

    settlement = AsyncMock(side_effect=settle_discarded)
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", settlement)
    original_claim = service._claim_http_bridge_discarded_accounting

    async def blocked_claim(
        claim_session: proxy_service._HTTPBridgeSession,
        *,
        owner_already_acquired: bool = False,
    ):
        claim_started.set()
        await release_claim.wait()
        return await cast(Any, original_claim)(
            claim_session,
            owner_already_acquired=owner_already_acquired,
        )

    monkeypatch.setattr(service, "_claim_http_bridge_discarded_accounting", blocked_claim)

    lifecycle_service = cast(Any, service)
    await lifecycle_service._handoff_http_bridge_discarded_accounting_reconciliation(session)
    reconciliation_task = next(
        task
        for task in service._proxy_cleanup_tasks
        if task.get_name().startswith("http-bridge-discarded-reconcile-")
    )
    await asyncio.wait_for(claim_started.wait(), timeout=0.1)
    reconciliation_task.cancel()
    release_claim.set()
    await asyncio.wait_for(reconciliation_task, timeout=0.2)

    await asyncio.wait_for(first_settlement_started.wait(), timeout=0.1)
    await asyncio.wait_for(second_settlement_started.wait(), timeout=0.1)
    assert settlement.await_count == 2
    assert session.discarded_request_accounting == {}

    release_first_settlement.set()
    await service.close_proxy_cleanup_tasks()


@pytest.mark.asyncio
async def test_discarded_fallback_claim_atomically_drains_known_and_anonymous_maps() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session, known_state, _known_reservation = _make_discarded_session(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "discarded-claim", None),
        response_id="response-known-claim",
    )
    anonymous_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-anonymous-claim",
        key_id="key-anonymous-claim",
        model="gpt-5.6-sol",
    )
    anonymous_state = proxy_service._WebSocketRequestState(
        request_id="request-anonymous-claim",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=None,
        started_at=time.monotonic(),
        transport="http",
    )
    session.anonymous_discarded_request_accounting[anonymous_state.request_id] = (
        _DiscardedRequestAccounting(
            request_state=anonymous_state,
            api_key_reservation=anonymous_reservation,
        )
    )

    lifecycle_service = cast(Any, service)
    claimed = await lifecycle_service._claim_http_bridge_discarded_accounting(session)

    assert {accounting.request_state.request_id for accounting in claimed} == {
        known_state.request_id,
        anonymous_state.request_id,
    }
    assert session.discarded_request_accounting == {}
    assert session.anonymous_discarded_request_accounting == {}
    assert await lifecycle_service._claim_http_bridge_discarded_accounting(session) == ()


@pytest.mark.asyncio
async def test_close_waits_for_inflight_submit_lock_before_sealing_and_transferring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    upstream_close = AsyncMock()
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "close-submit-race", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="close-submit-race"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="account-close-submit-race", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=upstream_close)),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-close-submit-race",
        key_id="key-close-submit-race",
        model="gpt-5.6-sol",
    )
    request_state = proxy_service._WebSocketRequestState(
        request_id="request-close-submit-race",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=reservation,
        started_at=time.monotonic(),
        response_id="response-close-submit-race",
        http_bridge_send_started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
        skip_request_log=True,
    )
    settlement = AsyncMock()
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", settlement)

    await session.lifecycle_lock.acquire()
    close_task = asyncio.create_task(service._close_http_bridge_session(session))
    await asyncio.sleep(0)
    assert close_task.done() is False
    assert session.closed is False

    async with session.pending_lock:
        session.pending_requests.append(request_state)
        session.queued_request_count = 1
    session.lifecycle_lock.release()

    await asyncio.wait_for(close_task, timeout=0.2)
    await service.close_proxy_cleanup_tasks()

    settlement.assert_awaited_once_with(
        request_state=request_state,
        reservation=reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="HTTP bridge session closed before discarded response completed",
    )
    assert request_state.api_key_reservation is None
    assert session.pending_requests == deque()
    upstream_close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_response_id", ["response-submitted-close", None])
async def test_scheduled_close_transfers_submitted_accounting_before_public_failure(
    monkeypatch: pytest.MonkeyPatch,
    submitted_response_id: str | None,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    submitted_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-submitted-close",
        key_id="key-submitted-close",
        model="gpt-5.6-sol",
    )
    never_sent_reservation = proxy_service.ApiKeyUsageReservationData(
        reservation_id="reservation-never-sent-close",
        key_id="key-never-sent-close",
        model="gpt-5.6-sol",
    )
    submitted_queue: asyncio.Queue[str | None] = asyncio.Queue()
    never_sent_queue: asyncio.Queue[str | None] = asyncio.Queue()
    submitted_state = proxy_service._WebSocketRequestState(
        request_id="request-submitted-close",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=submitted_reservation,
        started_at=time.monotonic(),
        response_id=submitted_response_id,
        http_bridge_send_started_at=time.monotonic(),
        event_queue=submitted_queue,
        transport="http",
        skip_request_log=True,
    )
    never_sent_state = proxy_service._WebSocketRequestState(
        request_id="request-never-sent-close",
        model="gpt-5.6-sol",
        service_tier=None,
        reasoning_effort="medium",
        api_key_reservation=never_sent_reservation,
        started_at=time.monotonic(),
        event_queue=never_sent_queue,
        transport="http",
        skip_request_log=True,
    )
    session = proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "scheduled-close-transfer", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(key="scheduled-close-transfer"),
        request_model="gpt-5.6-sol",
        account=cast(Any, SimpleNamespace(id="account-scheduled-close", status=AccountStatus.ACTIVE)),
        upstream=cast(UpstreamResponsesWebSocket, SimpleNamespace(close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque([submitted_state, never_sent_state]),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=2,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )
    finalization = Mock()
    fallback = AsyncMock()
    reader_started = asyncio.Event()
    late_response_id = submitted_response_id or "response-anonymous-submitted-close"

    monkeypatch.setattr(service, "_schedule_websocket_request_finalization", finalization)
    monkeypatch.setattr(service, "_settle_or_release_failed_websocket_reservation", fallback)

    async def buffered_terminal_reader() -> None:
        reader_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await service._process_http_bridge_upstream_text(
                session,
                '{"type":"response.completed","response":'
                f'{{"id":"{late_response_id}","status":"completed",'
                '"usage":{"input_tokens":7,"output_tokens":1,"total_tokens":8}}}',
            )

    session.upstream_reader = asyncio.create_task(buffered_terminal_reader())
    await asyncio.wait_for(reader_started.wait(), timeout=0.1)
    service._schedule_http_bridge_session_close(
        session,
        reason="test-accounting-transfer",
        error_code="stream_incomplete",
        error_message="scheduled close public failure",
    )
    await asyncio.gather(*tuple(service._http_bridge_background_close_tasks))
    await service.close_proxy_cleanup_tasks()

    finalization.assert_called_once()
    assert finalization.call_args.args[0] is submitted_state
    assert finalization.call_args.kwargs["api_key_reservation"] is submitted_reservation
    assert finalization.call_args.kwargs["reservation_preclaimed"] is True
    fallback.assert_awaited_once_with(
        request_state=never_sent_state,
        reservation=never_sent_reservation,
        api_key=None,
        error_code="stream_incomplete",
        error_message="scheduled close public failure",
    )
    assert submitted_state.api_key_reservation is None
    assert session.discarded_request_accounting == {}
    assert session.anonymous_discarded_request_accounting == {}
    submitted_failure = await asyncio.wait_for(submitted_queue.get(), timeout=0.1)
    never_sent_failure = await asyncio.wait_for(never_sent_queue.get(), timeout=0.1)
    assert submitted_failure is not None and "scheduled close public failure" in submitted_failure
    assert never_sent_failure is not None and "scheduled close public failure" in never_sent_failure
