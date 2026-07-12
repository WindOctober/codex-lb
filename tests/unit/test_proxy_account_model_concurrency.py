from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import anyio
import pytest

from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.db.models import AccountStatus
from app.modules.proxy import service as proxy_service
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLimiter
from app.modules.proxy.load_balancer import AccountSelection

pytestmark = pytest.mark.unit


def _request_state(request_id: str) -> proxy_service._WebSocketRequestState:
    return proxy_service._WebSocketRequestState(
        request_id=request_id,
        model="gpt-5.5",
        service_tier=None,
        reasoning_effort=None,
        api_key_reservation=None,
        started_at=time.monotonic(),
        event_queue=asyncio.Queue(),
        transport="http",
    )


def _bridge_session() -> proxy_service._HTTPBridgeSession:
    return proxy_service._HTTPBridgeSession(
        key=proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="cache-key",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.5",
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        upstream=cast(Any, SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=deque(),
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=0,
        last_used_at=time.monotonic(),
        idle_ttl_seconds=120.0,
    )


class _FakeSettingsCache:
    async def get(self) -> SimpleNamespace:
        return SimpleNamespace(
            prefer_earlier_reset_accounts=False,
            routing_strategy="high_waterline",
            sticky_reallocation_budget_threshold_pct=85.0,
        )


def _pressure_bridge_session(
    key: proxy_service._HTTPBridgeSessionKey,
    *,
    account_id: str,
    created_at: float,
    last_used_at: float,
    pending: bool = False,
    codex_session: bool = True,
) -> proxy_service._HTTPBridgeSession:
    pending_requests: deque[proxy_service._WebSocketRequestState] = deque()
    if pending:
        pending_requests.append(_request_state(f"pending-{key.affinity_key}"))
    return proxy_service._HTTPBridgeSession(
        key=key,
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key=key.affinity_key,
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        request_model="gpt-5.5",
        account=cast(Any, SimpleNamespace(id=account_id, status=AccountStatus.ACTIVE)),
        upstream=cast(Any, SimpleNamespace(send_text=AsyncMock(), close=AsyncMock())),
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        pending_requests=pending_requests,
        pending_lock=anyio.Lock(),
        response_create_gate=None,
        queued_request_count=1 if pending else 0,
        last_used_at=last_used_at,
        idle_ttl_seconds=120.0,
        created_at=created_at,
        codex_session=codex_session,
        last_completed_response_id=f"resp-{key.affinity_key}" if codex_session else None,
    )


def test_account_model_concurrency_limiter_enforces_limit_and_releases_once() -> None:
    limiter = AccountModelConcurrencyLimiter()

    first = limiter.try_acquire(account_id="acc-1", model="gpt-5.5", limit=1)
    assert first is not None
    assert limiter.try_acquire(account_id="acc-1", model="gpt-5.5", limit=1) is None
    other_model = limiter.try_acquire(account_id="acc-1", model="gpt-5", limit=1)
    assert other_model is not None
    assert limiter.full_account_ids(model="gpt-5.5", limit=1) == {"acc-1"}

    first.release()
    first.release()
    other_model.release()

    assert limiter.active_count(account_id="acc-1", model="gpt-5.5") == 0
    reacquired = limiter.try_acquire(account_id="acc-1", model="gpt-5.5", limit=1)
    assert reacquired is not None
    reacquired.release()


def test_http_bridge_busy_parallel_selects_idle_existing_slot_when_pool_full() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    base_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None, strength="soft")
    service._http_bridge_sessions[base_key] = _pressure_bridge_session(
        base_key,
        account_id="acc-base",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
        pending=True,
    )
    idle_parallel_key = proxy_service._http_bridge_busy_parallel_key(base_key, 1)
    service._http_bridge_sessions[idle_parallel_key] = _pressure_bridge_session(
        idle_parallel_key,
        account_id="acc-idle",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
    )

    selected = service._select_http_bridge_busy_parallel_key_locked(
        base_key,
        max_sessions=2,
        allow_soft_prompt_cache=True,
    )

    assert selected == idle_parallel_key


def test_http_bridge_allows_soft_prompt_cache_parallel_for_first_turn_batch() -> None:
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None, strength="soft")
    session = _pressure_bridge_session(
        key,
        account_id="acc-base",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
        pending=True,
        codex_session=False,
    )

    assert (
        proxy_service._http_bridge_soft_prompt_cache_busy_parallel_allowed(
            key=key,
            session=session,
            incoming_turn_state=None,
            previous_response_id=None,
        )
        is True
    )


def test_http_bridge_previous_response_recovery_handles_owner_unavailable() -> None:
    exc = ProxyResponseError(
        502,
        proxy_service.openai_error(
            "upstream_unavailable",
            "Previous response owner account is unavailable; retry later.",
            error_type="server_error",
        ),
    )

    assert proxy_service._http_bridge_should_attempt_local_previous_response_recovery(exc) is True


@pytest.mark.asyncio
async def test_select_account_excludes_locally_full_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    lease = service._account_model_concurrency.try_acquire(account_id="acc-full", model="gpt-5.5", limit=1)
    assert lease is not None
    captured: list[dict[str, object]] = []

    class FakeLoadBalancer:
        async def select_account(self, **kwargs: object) -> AccountSelection:
            captured.append(dict(kwargs))
            if len(captured) == 2:
                account = cast(Any, SimpleNamespace(id="acc-full", status=AccountStatus.ACTIVE))
                return AccountSelection(account=account, error_message=None)
            return AccountSelection(account=None, error_message="No active accounts available")

    class FakeSettingsCache:
        async def get(self) -> SimpleNamespace:
            return SimpleNamespace(sticky_reallocation_budget_threshold_pct=85.0)

    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings(proxy_account_model_concurrency_limit=1))
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: FakeSettingsCache())
    service._load_balancer = cast(Any, FakeLoadBalancer())

    selection = await service._select_account_with_budget(
        time.monotonic() + 5.0,
        request_id="req-select",
        kind="http_bridge",
        model="gpt-5.5",
    )

    assert captured[0]["exclude_account_ids"] == {"acc-full"}
    assert captured[1]["exclude_account_ids"] == set()
    assert selection.account is None
    assert selection.error_code == "proxy_overloaded"

    lease.release()


@pytest.mark.asyncio
async def test_select_account_selected_excluded_account_reports_local_overload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    lease = service._account_model_concurrency.try_acquire(account_id="acc-full", model="gpt-5.5", limit=1)
    assert lease is not None

    class FakeLoadBalancer:
        async def select_account(self, **_kwargs: object) -> AccountSelection:
            account = cast(Any, SimpleNamespace(id="acc-full", status=AccountStatus.ACTIVE))
            return AccountSelection(account=account, error_message=None)

    class FakeSettingsCache:
        async def get(self) -> SimpleNamespace:
            return SimpleNamespace(sticky_reallocation_budget_threshold_pct=85.0)

    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings(proxy_account_model_concurrency_limit=1))
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: FakeSettingsCache())
    service._load_balancer = cast(Any, FakeLoadBalancer())

    try:
        selection = await service._select_account_with_budget(
            time.monotonic() + 5.0,
            request_id="req-select-excluded",
            kind="http_bridge",
            model="gpt-5.5",
        )
    finally:
        lease.release()

    assert selection.account is None
    assert selection.error_code == "proxy_overloaded"
    assert selection.error_message == "All eligible accounts are at the local account/model concurrency budget"


@pytest.mark.asyncio
async def test_select_account_excludes_http_bridge_connect_full_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    lease = service._account_model_concurrency.try_acquire(account_id="acc-full", model="gpt-5.5", limit=1)
    assert lease is not None
    captured: list[dict[str, object]] = []

    class FakeLoadBalancer:
        async def select_account(self, **kwargs: object) -> AccountSelection:
            captured.append(dict(kwargs))
            if len(captured) == 2:
                account = cast(Any, SimpleNamespace(id="acc-full", status=AccountStatus.ACTIVE))
                return AccountSelection(account=account, error_message=None)
            return AccountSelection(account=None, error_message="No active accounts available")

    class FakeSettingsCache:
        async def get(self) -> SimpleNamespace:
            return SimpleNamespace(sticky_reallocation_budget_threshold_pct=85.0)

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_account_model_concurrency_limit=32,
            proxy_http_bridge_account_model_connect_limit=1,
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: FakeSettingsCache())
    service._load_balancer = cast(Any, FakeLoadBalancer())

    selection = await service._select_account_with_budget(
        time.monotonic() + 5.0,
        request_id="req-select-connect",
        kind="http_bridge",
        model="gpt-5.5",
    )

    assert captured[0]["exclude_account_ids"] == {"acc-full"}
    assert captured[1]["exclude_account_ids"] == set()
    assert selection.account is None
    assert selection.error_code == "proxy_overloaded"

    lease.release()


@pytest.mark.asyncio
async def test_select_account_allows_held_http_bridge_session_account_when_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    held = service._http_bridge_account_model_sessions.try_acquire(account_id="acc-held", model="gpt-5.5", limit=1)
    assert held is not None
    captured: list[dict[str, object]] = []
    account = cast(Any, SimpleNamespace(id="acc-held", status=AccountStatus.ACTIVE))

    class FakeLoadBalancer:
        async def select_account(self, **kwargs: object) -> AccountSelection:
            captured.append(dict(kwargs))
            if "acc-held" in cast(set[str], kwargs.get("exclude_account_ids", set())):
                return AccountSelection(account=None, error_message="No active accounts available")
            return AccountSelection(account=account, error_message=None)

    class FakeSettingsCache:
        async def get(self) -> SimpleNamespace:
            return SimpleNamespace(sticky_reallocation_budget_threshold_pct=85.0)

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(proxy_http_bridge_account_model_session_limit=1),
    )
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: FakeSettingsCache())
    service._load_balancer = cast(Any, FakeLoadBalancer())

    try:
        selection = await service._select_account_with_budget(
            time.monotonic() + 5.0,
            request_id="req-select-held",
            kind="http_bridge",
            model="gpt-5.5",
            held_http_bridge_session_account_id="acc-held",
        )
    finally:
        held.release()

    assert captured[0]["exclude_account_ids"] == set()
    assert selection.account is account


@pytest.mark.asyncio
async def test_http_bridge_session_waits_for_recoverable_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE))
    select_calls = 0

    async def select_account(*_args: object, **_kwargs: object) -> AccountSelection:
        nonlocal select_calls
        select_calls += 1
        if select_calls == 1:
            return AccountSelection(
                account=None,
                error_message="Rate limit exceeded. Try again in 1s",
                error_code=proxy_service._ACCOUNT_SELECTION_RECOVERABLE_WAIT_CODE,
                retry_after_seconds=0.05,
            )
        return AccountSelection(account=account, error_message=None, error_code=None)

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_http_bridge_account_model_session_limit=1,
            proxy_http_bridge_account_model_connect_limit=2,
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
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=account))
    monkeypatch.setattr(
        service,
        "_open_upstream_websocket_with_budget",
        AsyncMock(return_value=cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))),
    )
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    create_task = asyncio.create_task(
        service._create_http_bridge_session(
            proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None),
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-key"),
            api_key=None,
            request_model="gpt-5.5",
            idle_ttl_seconds=120.0,
        )
    )
    await asyncio.sleep(0.02)
    assert create_task.done() is False

    session = await asyncio.wait_for(create_task, timeout=1.0)

    assert select_calls >= 2
    assert session.account is account
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_http_bridge_pending_requests_hold_and_release_account_model_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _bridge_session()
    service._http_bridge_sessions[session.key] = session
    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings(proxy_account_model_concurrency_limit=1))

    session.submit_lease_count = 1
    first = _request_state("req-1")
    await service._submit_http_bridge_request(
        session,
        request_state=first,
        text_data='{"type":"response.create"}',
        queue_limit=0,
    )

    assert service._account_model_concurrency.active_count(account_id="acc-1", model="gpt-5.5") == 1
    assert session.submit_lease_count == 0

    second = _request_state("req-2")
    with pytest.raises(ProxyResponseError) as exc_info:
        await service._submit_http_bridge_request(
            session,
            request_state=second,
            text_data='{"type":"response.create"}',
            queue_limit=0,
        )

    assert exc_info.value.status_code == 429
    assert service._account_model_concurrency.active_count(account_id="acc-1", model="gpt-5.5") == 1

    assert await service._detach_http_bridge_request(session, request_state=first) is True
    assert service._account_model_concurrency.active_count(account_id="acc-1", model="gpt-5.5") == 0


@pytest.mark.asyncio
async def test_http_bridge_recreates_cached_session_when_request_budget_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("sticky_thread", "thread-key", None, strength="soft")
    existing = _pressure_bridge_session(
        key,
        account_id="acc-full",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
    )
    service._http_bridge_sessions[key] = existing
    held = service._account_model_concurrency.try_acquire(account_id="acc-full", model="gpt-5.5", limit=1)
    assert held is not None

    async def create_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **_kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        return _pressure_bridge_session(
            create_key,
            account_id="acc-next",
            created_at=time.monotonic(),
            last_used_at=time.monotonic(),
        )

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(proxy_account_model_concurrency_limit=1),
    )
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", create_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())

    try:
        selected = await service._get_or_create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="thread-key"),
            api_key=None,
            request_model="gpt-5.5",
            idle_ttl_seconds=120.0,
            max_sessions=0,
        )
    finally:
        held.release()

    assert selected.account.id == "acc-next"
    assert existing.closed is True
    assert service._http_bridge_sessions[key] is selected


@pytest.mark.asyncio
async def test_http_bridge_stale_replacement_preserves_handed_out_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None, strength="soft")
    existing = _pressure_bridge_session(
        key,
        account_id="acc-existing",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
        codex_session=True,
    )
    existing.submit_lease_count = 1
    service._http_bridge_sessions[key] = existing
    held = service._account_model_concurrency.try_acquire(
        account_id="acc-existing",
        model="gpt-5.5",
        limit=1,
    )
    assert held is not None
    created_keys: list[proxy_service._HTTPBridgeSessionKey] = []

    async def create_session(
        create_key: proxy_service._HTTPBridgeSessionKey,
        **_kwargs: object,
    ) -> proxy_service._HTTPBridgeSession:
        created_keys.append(create_key)
        return _pressure_bridge_session(
            create_key,
            account_id="acc-next",
            created_at=time.monotonic(),
            last_used_at=time.monotonic(),
        )

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_account_model_concurrency_limit=1,
            http_responses_session_bridge_soft_shard_max_shards=4,
        ),
    )
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _FakeSettingsCache())
    monkeypatch.setattr(
        proxy_service,
        "_http_bridge_owner_instance",
        AsyncMock(return_value=Settings().http_responses_session_bridge_instance_id),
    )
    monkeypatch.setattr(
        proxy_service,
        "_active_http_bridge_instance_ring",
        AsyncMock(
            return_value=(
                Settings().http_responses_session_bridge_instance_id,
                (Settings().http_responses_session_bridge_instance_id,),
            )
        ),
    )
    monkeypatch.setattr(service, "_create_http_bridge_session_compatible", create_session)
    monkeypatch.setattr(service, "_claim_durable_http_bridge_session", AsyncMock())

    try:
        selected = await service._get_or_create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(
                key="cache-key",
                kind=proxy_service.StickySessionKind.PROMPT_CACHE,
            ),
            api_key=None,
            request_model="gpt-5.5",
            idle_ttl_seconds=120.0,
            max_sessions=0,
        )
    finally:
        held.release()

    assert selected.account.id == "acc-next"
    assert selected.key != key
    assert created_keys == [selected.key]
    assert existing.closed is False
    assert service._http_bridge_sessions[key] is existing
    assert service._http_bridge_sessions[selected.key] is selected


@pytest.mark.asyncio
async def test_http_bridge_reconnect_failovers_after_proxy_response_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _bridge_session()
    old_account = cast(Any, SimpleNamespace(id="acc-old", status=AccountStatus.ACTIVE))
    new_account = cast(Any, SimpleNamespace(id="acc-new", status=AccountStatus.ACTIVE))
    session.account = old_account
    session.upstream = cast(Any, SimpleNamespace(close=AsyncMock()))
    selected_accounts: list[str] = []
    opened_accounts: list[str] = []

    async def select_account(*_args: object, **kwargs: object) -> AccountSelection:
        excluded = cast(set[str], kwargs.get("exclude_account_ids", set()))
        preferred = kwargs.get("preferred_account_id")
        if preferred == "acc-old" and "acc-old" not in excluded:
            selected_accounts.append("acc-old")
            return AccountSelection(account=old_account, error_message=None)
        if "acc-old" not in excluded:
            selected_accounts.append("acc-old")
            return AccountSelection(account=old_account, error_message=None)
        selected_accounts.append("acc-new")
        return AccountSelection(account=new_account, error_message=None)

    async def open_upstream(
        account: object,
        _headers: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> object:
        del timeout_seconds
        account_id = cast(Any, account).id
        opened_accounts.append(account_id)
        if account_id == "acc-old":
            raise ProxyResponseError(
                502,
                proxy_service.openai_error("upstream_unavailable", "[Errno 104] Connection reset by peer"),
            )
        return SimpleNamespace(close=AsyncMock())

    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings())
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _FakeSettingsCache())
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(side_effect=lambda account, **_kw: account))
    monkeypatch.setattr(service, "_open_upstream_websocket_with_budget", open_upstream)
    monkeypatch.setattr(
        service,
        "_handle_websocket_connect_error",
        AsyncMock(return_value={"failure_class": "retryable_transient"}),
    )

    await service._reconnect_http_bridge_session(
        session,
        request_state=_request_state("req-reconnect"),
        prefer_same_account=True,
    )

    assert selected_accounts == ["acc-old", "acc-old", "acc-new"]
    assert opened_accounts == ["acc-old", "acc-old", "acc-new"]
    assert session.account is new_account
    assert session.closed is False


@pytest.mark.asyncio
async def test_http_bridge_reconnect_reacquires_missing_same_account_session_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _bridge_session()
    account = session.account
    session.account_model_session_lease = None
    selected_accounts: list[str] = []

    async def select_account(*_args: object, **kwargs: object) -> AccountSelection:
        del kwargs
        selected_accounts.append(account.id)
        return AccountSelection(account=account, error_message=None)

    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings())
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _FakeSettingsCache())
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=account))
    monkeypatch.setattr(
        service,
        "_open_upstream_websocket_with_budget",
        AsyncMock(return_value=SimpleNamespace(close=AsyncMock())),
    )

    await service._reconnect_http_bridge_session(
        session,
        request_state=_request_state("req-reconnect-lease"),
        prefer_same_account=True,
    )

    assert selected_accounts == [account.id]
    assert session.account_model_session_lease is not None
    assert service._http_bridge_account_model_sessions.active_count(account_id=account.id, model="gpt-5.5") == 1


@pytest.mark.asyncio
async def test_http_bridge_preferred_owner_waits_for_session_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    owner = cast(Any, SimpleNamespace(id="acc-owner", status=AccountStatus.ACTIVE))
    other = cast(Any, SimpleNamespace(id="acc-other", status=AccountStatus.ACTIVE))
    held = service._http_bridge_account_model_sessions.try_acquire(
        account_id=owner.id,
        model="gpt-5.5",
        limit=1,
    )
    assert held is not None
    selected_accounts: list[str] = []
    sleep_calls = 0

    async def select_account(*_args: object, **kwargs: object) -> AccountSelection:
        excluded = cast(set[str], kwargs.get("exclude_account_ids", set()))
        preferred = kwargs.get("preferred_account_id")
        if preferred == owner.id and owner.id not in excluded:
            selected_accounts.append(owner.id)
            return AccountSelection(account=owner, error_message=None)
        selected_accounts.append(other.id)
        return AccountSelection(account=other, error_message=None)

    async def release_capacity_sleep(_seconds: float) -> None:
        nonlocal sleep_calls, held
        sleep_calls += 1
        if held is not None:
            held.release()
            held = None

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(proxy_http_bridge_account_model_session_limit=1),
    )
    monkeypatch.setattr(proxy_service, "get_settings_cache", lambda: _FakeSettingsCache())
    monkeypatch.setattr(proxy_service.asyncio, "sleep", release_capacity_sleep)
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=owner))
    monkeypatch.setattr(
        service,
        "_open_upstream_websocket_with_budget",
        AsyncMock(return_value=SimpleNamespace(close=AsyncMock())),
    )
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", AsyncMock())

    session = await service._create_http_bridge_session_compatible(
        proxy_service._HTTPBridgeSessionKey("prompt_cache", "owner-key", None, strength="soft"),
        headers={},
        affinity=proxy_service._AffinityPolicy(
            key="owner-key",
            kind=proxy_service.StickySessionKind.PROMPT_CACHE,
        ),
        api_key=None,
        request_model="gpt-5.5",
        idle_ttl_seconds=120.0,
        request_stage="reattach",
        preferred_account_id=owner.id,
        require_preferred_account=True,
    )

    assert sleep_calls == 1
    assert selected_accounts == [owner.id, owner.id]
    assert session.account is owner
    assert service._http_bridge_account_model_sessions.active_count(account_id=owner.id, model="gpt-5.5") == 1
    await service._close_http_bridge_session(session)


@pytest.mark.asyncio
async def test_http_bridge_submit_restores_unregistered_session_after_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _bridge_session()
    session.closed = True
    session.account_model_session_lease = None
    state = _request_state("req-restore-reconnect")
    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings())
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock())
    monkeypatch.setattr(service, "_retry_http_bridge_request_on_fresh_upstream", AsyncMock(return_value=True))

    await service._submit_http_bridge_request(
        session,
        request_state=state,
        text_data='{"type":"response.create"}',
        queue_limit=0,
    )

    assert service._http_bridge_sessions[session.key] is session
    assert session.account_model_session_lease is not None
    assert session.upstream.send_text.await_count == 1
    assert state in session.pending_requests


@pytest.mark.asyncio
async def test_http_bridge_submit_waits_for_lifecycle_close_before_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _bridge_session()
    session.closed = True
    session.account_model_session_lease = None
    state = _request_state("req-restore-after-close")
    retry_calls = 0

    async def retry_reconnect(*_args: object, **_kwargs: object) -> bool:
        nonlocal retry_calls
        retry_calls += 1
        return True

    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings())
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock())
    monkeypatch.setattr(service, "_retry_http_bridge_request_on_fresh_upstream", retry_reconnect)

    await session.lifecycle_lock.acquire()
    submit_task = asyncio.create_task(
        service._submit_http_bridge_request(
            session,
            request_state=state,
            text_data='{"type":"response.create"}',
            queue_limit=0,
        )
    )
    await asyncio.sleep(0)
    assert retry_calls == 0
    assert not submit_task.done()

    session.lifecycle_lock.release()
    await submit_task

    assert retry_calls == 1
    assert service._http_bridge_sessions[session.key] is session
    assert session.account_model_session_lease is not None
    assert session.upstream.send_text.await_count == 1


@pytest.mark.asyncio
async def test_http_bridge_restore_replaces_idle_hard_key_conflict() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("session_header", "session-key", None, strength="hard")
    existing = _pressure_bridge_session(
        key,
        account_id="acc-existing",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
    )
    restored = _pressure_bridge_session(
        key,
        account_id="acc-restored",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
    )
    service._http_bridge_sessions[key] = existing

    failure = await service._restore_http_bridge_session_after_reconnect(
        restored,
        request_id="req-hard-restore",
    )

    assert failure is None
    assert existing.closed is True
    assert service._http_bridge_sessions[key] is restored
    assert restored.account_model_session_lease is not None


@pytest.mark.asyncio
async def test_http_bridge_submit_allows_detached_soft_prompt_cache_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    session = _bridge_session()
    replacement = _pressure_bridge_session(
        session.key,
        account_id="acc-replacement",
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
    )
    service._http_bridge_sessions[session.key] = replacement
    state = _request_state("req-detached-soft")
    monkeypatch.setattr(proxy_service, "get_settings", lambda: Settings())
    monkeypatch.setattr(service, "_evict_http_bridge_pressure", AsyncMock())

    await service._submit_http_bridge_request(
        session,
        request_state=state,
        text_data='{"type":"response.create"}',
        queue_limit=0,
    )

    assert service._http_bridge_sessions[session.key] is replacement
    assert session.upstream.send_text.await_count == 1
    assert state in session.pending_requests


@pytest.mark.asyncio
async def test_finalize_websocket_request_state_releases_account_model_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    monkeypatch.setattr(service, "_settle_stream_api_key_usage", AsyncMock())
    service._load_balancer = cast(Any, SimpleNamespace(record_success=AsyncMock()))
    request_state = _request_state("req-finalize")
    request_state.skip_request_log = True
    lease = service._account_model_concurrency.try_acquire(account_id="acc-1", model="gpt-5.5", limit=1)
    assert lease is not None
    request_state.account_model_concurrency = lease

    await service._finalize_websocket_request_state(
        request_state,
        account=cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE)),
        account_id_value="acc-1",
        event=None,
        event_type="response.completed",
        payload=None,
        api_key=None,
        upstream_control=proxy_service._WebSocketUpstreamControl(),
        response_create_gate=None,
    )

    assert request_state.account_model_concurrency is None
    assert service._account_model_concurrency.active_count(account_id="acc-1", model="gpt-5.5") == 0


@pytest.mark.asyncio
async def test_http_bridge_pressure_evicts_idle_parallel_prompt_cache_batch() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "protected", None, strength="soft")
    busy_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "busy", None, strength="soft")
    hard_key = proxy_service._HTTPBridgeSessionKey("turn_state_header", "turn-state", None, strength="hard")
    settings = Settings(
        proxy_http_bridge_account_model_session_limit=20,
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=16,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(85):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"batch-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=f"acc-{index % 5}",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )
    service._http_bridge_sessions[protected_key] = _pressure_bridge_session(
        protected_key,
        account_id="acc-0",
        created_at=now - 45.0,
        last_used_at=now - 40.0,
    )
    service._http_bridge_sessions[busy_key] = _pressure_bridge_session(
        busy_key,
        account_id="acc-1",
        created_at=now - 45.0,
        last_used_at=now - 40.0,
        pending=True,
    )
    service._http_bridge_sessions[hard_key] = _pressure_bridge_session(
        hard_key,
        account_id="acc-2",
        created_at=now - 45.0,
        last_used_at=now - 40.0,
    )

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=0,
            protected_key=protected_key,
            request_model="gpt-5.5",
        )

    assert len(evicted) == 10
    assert protected_key in service._http_bridge_sessions
    assert busy_key in service._http_bridge_sessions
    assert hard_key in service._http_bridge_sessions
    assert all(session.key.affinity_kind == "prompt_cache" for session in evicted)
    assert all(session.key.strength == "soft" for session in evicted)
    assert all(not session.pending_requests for session in evicted)


@pytest.mark.asyncio
async def test_http_bridge_pressure_preserves_handed_out_prompt_cache_session() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "new", None, strength="soft")
    leased_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "leased", None, strength="soft")
    settings = Settings(
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=16,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(90):
        key = (
            leased_key
            if index == 0
            else proxy_service._HTTPBridgeSessionKey("prompt_cache", f"batch-{index}", None, strength="soft")
        )
        session = _pressure_bridge_session(
            key,
            account_id=f"acc-{index % 5}",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )
        if key == leased_key:
            session.submit_lease_count = 1
        service._http_bridge_sessions[key] = session

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=100,
            protected_key=protected_key,
            request_model="gpt-5.5",
        )

    assert len(evicted) == 12
    assert leased_key in service._http_bridge_sessions
    assert all(session.key != leased_key for session in evicted)


@pytest.mark.asyncio
async def test_http_bridge_pressure_uses_routable_capacity_hint() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "protected", None, strength="soft")
    settings = Settings(
        proxy_http_bridge_account_model_session_limit=20,
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=16,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(128):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"pool-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=f"acc-{index % 8}",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=0,
            protected_key=protected_key,
            request_model="gpt-5.5",
            capacity_hint=220,
        )

    assert evicted == []
    assert len(service._http_bridge_sessions) == 128


@pytest.mark.asyncio
async def test_http_bridge_pressure_scopes_current_count_to_capacity_hint_accounts() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "protected", None, strength="soft")
    scoped_account_id = "acc-scoped"
    settings = Settings(
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=1,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(12):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"scoped-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=scoped_account_id,
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )
    for index in range(128):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"other-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=f"acc-other-{index % 8}",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=0,
            protected_key=protected_key,
            request_model="gpt-5.5",
            capacity_hint=proxy_service._HTTPBridgePressureCapacityHint(
                capacity=20,
                account_ids=frozenset({scoped_account_id}),
            ),
        )

    assert evicted == []
    assert len(service._http_bridge_sessions) == 140


@pytest.mark.asyncio
async def test_http_bridge_pressure_evicts_only_scoped_capacity_hint_accounts() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "protected", None, strength="soft")
    scoped_account_id = "acc-scoped"
    settings = Settings(
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=1,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(18):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"scoped-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=scoped_account_id,
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )
    for index in range(64):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"other-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=f"acc-other-{index % 8}",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=0,
            protected_key=protected_key,
            request_model="gpt-5.5",
            capacity_hint=proxy_service._HTTPBridgePressureCapacityHint(
                capacity=20,
                account_ids=frozenset({scoped_account_id}),
            ),
        )

    assert len(evicted) == 4
    assert {session.account.id for session in evicted} == {scoped_account_id}
    assert sum(1 for session in service._http_bridge_sessions.values() if session.account.id == scoped_account_id) == 14
    assert sum(1 for session in service._http_bridge_sessions.values() if session.account.id != scoped_account_id) == 64


@pytest.mark.asyncio
async def test_http_bridge_pressure_ignores_small_prompt_cache_batches() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "new", None, strength="soft")
    settings = Settings(
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=16,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(10):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"small-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id="acc-1",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=12,
            protected_key=protected_key,
            request_model="gpt-5.5",
        )

    assert evicted == []
    assert len(service._http_bridge_sessions) == 10


@pytest.mark.asyncio
async def test_http_bridge_pressure_evicts_dispersed_idle_prompt_cache_pool() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "new", None, strength="soft")
    settings = Settings(
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=16,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(90):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"dispersed-{index}", None, strength="soft")
        service._http_bridge_sessions[key] = _pressure_bridge_session(
            key,
            account_id=f"acc-{index % 5}",
            created_at=now - 45.0 - (index % 15) * 120.0,
            last_used_at=now - 40.0,
        )

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=100,
            protected_key=protected_key,
            request_model="gpt-5.5",
        )

    assert len(evicted) == 12
    assert len(service._http_bridge_sessions) == 78
    assert all(session.key.affinity_kind == "prompt_cache" for session in evicted)
    assert all(not session.pending_requests for session in evicted)


@pytest.mark.asyncio
async def test_http_bridge_pressure_preserves_interactive_prompt_cache_sessions() -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    now = time.monotonic()
    protected_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "new", None, strength="soft")
    settings = Settings(
        http_responses_session_bridge_pressure_eviction_enabled=True,
        http_responses_session_bridge_pressure_eviction_threshold_percent=80.0,
        http_responses_session_bridge_pressure_eviction_batch_window_seconds=120.0,
        http_responses_session_bridge_pressure_eviction_min_batch_sessions=16,
        http_responses_session_bridge_pressure_eviction_min_idle_seconds=10.0,
    )

    for index in range(90):
        key = proxy_service._HTTPBridgeSessionKey("prompt_cache", f"batch-{index}", None, strength="soft")
        session = _pressure_bridge_session(
            key,
            account_id=f"acc-{index % 5}",
            created_at=now - 45.0,
            last_used_at=now - 40.0,
        )
        session.client_kind = "interactive" if index < 20 else "batch"
        service._http_bridge_sessions[key] = session

    async with service._http_bridge_lock:
        evicted = await service._evict_http_bridge_parallel_prompt_cache_pressure_locked(
            settings=settings,
            max_sessions=100,
            protected_key=protected_key,
            request_model="gpt-5.5",
        )

    assert len(evicted) == 12
    assert all(session.client_kind == "batch" for session in evicted)
    assert sum(1 for session in service._http_bridge_sessions.values() if session.client_kind == "interactive") == 20


def test_http_bridge_client_kind_detects_vscode_and_exec() -> None:
    soft_key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None, strength="soft")
    hard_key = proxy_service._HTTPBridgeSessionKey("session_header", "session-id", None, strength="hard")

    assert proxy_service._http_bridge_client_kind({"user-agent": "codex-vscode/1.0"}, key=soft_key) == "interactive"
    assert proxy_service._http_bridge_client_kind({"user-agent": "codex exec"}, key=soft_key) == "batch"
    assert proxy_service._http_bridge_client_kind({}, key=soft_key) == "batch"
    assert proxy_service._http_bridge_client_kind({}, key=hard_key) == "interactive"


@pytest.mark.asyncio
async def test_http_bridge_connect_budget_waits_until_slot_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE))
    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(proxy_http_bridge_account_model_connect_limit=1),
    )
    held = service._account_model_concurrency.try_acquire(account_id="acc-1", model="gpt-5.5", limit=1)
    assert held is not None

    acquire_task = asyncio.create_task(
        service._acquire_http_bridge_connect_account_model_concurrency(
            account=account,
            model="gpt-5.5",
            request_id="req-connect",
            deadline=time.monotonic() + 1.0,
        )
    )
    await asyncio.sleep(0.06)

    assert acquire_task.done() is False

    held.release()
    acquired = await asyncio.wait_for(acquire_task, timeout=1.0)
    assert service._account_model_concurrency.active_count(account_id="acc-1", model="gpt-5.5") == 1

    acquired.release()
    assert service._account_model_concurrency.active_count(account_id="acc-1", model="gpt-5.5") == 0


@pytest.mark.asyncio
async def test_create_http_bridge_session_switches_when_connect_slot_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    key = proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key-connect-full", None)
    full_accounts = [
        cast(Any, SimpleNamespace(id=f"acc-full-{index}", status=AccountStatus.ACTIVE)) for index in range(3)
    ]
    available_account = cast(Any, SimpleNamespace(id="acc-next", status=AccountStatus.ACTIVE))
    accounts = [*full_accounts, available_account]
    held_leases = [
        service._account_model_concurrency.try_acquire(account_id=account.id, model="gpt-5.5", limit=1)
        for account in full_accounts
    ]
    assert all(lease is not None for lease in held_leases)
    captured_exclusions: list[set[str]] = []

    async def select_account(*_args: object, **kwargs: object) -> AccountSelection:
        excluded = set(cast(set[str], kwargs.get("exclude_account_ids", set())))
        captured_exclusions.append(excluded)
        for account in accounts:
            if account.id not in excluded:
                return AccountSelection(account=account, error_message=None, error_code=None)
        return AccountSelection(account=None, error_message="No active accounts available", error_code="no_accounts")

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_http_bridge_account_model_connect_limit=1,
            proxy_http_bridge_account_model_session_limit=20,
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
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=available_account))
    monkeypatch.setattr(
        service,
        "_open_upstream_websocket_with_budget",
        AsyncMock(return_value=cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))),
    )
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    try:
        session = await service._create_http_bridge_session(
            key,
            headers={},
            affinity=proxy_service._AffinityPolicy(key="cache-key-connect-full"),
            api_key=None,
            request_model="gpt-5.5",
            idle_ttl_seconds=120.0,
        )

        assert session.account is available_account
        assert captured_exclusions == [
            set(),
            {"acc-full-0"},
            {"acc-full-0", "acc-full-1"},
            {"acc-full-0", "acc-full-1", "acc-full-2"},
        ]
        for account in full_accounts:
            assert service._account_model_concurrency.active_count(account_id=account.id, model="gpt-5.5") == 1
        assert service._account_model_concurrency.active_count(account_id=available_account.id, model="gpt-5.5") == 0

        await service._close_http_bridge_session(session)
    finally:
        for lease in held_leases:
            if lease is not None:
                lease.release()


@pytest.mark.asyncio
async def test_http_bridge_session_budget_waits_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = proxy_service.ProxyService(cast(Any, nullcontext()))
    account = cast(Any, SimpleNamespace(id="acc-1", status=AccountStatus.ACTIVE))
    held = service._http_bridge_account_model_sessions.try_acquire(account_id="acc-1", model="gpt-5.5", limit=1)
    assert held is not None

    async def select_account(*_args: object, **_kwargs: object) -> AccountSelection:
        if service._http_bridge_account_model_sessions.active_count(account_id="acc-1", model="gpt-5.5") >= 1:
            return AccountSelection(
                account=None,
                error_message="All eligible accounts are at the local account/model concurrency budget",
                error_code="proxy_overloaded",
            )
        return AccountSelection(account=account, error_message=None, error_code=None)

    async def fake_relay(_session: proxy_service._HTTPBridgeSession) -> None:
        return None

    monkeypatch.setattr(
        proxy_service,
        "get_settings",
        lambda: Settings(
            proxy_http_bridge_account_model_session_limit=1,
            proxy_http_bridge_account_model_connect_limit=2,
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
                        prefer_earlier_reset_accounts=False,
                        routing_strategy=None,
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr(service, "_select_account_with_budget_compatible", select_account)
    monkeypatch.setattr(service, "_ensure_fresh_with_budget", AsyncMock(return_value=account))
    monkeypatch.setattr(
        service,
        "_open_upstream_websocket_with_budget",
        AsyncMock(return_value=cast(Any, SimpleNamespace(response_header=lambda _name: None, close=AsyncMock()))),
    )
    monkeypatch.setattr(service, "_relay_http_bridge_upstream_messages", fake_relay)

    try:
        create_task = asyncio.create_task(
            service._create_http_bridge_session(
                proxy_service._HTTPBridgeSessionKey("prompt_cache", "cache-key", None),
                headers={},
                affinity=proxy_service._AffinityPolicy(key="cache-key"),
                api_key=None,
                request_model="gpt-5.5",
                idle_ttl_seconds=120.0,
            )
        )
        await asyncio.sleep(0.06)

        assert create_task.done() is False

        held.release()
        session = await asyncio.wait_for(create_task, timeout=1.0)

        assert session.account is account
        assert service._http_bridge_account_model_sessions.active_count(account_id="acc-1", model="gpt-5.5") == 1

        await service._close_http_bridge_session(session)
        assert service._http_bridge_account_model_sessions.active_count(account_id="acc-1", model="gpt-5.5") == 0
    finally:
        held.release()
