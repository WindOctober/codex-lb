from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Protocol

import anyio

from app.core import shutdown as shutdown_state
from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.core.metrics.prometheus import (
    PROMETHEUS_AVAILABLE,
    bridge_durable_recover_total,
    bridge_instance_mismatch_total,
)
from app.db.models import StickySessionKind
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _extract_model_class, _headers_with_turn_state
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_previous_response_alias_key,
    _http_bridge_turn_state_alias_key,
)
from app.modules.proxy._service.observability import (
    _elapsed_ms,
    _log_http_bridge_event,
    _record_bridge_reattach,
)
from app.modules.proxy._service.support import (
    _AffinityPolicy,
    _await_cancelled_task,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _WebSocketRequestState,
)
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeSessionCoordinator
from app.modules.proxy.ring_membership import RING_STALE_THRESHOLD_SECONDS

logger = logging.getLogger("app.modules.proxy.service")


def _is_missing_durable_bridge_table_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "http_bridge_sessions" not in message and "http_bridge_session_aliases" not in message:
        return False
    return "no such table" in message or "does not exist" in message or "undefinedtable" in message


def _http_bridge_durable_lease_ttl_seconds() -> float:
    return float(RING_STALE_THRESHOLD_SECONDS)


def _http_bridge_durable_renew_interval_seconds() -> float:
    return max(1.0, _http_bridge_durable_lease_ttl_seconds() / 3.0)


class _HTTPBridgeLifecycleService(Protocol):
    _durable_bridge: DurableBridgeSessionCoordinator
    _http_bridge_background_close_tasks: set[asyncio.Task[None]]
    _http_bridge_inflight_sessions: dict[_HTTPBridgeSessionKey, asyncio.Future[_HTTPBridgeSession]]
    _http_bridge_lock: anyio.Lock
    _http_bridge_previous_response_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey]
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]
    _http_bridge_turn_state_index: dict[tuple[str, str | None], _HTTPBridgeSessionKey]

    @staticmethod
    def _http_bridge_runtime_settings() -> Settings: ...

    async def _http_bridge_pending_count(self, session: _HTTPBridgeSession) -> int: ...

    async def _fail_pending_websocket_requests(
        self,
        *,
        account_id_value: str | None,
        pending_requests: deque[_WebSocketRequestState],
        pending_lock: anyio.Lock,
        error_code: str,
        error_message: str,
        api_key: ApiKeyData | None,
        response_create_gate: asyncio.Semaphore | None = None,
    ) -> None: ...

    async def _close_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
    ) -> None: ...

    async def _unregister_http_bridge_turn_states(self, session: _HTTPBridgeSession) -> None: ...

    async def _unregister_http_bridge_previous_response_ids(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_turn_states_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_previous_response_ids_locked(self, session: _HTTPBridgeSession) -> None: ...

    async def _settle_durable_http_bridge_session_refresh(self, session: _HTTPBridgeSession) -> None: ...

    def _defer_next_durable_http_bridge_session_refresh(
        self,
        session: _HTTPBridgeSession,
        *,
        now: float | None = None,
    ) -> None: ...

    async def _refresh_durable_http_bridge_session(self, session: _HTTPBridgeSession) -> None: ...


class _HTTPBridgeLifecycleMixin:
    async def close_all_http_bridge_sessions(self: _HTTPBridgeLifecycleService) -> None:
        async with self._http_bridge_lock:
            sessions_to_close = list(self._http_bridge_sessions.values())
            inflight_futures = list(self._http_bridge_inflight_sessions.values())
            self._http_bridge_sessions.clear()
            self._http_bridge_inflight_sessions.clear()
            self._http_bridge_previous_response_index.clear()

        shutdown_error = ProxyResponseError(
            503,
            openai_error(
                "upstream_unavailable",
                "HTTP responses session bridge is shutting down",
                error_type="server_error",
            ),
        )
        for inflight_future in inflight_futures:
            if inflight_future.done():
                continue
            inflight_future.set_exception(shutdown_error)
            inflight_future.exception()

        for session in sessions_to_close:
            await self._close_http_bridge_session(session)
        background_close_tasks = tuple(self._http_bridge_background_close_tasks)
        if background_close_tasks:
            await asyncio.gather(*background_close_tasks, return_exceptions=True)


    async def mark_http_bridge_draining(self: _HTTPBridgeLifecycleService) -> None:
        try:
            await self._durable_bridge.mark_instance_draining(
                instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
            )
        except Exception:
            logger.warning("Failed to mark durable HTTP bridge sessions draining", exc_info=True)


    async def _prune_http_bridge_sessions_locked(self: _HTTPBridgeLifecycleService) -> None:
        now = time.monotonic()
        stale_keys: list[_HTTPBridgeSessionKey] = []
        for key, session in self._http_bridge_sessions.items():
            if session.closed:
                stale_keys.append(key)
                continue
            pending_count = await self._http_bridge_pending_count(session)
            if pending_count or session.submit_lease_count > 0:
                continue
            if now - session.last_used_at < session.idle_ttl_seconds:
                continue
            stale_keys.append(key)
        for key in stale_keys:
            session = self._http_bridge_sessions.pop(key, None)
            if session is not None:
                _log_http_bridge_event(
                    "evict_idle",
                    key,
                    account_id=session.account.id,
                    model=session.request_model,
                    cache_key_family=key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )
                await self._close_http_bridge_session(session, turn_state_lock_held=True)


    async def _detach_http_bridge_session_for_background_close(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
    ) -> None:
        session.closed = True
        session_lease = session.account_model_session_lease
        if session_lease is not None:
            session.account_model_session_lease = None
            session_lease.release()
        await self._unregister_http_bridge_turn_states(session)
        await self._unregister_http_bridge_previous_response_ids(session)


    def _schedule_http_bridge_session_close(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        reason: str,
    ) -> None:
        async def close_session() -> None:
            started_at = time.monotonic()
            try:
                await self._close_http_bridge_session(session)
            except Exception:
                logger.warning(
                    "Background HTTP bridge session close failed reason=%s account_id=%s model=%s",
                    reason,
                    session.account.id,
                    session.request_model,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "http_bridge_background_close_complete reason=%s account_id=%s model=%s elapsed_ms=%s",
                    reason,
                    session.account.id,
                    session.request_model,
                    _elapsed_ms(started_at, time.monotonic()),
                )

        task = asyncio.create_task(close_session())
        self._http_bridge_background_close_tasks.add(task)
        task.add_done_callback(self._http_bridge_background_close_tasks.discard)


    async def _close_http_bridge_session(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
    ) -> None:
        session.closed = True
        session_lease = session.account_model_session_lease
        if session_lease is not None:
            session.account_model_session_lease = None
            session_lease.release()
        if turn_state_lock_held:
            self._unregister_http_bridge_turn_states_locked(session)
            self._unregister_http_bridge_previous_response_ids_locked(session)
        else:
            await self._unregister_http_bridge_turn_states(session)
            await self._unregister_http_bridge_previous_response_ids(session)
        if session.upstream_reader is not None and not skip_reader_task:
            await _await_cancelled_task(session.upstream_reader, label="http bridge upstream reader")
        try:
            await session.upstream.close()
        except Exception:
            logger.debug("Failed to close HTTP bridge upstream websocket", exc_info=True)
        pending_requests = getattr(session, "pending_requests", None)
        pending_lock = getattr(session, "pending_lock", None)
        response_create_gate = getattr(session, "response_create_gate", None)
        if pending_requests is not None and pending_lock is not None:
            async with pending_lock:
                session.queued_request_count = 0
            await self._fail_pending_websocket_requests(
                account_id_value=session.account.id,
                pending_requests=pending_requests,
                pending_lock=pending_lock,
                error_code="stream_incomplete",
                error_message="HTTP bridge session closed before response.completed",
                api_key=None,
                response_create_gate=response_create_gate,
            )
        if session.durable_session_id is not None and session.durable_owner_epoch is not None:
            await self._settle_durable_http_bridge_session_refresh(session)
            try:
                await self._durable_bridge.release_live_session(
                    session_id=session.durable_session_id,
                instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                    owner_epoch=session.durable_owner_epoch,
                    draining=shutdown_state.is_bridge_drain_active(),
                )
            except Exception:
                logger.warning("Failed to release durable HTTP bridge session", exc_info=True)
        _log_http_bridge_event(
            "close",
            session.key,
            account_id=session.account.id,
            model=session.request_model,
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )


    async def _evict_http_bridge_session_after_upstream_disconnect(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        error_message: str,
    ) -> None:
        async with self._http_bridge_lock:
            if self._http_bridge_sessions.get(session.key) is session:
                self._http_bridge_sessions.pop(session.key, None)
        _log_http_bridge_event(
            "evict_upstream_disconnected",
            session.key,
            account_id=session.account.id,
            model=session.request_model,
            detail=error_message,
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )
        await self._close_http_bridge_session(session, skip_reader_task=True)


    async def _register_http_bridge_turn_state(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
        turn_state: str,
    ) -> None:
        async with self._http_bridge_lock:
            if session.closed:
                return
            session.downstream_turn_state_aliases.add(turn_state)
            if session.downstream_turn_state is None:
                session.downstream_turn_state = turn_state
            for alias in session.downstream_turn_state_aliases:
                self._http_bridge_turn_state_index[_http_bridge_turn_state_alias_key(alias, session.key.api_key_id)] = (
                    session.key
                )
        if session.durable_session_id is not None and session.durable_owner_epoch is not None:
            try:
                await self._durable_bridge.register_turn_state(
                    session_id=session.durable_session_id,
                    api_key_id=session.key.api_key_id,
                instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                    owner_epoch=session.durable_owner_epoch,
                    turn_state=turn_state,
                    lease_ttl_seconds=_http_bridge_durable_lease_ttl_seconds(),
                )
                self._defer_next_durable_http_bridge_session_refresh(session)
            except Exception:
                logger.warning("Failed to persist durable HTTP bridge turn-state alias", exc_info=True)


    async def _register_http_bridge_previous_response_id(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        response_id: str,
        *,
        input_item_count: int | None = None,
        input_full_fingerprint: str | None = None,
    ) -> None:
        stripped_response_id = response_id.strip()
        if not stripped_response_id:
            return
        async with self._http_bridge_lock:
            if session.closed:
                return
            alias_key = _http_bridge_previous_response_alias_key(stripped_response_id, session.key.api_key_id)
            self._http_bridge_previous_response_index[alias_key] = session.key
            session.previous_response_ids.add(stripped_response_id)
        if session.durable_session_id is not None and session.durable_owner_epoch is not None:
            try:
                await self._durable_bridge.register_previous_response_id(
                    session_id=session.durable_session_id,
                    api_key_id=session.key.api_key_id,
                instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                    owner_epoch=session.durable_owner_epoch,
                    response_id=stripped_response_id,
                    lease_ttl_seconds=_http_bridge_durable_lease_ttl_seconds(),
                    input_item_count=input_item_count,
                    input_full_fingerprint=input_full_fingerprint,
                )
                self._defer_next_durable_http_bridge_session_refresh(session)
            except Exception:
                logger.warning("Failed to persist durable HTTP bridge previous_response_id alias", exc_info=True)


    async def _unregister_http_bridge_turn_states(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        async with self._http_bridge_lock:
            self._unregister_http_bridge_turn_states_locked(session)


    async def _unregister_http_bridge_previous_response_ids(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        async with self._http_bridge_lock:
            self._unregister_http_bridge_previous_response_ids_locked(session)


    def _unregister_http_bridge_turn_states_locked(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        aliases = tuple(session.downstream_turn_state_aliases)
        for alias in aliases:
            self._http_bridge_turn_state_index.pop(
                _http_bridge_turn_state_alias_key(alias, session.key.api_key_id),
                None,
            )
        session.downstream_turn_state_aliases.clear()


    def _unregister_http_bridge_previous_response_ids_locked(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        response_ids = tuple(session.previous_response_ids)
        for response_id in response_ids:
            self._http_bridge_previous_response_index.pop(
                _http_bridge_previous_response_alias_key(response_id, session.key.api_key_id),
                None,
            )
        session.previous_response_ids.clear()


    def _promote_http_bridge_session_to_codex_affinity(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        turn_state: str,
        settings: Settings,
    ) -> None:
        session.affinity = _AffinityPolicy(key=turn_state, kind=StickySessionKind.CODEX_SESSION)
        session.codex_session = True
        session.downstream_turn_state = turn_state
        session.downstream_turn_state_aliases.add(turn_state)
        session.idle_ttl_seconds = max(
            session.idle_ttl_seconds,
            float(settings.http_responses_session_bridge_codex_idle_ttl_seconds),
        )
        session.headers = _headers_with_turn_state(session.headers, turn_state)


    async def _claim_durable_http_bridge_session(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        allow_takeover: bool,
    ) -> None:
        current_instance = self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id
        try:
            lookup = await self._durable_bridge.claim_live_session(
                session_key_kind=session.key.affinity_kind,
                session_key_value=session.key.affinity_key,
                api_key_id=session.key.api_key_id,
                instance_id=current_instance,
                lease_ttl_seconds=_http_bridge_durable_lease_ttl_seconds(),
                account_id=session.account.id,
                model=session.request_model,
                service_tier=None,
                latest_turn_state=session.downstream_turn_state,
                latest_response_id=None,
                allow_takeover=allow_takeover,
                latest_input_item_count=session.last_completed_input_count
                if session.last_completed_input_count > 0
                else None,
                latest_input_full_fingerprint=session.last_completed_input_prefix_fingerprint
                if session.last_completed_input_count > 0
                else None,
            )
            if lookup.owner_instance_id != current_instance:
                _log_http_bridge_event(
                    "owner_mismatch_retry",
                    session.key,
                    account_id=None,
                    model=session.request_model,
                    detail=(
                        "expected_instance="
                        f"{lookup.owner_instance_id}, current_instance={current_instance}, outcome=claim_rejected"
                    ),
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                    owner_check_applied=True,
                )
                if PROMETHEUS_AVAILABLE and bridge_instance_mismatch_total is not None:
                    bridge_instance_mismatch_total.labels(outcome="retry").inc()
                raise ProxyResponseError(
                    409,
                    openai_error(
                        "bridge_instance_mismatch",
                        "HTTP bridge session is owned by a different instance; retry to reach the correct replica",
                        error_type="server_error",
                    ),
                )
            session.durable_session_id = lookup.session_id
            session.durable_owner_epoch = lookup.owner_epoch
            self._defer_next_durable_http_bridge_session_refresh(session)
            session.headers = _headers_with_turn_state(session.headers, session.downstream_turn_state)
            if (
                PROMETHEUS_AVAILABLE
                and bridge_durable_recover_total is not None
                and allow_takeover
                and lookup.owner_epoch > 1
            ):
                bridge_durable_recover_total.labels(path="restart_takeover").inc()
                _record_bridge_reattach(path="restart_takeover", outcome="success")
            if session.key.affinity_kind == "session_header":
                await self._durable_bridge.register_session_header(
                    session_id=lookup.session_id,
                    api_key_id=session.key.api_key_id,
                    session_header=session.key.affinity_key,
                )
        except Exception as exc:
            if _is_missing_durable_bridge_table_error(exc):
                logger.warning("Durable bridge tables missing; using in-memory bridge session fallback", exc_info=True)
                return
            raise


    async def _refresh_durable_http_bridge_session(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
    ) -> None:
        if session.durable_session_id is None or session.durable_owner_epoch is None:
            return
        try:
            lookup = await self._durable_bridge.renew_live_session(
                session_id=session.durable_session_id,
                api_key_id=session.key.api_key_id,
                instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                owner_epoch=session.durable_owner_epoch,
                lease_ttl_seconds=_http_bridge_durable_lease_ttl_seconds(),
                latest_turn_state=session.downstream_turn_state,
                latest_response_id=None,
                latest_input_item_count=session.last_completed_input_count
                if session.last_completed_input_count > 0
                else None,
                latest_input_full_fingerprint=session.last_completed_input_prefix_fingerprint
                if session.last_completed_input_count > 0
                else None,
            )
            if lookup is not None:
                session.durable_owner_epoch = lookup.owner_epoch
        except Exception:
            logger.warning("Failed to renew durable HTTP bridge session lease", exc_info=True)


    def _defer_next_durable_http_bridge_session_refresh(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else now
        session.durable_renew_after = current + _http_bridge_durable_renew_interval_seconds()


    def _schedule_durable_http_bridge_session_refresh(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        if session.closed or session.durable_session_id is None or session.durable_owner_epoch is None:
            return
        current = time.monotonic()
        if current < session.durable_renew_after:
            return
        existing_task = session.durable_renew_task
        if existing_task is not None and not existing_task.done():
            return

        self._defer_next_durable_http_bridge_session_refresh(session, now=current)

        async def refresh() -> None:
            try:
                await self._refresh_durable_http_bridge_session(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Unexpected durable HTTP bridge lease refresh failure", exc_info=True)

        task = asyncio.create_task(refresh())
        session.durable_renew_task = task

        def clear_completed_task(completed: asyncio.Task[None]) -> None:
            if session.durable_renew_task is completed:
                session.durable_renew_task = None

        task.add_done_callback(clear_completed_task)


    async def _settle_durable_http_bridge_session_refresh(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        task = session.durable_renew_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                return
            raise
        except Exception:
            logger.warning("Failed while settling durable HTTP bridge lease refresh", exc_info=True)
