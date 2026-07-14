from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, overload

import anyio

from app.core import shutdown as shutdown_state
from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.db.models import AccountStatus
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import (
    _extract_model_class,
    _sticky_key_from_session_header,
    _sticky_key_from_turn_state_header,
)
from app.modules.proxy._service.budget import _raise_proxy_budget_exhausted, _remaining_budget_seconds
from app.modules.proxy._service.http_bridge.capacity import _HTTPBridgeCreationSlotReservation
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_previous_response_alias_key,
    _http_bridge_soft_shard_index,
    _http_bridge_soft_sharding_allowed,
    _http_bridge_turn_state_alias_key,
)
from app.modules.proxy._service.http_bridge.owner_resolution import _HTTPBridgeOwnerResolution
from app.modules.proxy._service.http_bridge.ownership import (
    _durable_bridge_lookup_local_reuse_decision,
    _forwarded_http_bridge_session_key,
    _http_bridge_allow_durable_takeover,
    _http_bridge_busy_session_error_envelope,
    _http_bridge_can_recover_during_drain,
    _http_bridge_continuity_lost_error_envelope,
)
from app.modules.proxy._service.http_bridge.policy import (
    _http_bridge_session_allows_api_key,
    _http_bridge_session_matches_preferred_account,
    _http_bridge_soft_prompt_cache_busy_parallel_allowed,
)
from app.modules.proxy._service.http_bridge.runtime import _HTTPBridgePressureCapacityHint
from app.modules.proxy._service.observability import (
    _elapsed_ms,
    _hash_identifier,
    _log_http_bridge_event,
    _log_http_bridge_get_or_create_breakdown,
    _record_bridge_drain_recovery_allowed,
)
from app.modules.proxy._service.support import (
    _AffinityPolicy,
    _await_operation_before_hard_timeout,
    _await_shielded_cleanup,
    _HTTPBridgeOwnerForward,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _merge_affinity_required_wire_api,
    _schedule_tracked_background_task,
)
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeLookup
from app.modules.proxy.ring_membership import RingMembershipService


@dataclass(slots=True)
class _HTTPBridgeAcquireTrace:
    request_id: str | None
    started_at: float = field(default_factory=time.monotonic)
    registration_check_ms: int = 0
    registration_gate_ms: int = 0
    main_lock_wait_ms: int = 0
    main_lock_body_ms: int = 0
    stale_close_ms: int = 0
    capacity_wait_ms: int = 0
    inflight_wait_ms: int = 0
    create_session_ms: int = 0
    loops: int = 0

    def log(
        self,
        *,
        outcome: str,
        key: _HTTPBridgeSessionKey,
        account_id: str | None,
        model: str | None,
        request_stage: str,
        preferred_account_id: str | None,
        require_preferred_account: bool,
    ) -> None:
        _log_http_bridge_get_or_create_breakdown(
            trace_request_id=self.request_id,
            outcome=outcome,
            key=key,
            account_id=account_id,
            model=model,
            request_stage=request_stage,
            total_ms=_elapsed_ms(self.started_at, time.monotonic()),
            registration_check_ms=self.registration_check_ms,
            registration_gate_ms=self.registration_gate_ms,
            main_lock_wait_ms=self.main_lock_wait_ms,
            main_lock_body_ms=self.main_lock_body_ms,
            stale_close_ms=self.stale_close_ms,
            capacity_wait_ms=self.capacity_wait_ms,
            inflight_wait_ms=self.inflight_wait_ms,
            create_session_ms=self.create_session_ms,
            loops=self.loops,
            preferred_account_id=preferred_account_id,
            require_preferred_account=require_preferred_account,
        )


class _HTTPBridgeSessionAcquireService(Protocol):
    _http_bridge_inflight_sessions: dict[_HTTPBridgeSessionKey, asyncio.Future[_HTTPBridgeSession]]
    _http_bridge_lock: anyio.Lock
    _http_bridge_previous_response_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey]
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]
    _http_bridge_turn_state_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey]
    _proxy_cleanup_tasks: set[asyncio.Task[None]]
    _ring_membership: RingMembershipService | None

    async def _get_or_create_http_bridge_session(
        self,
        key: _HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> _HTTPBridgeSession | _HTTPBridgeOwnerForward: ...

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    async def _http_bridge_should_wait_for_registration_compatible(
        self,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
    ) -> bool: ...

    async def _http_bridge_owner_instance_compatible(
        self,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
    ) -> str | None: ...

    async def _active_http_bridge_instance_ring_compatible(
        self,
        settings: Settings,
    ) -> tuple[str, tuple[str, ...]]: ...

    async def _resolve_http_bridge_owner(
        self,
        *,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
        headers: dict[str, str],
        request_model: str | None,
        previous_response_id: str | None,
        durable_lookup: DurableBridgeLookup | None,
        gateway_safe_mode: bool,
        allow_previous_response_recovery_rebind: bool,
        allow_bootstrap_owner_rebind: bool,
        allow_forward_to_owner: bool,
        forwarded_request: bool,
        incoming_turn_state: str | None,
        incoming_session_key: str | None,
    ) -> _HTTPBridgeOwnerResolution: ...

    @staticmethod
    def _http_bridge_session_reusable_for_request_compatible(
        *,
        session: _HTTPBridgeSession,
        key: _HTTPBridgeSessionKey,
        incoming_turn_state: str | None,
        previous_response_id: str | None,
        request_model: str | None,
        required_upstream_wire_api: str | None = None,
    ) -> bool: ...

    @staticmethod
    def _record_http_bridge_continuity_fail_closed(
        *,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None,
        upstream_error_code: str | None = None,
    ) -> None: ...

    async def _resolve_http_bridge_pressure_capacity_hint(
        self,
        *,
        settings: Settings,
        max_sessions: int,
        api_key: ApiKeyData | None,
        request_model: str | None,
    ) -> _HTTPBridgePressureCapacityHint | None: ...

    async def _reserve_http_bridge_creation_slot_locked(
        self,
        *,
        key: _HTTPBridgeSessionKey,
        max_sessions: int,
        request_model: str | None,
    ) -> _HTTPBridgeCreationSlotReservation: ...

    async def _prune_http_bridge_sessions_locked(self) -> list[_HTTPBridgeSession]: ...

    async def _evict_http_bridge_parallel_prompt_cache_pressure_locked(
        self,
        *,
        settings: Settings,
        max_sessions: int,
        protected_key: _HTTPBridgeSessionKey,
        request_model: str | None,
        capacity_hint: _HTTPBridgePressureCapacityHint | int | None = None,
    ) -> list[_HTTPBridgeSession]: ...

    async def _select_http_bridge_soft_shard_key_locked(
        self,
        base_key: _HTTPBridgeSessionKey,
        *,
        api_key: ApiKeyData | None,
        request_model: str | None,
        pending_limit: int,
        max_shards: int,
    ) -> _HTTPBridgeSessionKey: ...

    def _promote_http_bridge_session_to_codex_affinity(
        self,
        session: _HTTPBridgeSession,
        *,
        turn_state: str,
        settings: Settings,
    ) -> None: ...

    def _http_bridge_session_request_budget_full(
        self,
        session: _HTTPBridgeSession,
        *,
        request_model: str | None,
    ) -> bool: ...

    def _schedule_durable_http_bridge_session_refresh(self, session: _HTTPBridgeSession) -> None: ...

    async def _http_bridge_pending_count(self, session: _HTTPBridgeSession) -> int: ...

    def _acquire_http_bridge_submit_lease_locked(self, session: _HTTPBridgeSession) -> _HTTPBridgeSession: ...

    async def _http_bridge_replacement_busy_count(self, session: _HTTPBridgeSession) -> int: ...

    def _select_http_bridge_busy_parallel_key_locked(
        self,
        base_key: _HTTPBridgeSessionKey,
        *,
        max_sessions: int,
        allow_soft_prompt_cache: bool = False,
    ) -> _HTTPBridgeSessionKey | None: ...

    def _http_bridge_busy_parallel_replacement_key_locked(
        self,
        *,
        key: _HTTPBridgeSessionKey,
        session: _HTTPBridgeSession,
        busy_count: int,
        max_sessions: int,
        incoming_turn_state: str | None,
        previous_response_id: str | None,
    ) -> _HTTPBridgeSessionKey | None: ...

    def _unregister_http_bridge_turn_states_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_previous_response_ids_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _detach_http_bridge_session_indexes_locked(self, session: _HTTPBridgeSession) -> bool: ...

    def _mark_stale_durable_http_bridge_session_locked(
        self,
        session: _HTTPBridgeSession,
    ) -> bool: ...

    async def _detach_http_bridge_session_for_background_close(self, session: _HTTPBridgeSession) -> None: ...

    def _schedule_http_bridge_session_close(
        self,
        session: _HTTPBridgeSession,
        *,
        reason: str,
    ) -> asyncio.Future[None]: ...

    async def _create_http_bridge_session_compatible(
        self,
        key: _HTTPBridgeSessionKey,
        **kwargs: object,
    ) -> _HTTPBridgeSession: ...

    async def _claim_durable_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        allow_takeover: bool,
    ) -> None: ...

    async def _close_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
    ) -> None: ...

    async def _release_http_bridge_submit_lease(self, session: _HTTPBridgeSession) -> None: ...


class _HTTPBridgeSessionAcquireMixin:
    def _http_bridge_busy_parallel_replacement_key_locked(
        self: _HTTPBridgeSessionAcquireService,
        *,
        key: _HTTPBridgeSessionKey,
        session: _HTTPBridgeSession,
        busy_count: int,
        max_sessions: int,
        incoming_turn_state: str | None,
        previous_response_id: str | None,
    ) -> _HTTPBridgeSessionKey | None:
        if busy_count <= 0:
            return None
        parallel_key = self._select_http_bridge_busy_parallel_key_locked(
            key,
            max_sessions=max_sessions,
            allow_soft_prompt_cache=_http_bridge_soft_prompt_cache_busy_parallel_allowed(
                key=key,
                session=session,
                incoming_turn_state=incoming_turn_state,
                previous_response_id=previous_response_id,
            ),
        )
        _log_http_bridge_event(
            "busy_recreate_parallel" if parallel_key is not None else "busy_recreate_deferred",
            parallel_key or key,
            account_id=session.account.id,
            model=session.request_model,
            pending_count=busy_count,
            detail=f"base_key={_hash_identifier(key.affinity_key)}" if parallel_key is not None else None,
            cache_key_family=(parallel_key or key).affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )
        if parallel_key is None:
            raise ProxyResponseError(503, _http_bridge_busy_session_error_envelope())
        return parallel_key

    @overload
    async def _get_or_create_http_bridge_session(
        self: _HTTPBridgeSessionAcquireService,
        key: "_HTTPBridgeSessionKey",
        *,
        headers: dict[str, str],
        affinity: _AffinityPolicy,
        api_key: ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        max_sessions: int,
        previous_response_id: str | None = None,
        gateway_safe_mode: bool = False,
        allow_forward_to_owner: Literal[False] = False,
        forwarded_request: bool = False,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        allow_previous_response_recovery_rebind: bool = False,
        allow_bootstrap_owner_rebind: bool = False,
        durable_lookup: DurableBridgeLookup | None = None,
        durable_account_supports_request_model: bool = True,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
        trace_request_id: str | None = None,
        request_deadline_at: float | None = None,
    ) -> "_HTTPBridgeSession": ...

    @overload
    async def _get_or_create_http_bridge_session(
        self: _HTTPBridgeSessionAcquireService,
        key: "_HTTPBridgeSessionKey",
        *,
        headers: dict[str, str],
        affinity: _AffinityPolicy,
        api_key: ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        max_sessions: int,
        previous_response_id: str | None = None,
        gateway_safe_mode: bool = False,
        allow_forward_to_owner: Literal[True],
        forwarded_request: bool = False,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        allow_previous_response_recovery_rebind: bool = False,
        allow_bootstrap_owner_rebind: bool = False,
        durable_lookup: DurableBridgeLookup | None = None,
        durable_account_supports_request_model: bool = True,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
        trace_request_id: str | None = None,
        request_deadline_at: float | None = None,
    ) -> "_HTTPBridgeSession | _HTTPBridgeOwnerForward": ...

    async def _get_or_create_http_bridge_session(
        self: _HTTPBridgeSessionAcquireService,
        key: "_HTTPBridgeSessionKey",
        *,
        headers: dict[str, str],
        affinity: _AffinityPolicy,
        api_key: ApiKeyData | None,
        request_model: str | None,
        idle_ttl_seconds: float,
        max_sessions: int,
        previous_response_id: str | None = None,
        gateway_safe_mode: bool = False,
        allow_forward_to_owner: bool = False,
        forwarded_request: bool = False,
        forwarded_affinity_kind: str | None = None,
        forwarded_affinity_key: str | None = None,
        allow_previous_response_recovery_rebind: bool = False,
        allow_bootstrap_owner_rebind: bool = False,
        durable_lookup: DurableBridgeLookup | None = None,
        durable_account_supports_request_model: bool = True,
        request_stage: str = "first_turn",
        preferred_account_id: str | None = None,
        trace_request_id: str | None = None,
        request_deadline_at: float | None = None,
        _deadline_guarded: bool = False,
    ) -> "_HTTPBridgeSession | _HTTPBridgeOwnerForward":
        if request_deadline_at is not None and not _deadline_guarded:
            remaining = request_deadline_at - time.monotonic()
            if remaining <= 0:
                _raise_proxy_budget_exhausted()
            operation = asyncio.create_task(
                self._get_or_create_http_bridge_session(
                    key,
                    headers=headers,
                    affinity=affinity,
                    api_key=api_key,
                    request_model=request_model,
                    idle_ttl_seconds=idle_ttl_seconds,
                    max_sessions=max_sessions,
                    previous_response_id=previous_response_id,
                    gateway_safe_mode=gateway_safe_mode,
                    allow_forward_to_owner=allow_forward_to_owner,
                    forwarded_request=forwarded_request,
                    forwarded_affinity_kind=forwarded_affinity_kind,
                    forwarded_affinity_key=forwarded_affinity_key,
                    allow_previous_response_recovery_rebind=allow_previous_response_recovery_rebind,
                    allow_bootstrap_owner_rebind=allow_bootstrap_owner_rebind,
                    durable_lookup=durable_lookup,
                    durable_account_supports_request_model=durable_account_supports_request_model,
                    request_stage=request_stage,
                    preferred_account_id=preferred_account_id,
                    trace_request_id=trace_request_id,
                    request_deadline_at=request_deadline_at,
                    _deadline_guarded=True,
                ),
                name=f"http-bridge-acquire-{trace_request_id or key.affinity_kind}",
            )

            def reconcile_late_result() -> None:
                operation.cancel()

                async def reconcile() -> None:
                    try:
                        result = await operation
                    except (asyncio.CancelledError, Exception):
                        return
                    if isinstance(result, _HTTPBridgeSession):
                        await self._release_http_bridge_submit_lease(result)

                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    reconcile(),
                    name=f"http-bridge-acquire-reconcile-{trace_request_id or key.affinity_kind}",
                    label=f"late HTTP bridge acquisition result request_id={trace_request_id}",
                )

            try:
                done, _pending = await asyncio.wait({operation}, timeout=remaining)
            except BaseException:
                reconcile_late_result()
                raise
            if not done:
                reconcile_late_result()
                _raise_proxy_budget_exhausted()
            return operation.result()
        settings = self._http_bridge_runtime_settings()
        api_key_id = api_key.id if api_key is not None else None
        incoming_turn_state = _sticky_key_from_turn_state_header(headers)
        incoming_session_key = _sticky_key_from_session_header(headers)
        trace = _HTTPBridgeAcquireTrace(trace_request_id)
        registration_check_started_at = time.monotonic()
        should_wait_for_registration = await self._http_bridge_should_wait_for_registration_compatible(key, settings)
        trace.registration_check_ms += _elapsed_ms(registration_check_started_at, time.monotonic()) or 0
        if should_wait_for_registration:
            skip_registration_gate = False
            async with self._http_bridge_lock:
                existing = self._http_bridge_sessions.get(key)
                if existing is not None:
                    skip_registration_gate = True
                elif incoming_turn_state is not None:
                    alias_index_key = _http_bridge_turn_state_alias_key(incoming_turn_state, api_key_id)
                    alias_key = self._http_bridge_turn_state_index.get(alias_index_key)
                    if alias_key is not None and alias_key in self._http_bridge_sessions:
                        skip_registration_gate = True
            if not skip_registration_gate:
                import app.core.startup as startup_module

                registration_gate_started_at = time.monotonic()
                registered = await startup_module.wait_for_bridge_registration(
                    timeout_seconds=settings.upstream_connect_timeout_seconds,
                )
                trace.registration_gate_ms += _elapsed_ms(registration_gate_started_at, time.monotonic()) or 0
                if not registered:
                    raise ProxyResponseError(
                        503,
                        openai_error(
                            "bridge_owner_unreachable",
                            "HTTP bridge registration is not ready",
                            error_type="server_error",
                        ),
                    )
        effective_idle_ttl_seconds = idle_ttl_seconds
        forwarded_affinity = (
            _forwarded_http_bridge_session_key(
                headers,
                api_key,
                forwarded_affinity_kind=forwarded_affinity_kind,
                forwarded_affinity_key=forwarded_affinity_key,
            )
            if forwarded_request
            else None
        )
        old_account_id: str | None = None
        forced_bridge_key: _HTTPBridgeSessionKey | None = None
        owner_resolution_deadline_at = request_deadline_at or (
            time.monotonic() + settings.http_responses_session_bridge_request_budget_seconds
        )
        pressure_hint_remaining = _remaining_budget_seconds(owner_resolution_deadline_at)
        if pressure_hint_remaining <= 0:
            _raise_proxy_budget_exhausted()
        try:
            pressure_capacity_hint = await _await_operation_before_hard_timeout(
                self._resolve_http_bridge_pressure_capacity_hint(
                    settings=settings,
                    max_sessions=max_sessions,
                    api_key=api_key,
                    request_model=request_model,
                ),
                timeout_seconds=pressure_hint_remaining,
                tasks=self._proxy_cleanup_tasks,
                label=f"HTTP bridge pressure capacity resolution request_id={trace_request_id}",
            )
        except TimeoutError:
            _raise_proxy_budget_exhausted()
        while True:
            owner_resolution_key = forced_bridge_key or key
            owner_resolution_remaining = _remaining_budget_seconds(owner_resolution_deadline_at)
            if owner_resolution_remaining <= 0:
                _raise_proxy_budget_exhausted()
            try:
                owner_resolution = await _await_operation_before_hard_timeout(
                    self._resolve_http_bridge_owner(
                        key=owner_resolution_key,
                        settings=settings,
                        headers=headers,
                        request_model=request_model,
                        previous_response_id=previous_response_id,
                        durable_lookup=durable_lookup,
                        gateway_safe_mode=gateway_safe_mode,
                        allow_previous_response_recovery_rebind=allow_previous_response_recovery_rebind,
                        allow_bootstrap_owner_rebind=allow_bootstrap_owner_rebind,
                        allow_forward_to_owner=allow_forward_to_owner,
                        forwarded_request=forwarded_request,
                        incoming_turn_state=incoming_turn_state,
                        incoming_session_key=incoming_session_key,
                    ),
                    timeout_seconds=owner_resolution_remaining,
                    tasks=self._proxy_cleanup_tasks,
                    label=f"HTTP bridge owner resolution request_id={trace_request_id}",
                )
            except TimeoutError:
                _raise_proxy_budget_exhausted()
            trace.loops += 1
            sessions_to_close: list[_HTTPBridgeSession] = []
            inflight_future: asyncio.Future[_HTTPBridgeSession] | None = None
            capacity_wait_future: asyncio.Future[_HTTPBridgeSession] | None = None
            owns_creation = False
            continuity_error: ProxyResponseError | None = None
            owner_forward: _HTTPBridgeOwnerForward | None = None
            force_durable_takeover = False
            missing_turn_state_alias = False
            allow_missing_turn_state_fresh_session = False
            preserve_durable_canonical_key = (
                incoming_turn_state is not None
                and forwarded_affinity is None
                and durable_lookup is not None
                and key.affinity_kind == durable_lookup.canonical_kind
                and key.affinity_key == durable_lookup.canonical_key
                and key.affinity_kind != "turn_state_header"
            )

            main_lock_wait_started_at = time.monotonic()
            async with self._http_bridge_lock:
                main_lock_acquired_at = time.monotonic()
                trace.main_lock_wait_ms += _elapsed_ms(main_lock_wait_started_at, main_lock_acquired_at) or 0
                if forced_bridge_key is not None:
                    key = forced_bridge_key
                    forced_bridge_key = None
                elif (
                    incoming_turn_state is not None
                    and forwarded_affinity is None
                    and not preserve_durable_canonical_key
                ):
                    alias_index_key = _http_bridge_turn_state_alias_key(incoming_turn_state, api_key_id)
                    alias_key = self._http_bridge_turn_state_index.get(alias_index_key)
                    if alias_key is not None:
                        key = alias_key
                        alias_session = self._http_bridge_sessions.get(alias_key)
                        if (
                            alias_session is None
                            or alias_session.closed
                            or alias_session.account.status != AccountStatus.ACTIVE
                            or not _http_bridge_session_matches_preferred_account(
                                session=alias_session,
                                previous_response_id=previous_response_id,
                                preferred_account_id=preferred_account_id,
                            )
                        ):
                            self._http_bridge_turn_state_index.pop(alias_index_key, None)
                            key = _HTTPBridgeSessionKey("turn_state_header", incoming_turn_state, api_key_id)
                        else:
                            self._promote_http_bridge_session_to_codex_affinity(
                                alias_session,
                                turn_state=incoming_turn_state,
                                settings=settings,
                            )
                            for alias in alias_session.downstream_turn_state_aliases:
                                self._http_bridge_turn_state_index[
                                    _http_bridge_turn_state_alias_key(alias, alias_session.key.api_key_id)
                                ] = alias_session.key
                            key = alias_session.key
                    elif incoming_turn_state.startswith("http_turn_"):
                        if previous_response_id is not None:
                            previous_alias_key = _http_bridge_previous_response_alias_key(
                                previous_response_id,
                                api_key_id,
                            )
                            previous_key = self._http_bridge_previous_response_index.get(previous_alias_key)
                            previous_session = None
                            if previous_key is not None:
                                previous_session = self._http_bridge_sessions.get(previous_key)
                            if (
                                previous_session is not None
                                and not previous_session.closed
                                and previous_session.account.status == AccountStatus.ACTIVE
                                and _http_bridge_session_matches_preferred_account(
                                    session=previous_session,
                                    previous_response_id=previous_response_id,
                                    preferred_account_id=preferred_account_id,
                                )
                            ):
                                key = previous_session.key
                                self._promote_http_bridge_session_to_codex_affinity(
                                    previous_session,
                                    turn_state=incoming_turn_state,
                                    settings=settings,
                                )
                                previous_session.downstream_turn_state_aliases.add(incoming_turn_state)
                                for alias in previous_session.downstream_turn_state_aliases:
                                    self._http_bridge_turn_state_index[
                                        _http_bridge_turn_state_alias_key(
                                            alias,
                                            previous_session.key.api_key_id,
                                        )
                                    ] = previous_session.key
                                continue
                            if previous_key is not None:
                                self._http_bridge_previous_response_index.pop(previous_alias_key, None)
                        if incoming_session_key is not None:
                            # Missing generated turn-state aliases are not precise enough to reuse session_header state.
                            key = _HTTPBridgeSessionKey("turn_state_header", incoming_turn_state, api_key_id)
                            allow_missing_turn_state_fresh_session = True
                        else:
                            key = _HTTPBridgeSessionKey("turn_state_header", incoming_turn_state, api_key_id)
                            missing_turn_state_alias = True

                sessions_to_close.extend(await self._prune_http_bridge_sessions_locked())
                sessions_to_close.extend(
                    await self._evict_http_bridge_parallel_prompt_cache_pressure_locked(
                        settings=settings,
                        max_sessions=max_sessions,
                        protected_key=key,
                        request_model=request_model,
                        capacity_hint=pressure_capacity_hint,
                    )
                )
                if _http_bridge_soft_sharding_allowed(
                    key,
                    incoming_turn_state=incoming_turn_state,
                    previous_response_id=previous_response_id,
                    forwarded_request=forwarded_request,
                ):
                    selected_shard_key = await self._select_http_bridge_soft_shard_key_locked(
                        key,
                        api_key=api_key,
                        request_model=request_model,
                        pending_limit=settings.http_responses_session_bridge_soft_shard_pending_limit,
                        max_shards=settings.http_responses_session_bridge_soft_shard_max_shards,
                    )
                    if selected_shard_key != key:
                        _log_http_bridge_event(
                            "soft_shard_select",
                            selected_shard_key,
                            account_id=None,
                            model=request_model,
                            detail=(
                                f"base_key={_hash_identifier(key.affinity_key)}, "
                                f"shard={_http_bridge_soft_shard_index(selected_shard_key)}"
                            ),
                            cache_key_family=selected_shard_key.affinity_kind,
                            model_class=_extract_model_class(request_model) if request_model else None,
                        )
                        key = selected_shard_key
                        durable_lookup = None

                if key != owner_resolution_key:
                    for stale_session in sessions_to_close:
                        self._schedule_http_bridge_session_close(
                            stale_session,
                            reason="owner-resolution-key-changed",
                        )
                    continue

                existing = self._http_bridge_sessions.get(key)
                if (
                    existing is not None
                    and not existing.closed
                    and existing.account.status == AccountStatus.ACTIVE
                    and _http_bridge_session_allows_api_key(existing, api_key)
                    and self._http_bridge_session_reusable_for_request_compatible(
                        session=existing,
                        key=key,
                        incoming_turn_state=incoming_turn_state,
                        previous_response_id=previous_response_id,
                        request_model=request_model,
                        required_upstream_wire_api=affinity.required_upstream_wire_api,
                    )
                    and _http_bridge_session_matches_preferred_account(
                        session=existing,
                        previous_response_id=previous_response_id,
                        preferred_account_id=preferred_account_id,
                    )
                ):
                    current_instance = settings.http_responses_session_bridge_instance_id
                    local_reuse_decision = _durable_bridge_lookup_local_reuse_decision(
                        durable_lookup,
                        current_instance=current_instance,
                        local_session_id=existing.durable_session_id,
                        local_owner_epoch=existing.durable_owner_epoch,
                    )
                    request_budget_full = self._http_bridge_session_request_budget_full(
                        existing,
                        request_model=request_model,
                    )
                    if local_reuse_decision != "reject" and not request_budget_full:
                        existing.api_key = api_key
                        existing.request_model = request_model
                        existing.last_used_at = time.monotonic()
                        if local_reuse_decision == "fence":
                            existing.durable_lease_expires_at = None
                        else:
                            self._schedule_durable_http_bridge_session_refresh(existing)
                        _log_http_bridge_event(
                            "reuse",
                            key,
                            account_id=existing.account.id,
                            model=existing.request_model,
                            pending_count=await self._http_bridge_pending_count(existing),
                            cache_key_family=key.affinity_kind,
                            model_class=_extract_model_class(existing.request_model)
                            if existing.request_model
                            else None,
                        )
                        trace.log(
                            outcome="reuse",
                            key=key,
                            account_id=existing.account.id,
                            model=existing.request_model,
                            request_stage=request_stage,
                            preferred_account_id=preferred_account_id,
                            require_preferred_account=False,
                        )
                        existing.affinity = _merge_affinity_required_wire_api(existing.affinity, affinity)
                        return self._acquire_http_bridge_submit_lease_locked(existing)
                    if local_reuse_decision == "reject":
                        self._mark_stale_durable_http_bridge_session_locked(existing)
                    else:
                        existing_busy_count = await self._http_bridge_replacement_busy_count(existing)
                        parallel_key = self._http_bridge_busy_parallel_replacement_key_locked(
                            key=key,
                            session=existing,
                            busy_count=existing_busy_count,
                            max_sessions=max_sessions,
                            incoming_turn_state=incoming_turn_state,
                            previous_response_id=previous_response_id,
                        )
                        if parallel_key is not None:
                            key = parallel_key
                            durable_lookup = None
                            existing = self._http_bridge_sessions.get(key)
                            if existing is None or existing.closed:
                                forced_bridge_key = parallel_key
                                continue
                    old_account_id = existing.account.id
                    if local_reuse_decision != "reject":
                        self._detach_http_bridge_session_indexes_locked(existing)
                    sessions_to_close.append(existing)
                    existing = None
                if existing is not None and not existing.closed and existing.account.status == AccountStatus.ACTIVE:
                    existing_busy_count = await self._http_bridge_replacement_busy_count(existing)
                    parallel_key = self._http_bridge_busy_parallel_replacement_key_locked(
                        key=key,
                        session=existing,
                        busy_count=existing_busy_count,
                        max_sessions=max_sessions,
                        incoming_turn_state=incoming_turn_state,
                        previous_response_id=previous_response_id,
                    )
                    if parallel_key is not None:
                        key = parallel_key
                        durable_lookup = None
                        existing = self._http_bridge_sessions.get(key)
                        if existing is None or existing.closed:
                            forced_bridge_key = parallel_key
                            continue
                    old_account_id = existing.account.id
                    self._detach_http_bridge_session_indexes_locked(existing)
                    sessions_to_close.append(existing)
                    existing = None

                if shutdown_state.is_bridge_drain_active() and not _http_bridge_can_recover_during_drain(
                    key=key,
                    headers=headers,
                    previous_response_id=previous_response_id,
                    durable_lookup=durable_lookup,
                ):
                    raise ProxyResponseError(
                        503,
                        openai_error(
                            "bridge_drain_active",
                            "HTTP bridge is draining — new sessions not accepted during shutdown",
                            error_type="server_error",
                        ),
                    )
                if shutdown_state.is_bridge_drain_active():
                    _record_bridge_drain_recovery_allowed()

                owner_check_required = owner_resolution.owner_check_required
                owner_forward = owner_resolution.owner_forward
                force_durable_takeover = owner_resolution.force_durable_takeover
                if existing is not None:
                    existing_busy_count = await self._http_bridge_replacement_busy_count(existing)
                    parallel_key = self._http_bridge_busy_parallel_replacement_key_locked(
                        key=key,
                        session=existing,
                        busy_count=existing_busy_count,
                        max_sessions=max_sessions,
                        incoming_turn_state=incoming_turn_state,
                        previous_response_id=previous_response_id,
                    )
                    if parallel_key is not None:
                        forced_bridge_key = parallel_key
                        durable_lookup = None
                        continue
                    old_account_id = existing.account.id
                    _log_http_bridge_event(
                        "discard_stale",
                        key,
                        account_id=existing.account.id,
                        model=existing.request_model,
                        cache_key_family=key.affinity_kind,
                        model_class=_extract_model_class(existing.request_model) if existing.request_model else None,
                    )
                    self._detach_http_bridge_session_indexes_locked(existing)
                    sessions_to_close.append(existing)

                inflight_future = self._http_bridge_inflight_sessions.get(key)
                if (
                    previous_response_id is not None
                    and inflight_future is None
                    and (existing is None or existing.closed or existing.account.status != AccountStatus.ACTIVE)
                ):
                    previous_alias_key = _http_bridge_previous_response_alias_key(previous_response_id, api_key_id)
                    previous_key = self._http_bridge_previous_response_index.get(previous_alias_key)
                    if previous_key is not None:
                        previous_session = self._http_bridge_sessions.get(previous_key)
                        if (
                            previous_session is not None
                            and not previous_session.closed
                            and previous_session.account.status == AccountStatus.ACTIVE
                            and self._http_bridge_session_reusable_for_request_compatible(
                                session=previous_session,
                                key=previous_key,
                                incoming_turn_state=incoming_turn_state,
                                previous_response_id=previous_response_id,
                                request_model=request_model,
                                required_upstream_wire_api=affinity.required_upstream_wire_api,
                            )
                            and not self._http_bridge_session_request_budget_full(
                                previous_session,
                                request_model=request_model,
                            )
                        ):
                            previous_reuse_decision = _durable_bridge_lookup_local_reuse_decision(
                                durable_lookup,
                                current_instance=settings.http_responses_session_bridge_instance_id,
                                local_session_id=previous_session.durable_session_id,
                                local_owner_epoch=previous_session.durable_owner_epoch,
                            )
                            if previous_reuse_decision == "reject":
                                old_account_id = previous_session.account.id
                                self._mark_stale_durable_http_bridge_session_locked(previous_session)
                                sessions_to_close.append(previous_session)
                                self._http_bridge_previous_response_index.pop(previous_alias_key, None)
                            else:
                                key = previous_session.key
                                existing = previous_session
                                inflight_future = self._http_bridge_inflight_sessions.get(previous_key)
                                if incoming_turn_state:
                                    self._promote_http_bridge_session_to_codex_affinity(
                                        previous_session,
                                        turn_state=incoming_turn_state,
                                        settings=settings,
                                    )
                                    previous_session.downstream_turn_state_aliases.add(incoming_turn_state)
                                    for alias in previous_session.downstream_turn_state_aliases:
                                        self._http_bridge_turn_state_index[
                                            _http_bridge_turn_state_alias_key(
                                                alias,
                                                previous_session.key.api_key_id,
                                            )
                                        ] = previous_session.key
                                if inflight_future is None:
                                    previous_session.affinity = _merge_affinity_required_wire_api(
                                        previous_session.affinity,
                                        affinity,
                                    )
                                    previous_session.request_model = request_model
                                    previous_session.last_used_at = time.monotonic()
                                    if previous_reuse_decision == "fence":
                                        previous_session.durable_lease_expires_at = None
                                    else:
                                        self._schedule_durable_http_bridge_session_refresh(previous_session)
                                    _log_http_bridge_event(
                                        "reuse",
                                        key,
                                        account_id=previous_session.account.id,
                                        model=previous_session.request_model,
                                        pending_count=await self._http_bridge_pending_count(previous_session),
                                        cache_key_family=key.affinity_kind,
                                        model_class=_extract_model_class(previous_session.request_model)
                                        if previous_session.request_model
                                        else None,
                                    )
                                    trace.log(
                                        outcome="previous_response_reuse",
                                        key=key,
                                        account_id=previous_session.account.id,
                                        model=previous_session.request_model,
                                        request_stage=request_stage,
                                        preferred_account_id=preferred_account_id,
                                        require_preferred_account=False,
                                    )
                                    return self._acquire_http_bridge_submit_lease_locked(previous_session)
                        else:
                            self._http_bridge_previous_response_index.pop(previous_alias_key, None)
                if (
                    previous_response_id is not None
                    and not allow_missing_turn_state_fresh_session
                    and not allow_previous_response_recovery_rebind
                    and durable_lookup is None
                ):
                    self._record_http_bridge_continuity_fail_closed(
                        reason="continuity_lost",
                        previous_response_id=previous_response_id,
                        session_id=incoming_turn_state or incoming_session_key,
                    )
                    continuity_error = ProxyResponseError(502, _http_bridge_continuity_lost_error_envelope())
                elif missing_turn_state_alias and inflight_future is None and durable_lookup is None:
                    turn_state_scope_conflict = incoming_turn_state is not None and any(
                        alias == incoming_turn_state and alias_api_key != api_key_id
                        for alias, alias_api_key in self._http_bridge_turn_state_index
                    )
                    if turn_state_scope_conflict:
                        self._record_http_bridge_continuity_fail_closed(
                            reason="turn_state_scope_conflict",
                            previous_response_id=previous_response_id,
                            session_id=incoming_turn_state,
                        )
                        continuity_error = ProxyResponseError(
                            409,
                            openai_error(
                                "bridge_instance_mismatch",
                                "HTTP bridge turn-state is bound to a different API key scope",
                                error_type="server_error",
                            ),
                        )
                    elif (
                        incoming_turn_state is not None
                        and incoming_turn_state.startswith("http_turn_")
                        and not allow_forward_to_owner
                    ):
                        self._record_http_bridge_continuity_fail_closed(
                            reason="generated_turn_state_continuity_lost",
                            previous_response_id=previous_response_id,
                            session_id=incoming_turn_state,
                        )
                        continuity_error = ProxyResponseError(
                            409,
                            openai_error(
                                "bridge_instance_mismatch",
                                "HTTP bridge continuity was lost for generated turn-state",
                                error_type="server_error",
                            ),
                        )
                    else:
                        _log_http_bridge_event(
                            "turn_state_alias_miss_local_rebind",
                            key,
                            account_id=None,
                            model=request_model,
                            detail="outcome=local_rebind_without_alias",
                            cache_key_family=key.affinity_kind,
                            model_class=_extract_model_class(request_model) if request_model else None,
                            owner_check_applied=owner_check_required,
                        )
                elif inflight_future is None:
                    reservation = await self._reserve_http_bridge_creation_slot_locked(
                        key=key,
                        max_sessions=max_sessions,
                        request_model=request_model,
                    )
                    sessions_to_close.extend(reservation.evicted_sessions)
                    inflight_future = reservation.inflight_future
                    capacity_wait_future = reservation.capacity_wait_future
                    owns_creation = reservation.owns_creation
                trace.main_lock_body_ms += _elapsed_ms(main_lock_acquired_at, time.monotonic()) or 0

            stale_detached: list[asyncio.Future[None]] = []
            for stale_session in sessions_to_close:
                stale_close_started_at = time.monotonic()
                stale_detached.append(
                    self._schedule_http_bridge_session_close(stale_session, reason="get_or_create_stale")
                )
                trace.stale_close_ms += _elapsed_ms(stale_close_started_at, time.monotonic()) or 0
            if stale_detached:
                try:
                    await asyncio.gather(*(asyncio.shield(detached) for detached in stale_detached))
                except BaseException as exc:
                    failure = exc
                    if owns_creation:

                        async def abandon_inflight_creation() -> None:
                            async with self._http_bridge_lock:
                                current_future = self._http_bridge_inflight_sessions.get(key)
                                if current_future is not inflight_future:
                                    return
                                self._http_bridge_inflight_sessions.pop(key, None)
                                if inflight_future is None or inflight_future.done():
                                    return
                                if isinstance(failure, asyncio.CancelledError):
                                    inflight_future.cancel()
                                else:
                                    inflight_future.set_exception(failure)
                                    inflight_future.exception()

                        await _await_shielded_cleanup(
                            abandon_inflight_creation(),
                            label="stale HTTP bridge inflight creation cleanup",
                        )
                    raise

            if owner_forward is not None:
                trace.log(
                    outcome="owner_forward",
                    key=key,
                    account_id=None,
                    model=request_model,
                    request_stage=request_stage,
                    preferred_account_id=preferred_account_id,
                    require_preferred_account=False,
                )
                return owner_forward

            if continuity_error is not None:
                raise continuity_error

            if capacity_wait_future is not None:
                try:
                    capacity_wait_started_at = time.monotonic()
                    await asyncio.shield(capacity_wait_future)
                    trace.capacity_wait_ms += _elapsed_ms(capacity_wait_started_at, time.monotonic()) or 0
                except asyncio.CancelledError:
                    if capacity_wait_future.cancelled():
                        continue
                    raise
                except ProxyResponseError:
                    raise
                except Exception:
                    pass
                continue

            if inflight_future is not None and not owns_creation:
                try:
                    inflight_wait_started_at = time.monotonic()
                    session = await asyncio.shield(inflight_future)
                    trace.inflight_wait_ms += _elapsed_ms(inflight_wait_started_at, time.monotonic()) or 0
                except asyncio.CancelledError:
                    if inflight_future.cancelled():
                        continue
                    raise
                except Exception:
                    raise
                if session is None:
                    continue
                inflight_reuse_rejected = False
                if (
                    not session.closed
                    and session.account.status == AccountStatus.ACTIVE
                    and _http_bridge_session_allows_api_key(session, api_key)
                    and self._http_bridge_session_reusable_for_request_compatible(
                        session=session,
                        key=key,
                        incoming_turn_state=incoming_turn_state,
                        previous_response_id=previous_response_id,
                        request_model=request_model,
                        required_upstream_wire_api=affinity.required_upstream_wire_api,
                    )
                    and _http_bridge_session_matches_preferred_account(
                        session=session,
                        previous_response_id=previous_response_id,
                        preferred_account_id=preferred_account_id,
                    )
                ):
                    current_instance = settings.http_responses_session_bridge_instance_id
                    inflight_reuse_decision = _durable_bridge_lookup_local_reuse_decision(
                        durable_lookup,
                        current_instance=current_instance,
                        local_session_id=session.durable_session_id,
                        local_owner_epoch=session.durable_owner_epoch,
                    )
                    request_budget_full = self._http_bridge_session_request_budget_full(
                        session,
                        request_model=request_model,
                    )
                    if inflight_reuse_decision != "reject" and not request_budget_full:
                        session.api_key = api_key
                        session.affinity = _merge_affinity_required_wire_api(session.affinity, affinity)
                        session.request_model = request_model
                        session.last_used_at = time.monotonic()
                        if inflight_reuse_decision == "fence":
                            session.durable_lease_expires_at = None
                        trace.log(
                            outcome="inflight_reuse",
                            key=key,
                            account_id=session.account.id,
                            model=session.request_model,
                            request_stage=request_stage,
                            preferred_account_id=preferred_account_id,
                            require_preferred_account=False,
                        )
                        async with self._http_bridge_lock:
                            return self._acquire_http_bridge_submit_lease_locked(session)
                    inflight_reuse_rejected = inflight_reuse_decision == "reject"
                if inflight_reuse_rejected:
                    old_account_id = session.account.id
                    async with self._http_bridge_lock:
                        self._mark_stale_durable_http_bridge_session_locked(session)
                    detached = self._schedule_http_bridge_session_close(
                        session,
                        reason="durable-inflight-owner-epoch-mismatch",
                    )
                    await asyncio.shield(detached)
                    continue
                if not session.closed and session.account.status == AccountStatus.ACTIVE:
                    session_busy_count = await self._http_bridge_replacement_busy_count(session)
                    parallel_key = None
                    if session_busy_count:
                        async with self._http_bridge_lock:
                            parallel_key = self._http_bridge_busy_parallel_replacement_key_locked(
                                key=key,
                                session=session,
                                busy_count=session_busy_count,
                                max_sessions=max_sessions,
                                incoming_turn_state=incoming_turn_state,
                                previous_response_id=previous_response_id,
                            )
                    if parallel_key is not None:
                        forced_bridge_key = parallel_key
                        durable_lookup = None
                        continue
                    old_account_id = session.account.id
                    async with self._http_bridge_lock:
                        self._detach_http_bridge_session_indexes_locked(session)
                    detached = self._schedule_http_bridge_session_close(
                        session,
                        reason="incompatible-inflight-result",
                    )
                    await asyncio.shield(detached)
                continue

            created_session: _HTTPBridgeSession | None = None
            session_registered = False
            hard_session_account_id = old_account_id if old_account_id is not None and key.strength == "hard" else None
            create_preferred_account_id = preferred_account_id or hard_session_account_id
            require_preferred_account = (
                previous_response_id is not None and preferred_account_id is not None
            ) or hard_session_account_id is not None
            allow_durable_takeover = (
                force_durable_takeover
                or not durable_account_supports_request_model
                or _http_bridge_allow_durable_takeover(durable_lookup)
            )
            try:
                create_session_started_at = time.monotonic()
                created_session = await self._create_http_bridge_session_compatible(
                    key,
                    headers=headers,
                    affinity=affinity,
                    api_key=api_key,
                    request_model=request_model,
                    idle_ttl_seconds=effective_idle_ttl_seconds,
                    request_stage=request_stage,
                    preferred_account_id=create_preferred_account_id,
                    require_preferred_account=require_preferred_account,
                    request_deadline_at=request_deadline_at,
                )
                trace.create_session_ms += _elapsed_ms(create_session_started_at, time.monotonic()) or 0
                await self._claim_durable_http_bridge_session(
                    created_session,
                    allow_takeover=allow_durable_takeover,
                )
                if request_deadline_at is not None and _remaining_budget_seconds(request_deadline_at) <= 0:
                    _raise_proxy_budget_exhausted()
                async with self._http_bridge_lock:
                    if request_deadline_at is not None and _remaining_budget_seconds(request_deadline_at) <= 0:
                        _raise_proxy_budget_exhausted()
                    current_future = self._http_bridge_inflight_sessions.get(key)
                    if current_future is inflight_future:
                        self._http_bridge_inflight_sessions.pop(key, None)
                        self._http_bridge_sessions[key] = created_session
                        session_registered = True
                        if inflight_future is not None and not inflight_future.done():
                            inflight_future.set_result(created_session)
            except BaseException as exc:
                async with self._http_bridge_lock:
                    current_future = self._http_bridge_inflight_sessions.get(key)
                    if current_future is inflight_future:
                        self._http_bridge_inflight_sessions.pop(key, None)
                        if inflight_future is not None and not inflight_future.done():
                            if isinstance(exc, asyncio.CancelledError):
                                inflight_future.cancel()
                            else:
                                inflight_future.set_exception(exc)
                                inflight_future.exception()
                if created_session is not None and not session_registered:
                    await self._close_http_bridge_session(created_session)
                raise
            assert created_session is not None
            _log_http_bridge_event(
                "create",
                key,
                account_id=created_session.account.id,
                model=created_session.request_model,
                detail=(
                    f"request_stage={request_stage}, preferred_account_id={preferred_account_id}, "
                    f"selected_account_id={created_session.account.id}, "
                    f"durable_session_id={created_session.durable_session_id}"
                ),
                cache_key_family=key.affinity_kind,
                model_class=_extract_model_class(created_session.request_model)
                if created_session.request_model
                else None,
            )
            if old_account_id is not None and old_account_id != created_session.account.id:
                _log_http_bridge_event(
                    "reallocation_orphan",
                    key,
                    account_id=created_session.account.id,
                    model=created_session.request_model,
                    detail=f"old_account={old_account_id}",
                    cache_key_family=key.affinity_kind,
                    model_class=_extract_model_class(created_session.request_model)
                    if created_session.request_model
                    else None,
                )
            trace.log(
                outcome="create",
                key=key,
                account_id=created_session.account.id,
                model=created_session.request_model,
                request_stage=request_stage,
                preferred_account_id=preferred_account_id,
                require_preferred_account=require_preferred_account,
            )
            async with self._http_bridge_lock:
                return self._acquire_http_bridge_submit_lease_locked(created_session)
