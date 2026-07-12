from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

import anyio

from app.core.balancer import RoutingStrategy
from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.db.models import Account, AccountStatus, DashboardSettings
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _extract_model_class
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_busy_parallel_key,
    _http_bridge_parallel_batch_key,
    _http_bridge_previous_response_alias_key,
    _http_bridge_soft_shard_key,
    _http_bridge_turn_state_alias_key,
)
from app.modules.proxy._service.http_bridge.policy import (
    _account_status_value,
    _http_bridge_client_eviction_priority,
    _http_bridge_eviction_priority,
    _http_bridge_pressure_evictable_prompt_cache_session,
    _http_bridge_session_account_supports_request_model,
    _http_bridge_session_allows_api_key,
)
from app.modules.proxy._service.http_bridge.runtime import _HTTPBridgePressureCapacityHint
from app.modules.proxy._service.observability import _log_http_bridge_event
from app.modules.proxy._service.support import (
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _routing_strategy,
)
from app.modules.proxy.account_concurrency import AccountModelConcurrencyLease

logger = logging.getLogger("app.modules.proxy.service")


@dataclass(frozen=True, slots=True)
class _HTTPBridgeCreationSlotReservation:
    inflight_future: asyncio.Future[_HTTPBridgeSession] | None
    capacity_wait_future: asyncio.Future[_HTTPBridgeSession] | None
    owns_creation: bool
    evicted_sessions: tuple[_HTTPBridgeSession, ...]


class _HTTPBridgeCapacityService(Protocol):
    _http_bridge_inflight_sessions: dict[_HTTPBridgeSessionKey, asyncio.Future[_HTTPBridgeSession]]
    _http_bridge_lock: anyio.Lock
    _http_bridge_previous_response_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey]
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]
    _http_bridge_turn_state_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey]

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    @staticmethod
    async def _http_bridge_dashboard_settings() -> DashboardSettings: ...

    async def _http_bridge_routable_budget_safe_account_ids(
        self,
        *,
        model: str | None,
        account_ids: Collection[str] | None,
        allowed_groups: Collection[str] | None,
        preferred_group_priorities: dict[str, int] | None,
        budget_threshold_pct: float,
        routing_strategy: RoutingStrategy,
    ) -> set[str]: ...

    def _try_acquire_http_bridge_session_account_model_concurrency(
        self,
        *,
        account: Account,
        model: str | None,
        request_id: str,
    ) -> AccountModelConcurrencyLease | None: ...

    async def _close_http_bridge_session(self, session: _HTTPBridgeSession, **kwargs: object) -> None: ...

    def _unregister_http_bridge_turn_states_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_previous_response_ids_locked(self, session: _HTTPBridgeSession) -> None: ...

    async def _http_bridge_pending_count(self, session: _HTTPBridgeSession) -> int: ...

    async def _http_bridge_replacement_busy_count(self, session: _HTTPBridgeSession) -> int: ...

    async def _evict_http_bridge_idle_session_for_account_model_capacity(
        self,
        *,
        model: str | None,
        protected_key: _HTTPBridgeSessionKey,
        account_ids: set[str] | None = None,
    ) -> bool: ...

    async def _resolve_http_bridge_pressure_capacity_hint(
        self,
        *,
        settings: Settings,
        max_sessions: int,
        api_key: ApiKeyData | None,
        request_model: str | None,
    ) -> _HTTPBridgePressureCapacityHint | None: ...

    def _http_bridge_pressure_capacity_locked(
        self,
        *,
        settings: Settings,
        max_sessions: int,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> int: ...

    def _http_bridge_pressure_current_count_locked(
        self,
        *,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> int: ...

    async def _evict_http_bridge_parallel_prompt_cache_pressure_locked(
        self,
        *,
        settings: Settings,
        max_sessions: int,
        protected_key: _HTTPBridgeSessionKey,
        request_model: str | None,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> list[_HTTPBridgeSession]: ...

    async def _reserve_http_bridge_creation_slot_locked(
        self,
        *,
        key: _HTTPBridgeSessionKey,
        max_sessions: int,
        request_model: str | None,
    ) -> _HTTPBridgeCreationSlotReservation: ...


class _HTTPBridgeCapacityMixin:
    async def _reserve_http_bridge_creation_slot_locked(
        self: _HTTPBridgeCapacityService,
        *,
        key: _HTTPBridgeSessionKey,
        max_sessions: int,
        request_model: str | None,
    ) -> _HTTPBridgeCreationSlotReservation:
        evicted_sessions: list[_HTTPBridgeSession] = []
        while (
            max_sessions > 0
            and len(self._http_bridge_sessions) + len(self._http_bridge_inflight_sessions) >= max_sessions
            and self._http_bridge_sessions
        ):
            evictable_sessions: list[tuple[_HTTPBridgeSessionKey, _HTTPBridgeSession]] = []
            for candidate_key, candidate_session in self._http_bridge_sessions.items():
                pending_count = await self._http_bridge_pending_count(candidate_session)
                if pending_count or candidate_session.submit_lease_count > 0:
                    continue
                evictable_sessions.append((candidate_key, candidate_session))
            if not evictable_sessions:
                break
            lru_key, lru_session = min(
                evictable_sessions,
                key=lambda item: _http_bridge_eviction_priority(item[1]),
            )
            _log_http_bridge_event(
                "evict_lru",
                lru_key,
                account_id=lru_session.account.id,
                model=lru_session.request_model,
                cache_key_family=lru_key.affinity_kind,
                model_class=_extract_model_class(lru_session.request_model) if lru_session.request_model else None,
            )
            self._http_bridge_sessions.pop(lru_key, None)
            evicted_sessions.append(lru_session)

        if (
            max_sessions > 0
            and len(self._http_bridge_sessions) + len(self._http_bridge_inflight_sessions) >= max_sessions
        ):
            if self._http_bridge_inflight_sessions:
                return _HTTPBridgeCreationSlotReservation(
                    inflight_future=None,
                    capacity_wait_future=next(iter(self._http_bridge_inflight_sessions.values())),
                    owns_creation=False,
                    evicted_sessions=tuple(evicted_sessions),
                )
            _log_http_bridge_event(
                "capacity_exhausted_active_sessions",
                key,
                account_id=None,
                model=request_model,
                pending_count=len(self._http_bridge_sessions) + len(self._http_bridge_inflight_sessions),
                cache_key_family=key.affinity_kind,
                model_class=_extract_model_class(request_model) if request_model else None,
            )
            raise ProxyResponseError(
                429,
                openai_error(
                    "rate_limit_exceeded",
                    "HTTP responses session bridge has no idle capacity",
                    error_type="rate_limit_error",
                ),
            )

        inflight_future = asyncio.get_running_loop().create_future()
        self._http_bridge_inflight_sessions[key] = inflight_future
        return _HTTPBridgeCreationSlotReservation(
            inflight_future=inflight_future,
            capacity_wait_future=None,
            owns_creation=True,
            evicted_sessions=tuple(evicted_sessions),
        )

    async def _http_bridge_pending_count(
        self: _HTTPBridgeCapacityService,
        session: _HTTPBridgeSession,
    ) -> int:
        async with session.pending_lock:
            return max(len(session.pending_requests), session.queued_request_count)

    async def _http_bridge_replacement_busy_count(
        self: _HTTPBridgeCapacityService,
        session: _HTTPBridgeSession,
    ) -> int:
        pending_count = await self._http_bridge_pending_count(session)
        return max(pending_count, session.submit_lease_count)

    def _acquire_http_bridge_submit_lease_locked(
        self: _HTTPBridgeCapacityService,
        session: _HTTPBridgeSession,
    ) -> _HTTPBridgeSession:
        session.submit_lease_count += 1
        return session

    async def _release_http_bridge_submit_lease(
        self: _HTTPBridgeCapacityService,
        session: _HTTPBridgeSession,
    ) -> None:
        async with self._http_bridge_lock:
            session.submit_lease_count = max(0, session.submit_lease_count - 1)

    async def _restore_http_bridge_session_after_reconnect(
        self: _HTTPBridgeCapacityService,
        session: "_HTTPBridgeSession",
        *,
        request_id: str,
    ) -> str | None:
        sessions_to_close: list[_HTTPBridgeSession] = []
        if session.account_model_session_lease is None:
            lease = self._try_acquire_http_bridge_session_account_model_concurrency(
                account=session.account,
                model=session.request_model,
                request_id=request_id,
            )
            if lease is None:
                reclaimed = await self._evict_http_bridge_idle_session_for_account_model_capacity(
                    model=session.request_model,
                    protected_key=session.key,
                    account_ids={session.account.id},
                )
                if reclaimed:
                    lease = self._try_acquire_http_bridge_session_account_model_concurrency(
                        account=session.account,
                        model=session.request_model,
                        request_id=request_id,
                    )
            if lease is None:
                return "session_lease_unavailable"
            session.account_model_session_lease = lease

        async with self._http_bridge_lock:
            current_session = self._http_bridge_sessions.get(session.key)
            if current_session is not None and current_session is not session:
                if session.key.strength == "soft":
                    return None
                current_busy_count = await self._http_bridge_replacement_busy_count(current_session)
                if current_busy_count:
                    return "key_conflict_busy"
                self._http_bridge_sessions.pop(session.key, None)
                self._unregister_http_bridge_turn_states_locked(current_session)
                self._unregister_http_bridge_previous_response_ids_locked(current_session)
                current_session.closed = True
                sessions_to_close.append(current_session)
            self._http_bridge_sessions[session.key] = session
            if session.downstream_turn_state is not None:
                session.downstream_turn_state_aliases.add(session.downstream_turn_state)
            for alias in session.downstream_turn_state_aliases:
                self._http_bridge_turn_state_index[_http_bridge_turn_state_alias_key(alias, session.key.api_key_id)] = (
                    session.key
                )
            for response_id in session.previous_response_ids:
                self._http_bridge_previous_response_index[
                    _http_bridge_previous_response_alias_key(response_id, session.key.api_key_id)
                ] = session.key
        for old_session in sessions_to_close:
            await self._close_http_bridge_session(old_session)
        return None

    async def _evict_http_bridge_idle_session_for_account_model_capacity(
        self: _HTTPBridgeCapacityService,
        *,
        model: str | None,
        protected_key: "_HTTPBridgeSessionKey",
        account_ids: set[str] | None = None,
    ) -> bool:
        async with self._http_bridge_lock:
            candidates: list[tuple[_HTTPBridgeSessionKey, _HTTPBridgeSession]] = []
            for candidate_key, candidate_session in self._http_bridge_sessions.items():
                if candidate_key == protected_key or candidate_session.closed:
                    continue
                if account_ids is not None and candidate_session.account.id not in account_ids:
                    continue
                if model is not None and candidate_session.request_model != model:
                    continue
                pending_count = await self._http_bridge_pending_count(candidate_session)
                if pending_count > 0 or candidate_session.submit_lease_count > 0:
                    continue
                candidates.append((candidate_key, candidate_session))
            if not candidates:
                return False
            evict_key, evict_session = min(
                candidates,
                key=lambda item: (
                    _http_bridge_client_eviction_priority(item[1].client_kind),
                    *_http_bridge_eviction_priority(item[1]),
                    item[0].affinity_key,
                ),
            )
            removed = self._http_bridge_sessions.pop(evict_key, None)
            if removed is None:
                return False
            _log_http_bridge_event(
                "evict_account_model_session_capacity",
                evict_key,
                account_id=removed.account.id,
                model=removed.request_model,
                detail=f"request_model={model}, protected_kind={protected_key.affinity_kind}",
                cache_key_family=evict_key.affinity_kind,
                model_class=_extract_model_class(removed.request_model) if removed.request_model else None,
            )
        await self._close_http_bridge_session(removed)
        return True

    async def _resolve_http_bridge_pressure_capacity_hint(
        self: _HTTPBridgeCapacityService,
        *,
        settings: Settings,
        max_sessions: int,
        api_key: ApiKeyData | None,
        request_model: str | None,
    ) -> _HTTPBridgePressureCapacityHint | None:
        if max_sessions > 0:
            return _HTTPBridgePressureCapacityHint(capacity=max_sessions)
        per_account_limit = max(0, int(settings.proxy_http_bridge_account_model_session_limit))
        if per_account_limit <= 0:
            return None
        scoped_account_ids = (
            set(api_key.assigned_account_ids)
            if api_key is not None and api_key.account_assignment_scope_enabled
            else None
        )
        allowed_groups = set(api_key.allowed_groups) if api_key is not None and api_key.allowed_groups else None
        preferred_group_priorities = (
            {preference.group: preference.priority for preference in api_key.preferred_groups}
            if api_key is not None and api_key.preferred_groups
            else None
        )
        try:
            dashboard_settings = await self._http_bridge_dashboard_settings()
            account_ids = await self._http_bridge_routable_budget_safe_account_ids(
                model=request_model,
                account_ids=scoped_account_ids,
                allowed_groups=allowed_groups,
                preferred_group_priorities=preferred_group_priorities,
                budget_threshold_pct=dashboard_settings.sticky_reallocation_budget_threshold_pct,
                routing_strategy=_routing_strategy(dashboard_settings),
            )
        except Exception:
            logger.warning(
                "Failed to resolve HTTP bridge routable pressure capacity; falling back to session pool capacity",
                exc_info=True,
            )
            return None
        if not account_ids:
            return None
        return _HTTPBridgePressureCapacityHint(
            capacity=len(account_ids) * per_account_limit,
            account_ids=frozenset(account_ids),
        )

    def _http_bridge_pressure_capacity_locked(
        self: _HTTPBridgeCapacityService,
        *,
        settings: Settings,
        max_sessions: int,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> int:
        if max_sessions > 0:
            return max_sessions
        if isinstance(capacity_hint, _HTTPBridgePressureCapacityHint):
            return capacity_hint.capacity if capacity_hint.capacity > 0 else 0
        if isinstance(capacity_hint, int) and capacity_hint > 0:
            return capacity_hint
        account_ids = {
            session.account.id
            for session in self._http_bridge_sessions.values()
            if not session.closed
            and session.account.id is not None
            and _account_status_value(session.account.status) == AccountStatus.ACTIVE.value
        }
        per_account_limit = max(0, int(settings.proxy_http_bridge_account_model_session_limit))
        if per_account_limit <= 0 or not account_ids:
            return 0
        return len(account_ids) * per_account_limit

    def _http_bridge_pressure_current_count_locked(
        self: _HTTPBridgeCapacityService,
        *,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> int:
        if isinstance(capacity_hint, _HTTPBridgePressureCapacityHint) and capacity_hint.account_ids is not None:
            return sum(
                1
                for session in self._http_bridge_sessions.values()
                if not session.closed and session.account.id in capacity_hint.account_ids
            )
        return len(self._http_bridge_sessions) + len(self._http_bridge_inflight_sessions)

    async def _evict_http_bridge_pressure(
        self: _HTTPBridgeCapacityService,
        *,
        max_sessions: int,
        protected_key: "_HTTPBridgeSessionKey",
        request_model: str | None,
        api_key: ApiKeyData | None = None,
    ) -> list["_HTTPBridgeSession"]:
        settings = self._http_bridge_runtime_settings()
        if not settings.http_responses_session_bridge_pressure_eviction_enabled:
            return []
        capacity_hint = await self._resolve_http_bridge_pressure_capacity_hint(
            settings=settings,
            max_sessions=max_sessions,
            api_key=api_key,
            request_model=request_model,
        )
        async with self._http_bridge_lock:
            sessions_to_close = await self._evict_http_bridge_parallel_prompt_cache_pressure_locked(
                settings=settings,
                max_sessions=max_sessions,
                protected_key=protected_key,
                request_model=request_model,
                capacity_hint=capacity_hint,
            )
        for stale_session in sessions_to_close:
            await self._close_http_bridge_session(stale_session)
        return sessions_to_close

    async def _evict_http_bridge_parallel_prompt_cache_pressure_locked(
        self: _HTTPBridgeCapacityService,
        *,
        settings: Settings,
        max_sessions: int,
        protected_key: "_HTTPBridgeSessionKey",
        request_model: str | None,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> list["_HTTPBridgeSession"]:
        if not settings.http_responses_session_bridge_pressure_eviction_enabled:
            return []
        capacity = self._http_bridge_pressure_capacity_locked(
            settings=settings,
            max_sessions=max_sessions,
            capacity_hint=capacity_hint,
        )
        if capacity <= 0:
            return []
        current_count = self._http_bridge_pressure_current_count_locked(capacity_hint=capacity_hint)
        threshold_count = max(
            1,
            int(capacity * settings.http_responses_session_bridge_pressure_eviction_threshold_percent / 100.0),
        )
        projected_count = current_count + 1
        if projected_count < threshold_count:
            return []

        now = time.monotonic()
        batch_window_seconds = settings.http_responses_session_bridge_pressure_eviction_batch_window_seconds
        min_batch_sessions = settings.http_responses_session_bridge_pressure_eviction_min_batch_sessions
        min_idle_seconds = settings.http_responses_session_bridge_pressure_eviction_min_idle_seconds
        candidates: list[tuple[_HTTPBridgeSessionKey, _HTTPBridgeSession, tuple[str, str | None, int]]] = []
        batch_counts: dict[tuple[str, str | None, int], int] = {}
        scoped_account_ids = (
            capacity_hint.account_ids if isinstance(capacity_hint, _HTTPBridgePressureCapacityHint) else None
        )
        for candidate_key, candidate_session in self._http_bridge_sessions.items():
            if candidate_key == protected_key:
                continue
            if scoped_account_ids is not None and candidate_session.account.id not in scoped_account_ids:
                continue
            pending_count = await self._http_bridge_pending_count(candidate_session)
            if not _http_bridge_pressure_evictable_prompt_cache_session(
                candidate_session,
                pending_count=pending_count,
                now=now,
                min_idle_seconds=min_idle_seconds,
            ):
                continue
            batch_key = _http_bridge_parallel_batch_key(
                candidate_session,
                batch_window_seconds=batch_window_seconds,
            )
            candidates.append((candidate_key, candidate_session, batch_key))
            batch_counts[batch_key] = batch_counts.get(batch_key, 0) + 1

        large_batch_keys = {batch_key for batch_key, count in batch_counts.items() if count >= min_batch_sessions}
        if large_batch_keys:
            eligible = [candidate for candidate in candidates if candidate[2] in large_batch_keys]
            batch_scope = "bucket"
        elif len(candidates) >= min_batch_sessions:
            eligible = list(candidates)
            batch_scope = "pool"
        else:
            return []
        if not eligible:
            return []

        evict_count = max(1, projected_count - threshold_count + 1)
        eligible.sort(
            key=lambda item: (
                _http_bridge_client_eviction_priority(item[1].client_kind),
                -batch_counts[item[2]],
                item[1].last_used_at,
                item[1].created_at,
                item[0].affinity_key,
            )
        )
        evicted_sessions: list[_HTTPBridgeSession] = []
        for evict_key, evict_session, batch_key in eligible[:evict_count]:
            removed = self._http_bridge_sessions.pop(evict_key, None)
            if removed is None:
                continue
            _log_http_bridge_event(
                "pressure_evict_parallel_prompt_cache",
                evict_key,
                account_id=removed.account.id,
                model=removed.request_model,
                detail=(
                    f"capacity={capacity}, threshold={threshold_count}, current={current_count}, "
                    f"batch_size={batch_counts[batch_key]}, candidate_count={len(candidates)}, "
                    f"batch_scope={batch_scope}, client_kind={removed.client_kind}, request_model={request_model}"
                ),
                cache_key_family=evict_key.affinity_kind,
                model_class=_extract_model_class(removed.request_model) if removed.request_model else None,
            )
            evicted_sessions.append(removed)
        return evicted_sessions

    async def _select_http_bridge_soft_shard_key_locked(
        self: _HTTPBridgeCapacityService,
        base_key: "_HTTPBridgeSessionKey",
        *,
        api_key: ApiKeyData | None,
        request_model: str | None,
        pending_limit: int,
        max_shards: int,
    ) -> "_HTTPBridgeSessionKey":
        if max_shards <= 1:
            return base_key

        reusable_over_limit: list[tuple[int, int, _HTTPBridgeSessionKey]] = []
        first_available_new_key: _HTTPBridgeSessionKey | None = None
        for shard_index in range(max_shards):
            shard_key = _http_bridge_soft_shard_key(base_key, shard_index)
            if shard_key in self._http_bridge_inflight_sessions:
                continue
            session = self._http_bridge_sessions.get(shard_key)
            if session is None or session.closed:
                if first_available_new_key is None:
                    first_available_new_key = shard_key
                continue
            if (
                session.account.status != AccountStatus.ACTIVE
                or session.codex_session
                or not _http_bridge_session_allows_api_key(session, api_key)
                or not _http_bridge_session_account_supports_request_model(session, request_model)
            ):
                if shard_index == 0 and not session.codex_session:
                    return base_key
                continue
            pending_count = await self._http_bridge_pending_count(session)
            if pending_count < pending_limit:
                return shard_key
            reusable_over_limit.append((pending_count, shard_index, shard_key))

        if first_available_new_key is not None:
            return first_available_new_key
        if reusable_over_limit:
            return min(reusable_over_limit, key=lambda item: (item[0], item[1]))[2]
        return base_key

    def _select_http_bridge_busy_parallel_key_locked(
        self: _HTTPBridgeCapacityService,
        base_key: "_HTTPBridgeSessionKey",
        *,
        max_sessions: int,
        allow_soft_prompt_cache: bool = False,
    ) -> "_HTTPBridgeSessionKey | None":
        if max_sessions == 1:
            return None
        if base_key.strength != "hard" and not (allow_soft_prompt_cache and base_key.affinity_kind == "prompt_cache"):
            return None
        scan_limit = (
            max_sessions
            if max_sessions > 0
            else len(self._http_bridge_sessions) + len(self._http_bridge_inflight_sessions) + 2
        )
        idle_existing_key: _HTTPBridgeSessionKey | None = None
        for shard_index in range(1, scan_limit):
            parallel_key = _http_bridge_busy_parallel_key(base_key, shard_index)
            if parallel_key in self._http_bridge_inflight_sessions:
                continue
            session = self._http_bridge_sessions.get(parallel_key)
            if session is None or session.closed:
                return parallel_key
            if (
                idle_existing_key is None
                and session.submit_lease_count <= 0
                and not session.pending_requests
                and session.queued_request_count <= 0
            ):
                idle_existing_key = parallel_key
        if idle_existing_key is not None:
            return idle_existing_key
        return None
