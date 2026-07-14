from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Protocol

import anyio

from app.core import shutdown as shutdown_state
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamWebSocketMessage
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.core.metrics.prometheus import (
    PROMETHEUS_AVAILABLE,
    bridge_durable_recover_total,
    bridge_instance_mismatch_total,
)
from app.core.utils.time import to_utc_naive, utcnow
from app.db.models import StickySessionKind
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
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
    _await_operation_before_hard_timeout,
    _await_shielded_cleanup,
    _close_tracked_background_tasks,
    _DiscardedRequestAccounting,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
    _merge_affinity_required_wire_api,
    _schedule_tracked_background_task,
    _track_existing_background_task,
    _WebSocketRequestState,
)
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeSessionCoordinator
from app.modules.proxy.ring_membership import RING_STALE_THRESHOLD_SECONDS

logger = logging.getLogger("app.modules.proxy.service")

_HTTP_BRIDGE_SHUTDOWN_CLOSE_TIMEOUT_SECONDS = 5.0
_HTTP_BRIDGE_SHUTDOWN_MARK_DRAINING_TIMEOUT_SECONDS = 1.0
_HTTP_BRIDGE_CLOSE_RECEIVE_OBSERVATION_TIMEOUT_SECONDS = 1.0
_HTTP_BRIDGE_RECONCILER_UPSTREAM_CLOSE_TIMEOUT_SECONDS = 1.0


async def _observe_http_bridge_receive_owners(
    session: _HTTPBridgeSession,
    *,
    timeout_seconds: float,
) -> tuple[bool, UpstreamWebSocketMessage | None]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    upstream_reader = session.upstream_reader
    if upstream_reader is not None:
        if not upstream_reader.done():
            remaining = max(0.0, deadline - time.monotonic())
            done, _pending = await asyncio.wait({upstream_reader}, timeout=remaining)
            if upstream_reader not in done:
                return False, None
        try:
            upstream_reader.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("HTTP bridge reader failed during close observation", exc_info=True)

    detached_receive = session.detached_upstream_receive
    if detached_receive is None:
        return True, None
    if not detached_receive.done():
        remaining = max(0.0, deadline - time.monotonic())
        done, _pending = await asyncio.wait({detached_receive}, timeout=remaining)
        if detached_receive not in done:
            return False, None
    if session.detached_upstream_receive is detached_receive:
        session.detached_upstream_receive = None
    try:
        return True, detached_receive.result()
    except asyncio.CancelledError:
        return True, None
    except Exception:
        logger.debug("Detached HTTP bridge receive failed during close observation", exc_info=True)
        return True, None


async def _await_http_bridge_receive_owners(
    session: _HTTPBridgeSession,
) -> UpstreamWebSocketMessage | None:
    upstream_reader = session.upstream_reader
    if upstream_reader is not None:
        try:
            await asyncio.shield(upstream_reader)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if not upstream_reader.done() or (current_task is not None and current_task.cancelling()):
                raise
        except Exception:
            logger.debug("HTTP bridge reader failed during late reconciliation", exc_info=True)

    detached_receive = session.detached_upstream_receive
    if detached_receive is None:
        return None
    try:
        detached_message = await asyncio.shield(detached_receive)
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if not detached_receive.done() or (current_task is not None and current_task.cancelling()):
            raise
        return None
    except Exception:
        logger.debug("Detached HTTP bridge receive failed during late reconciliation", exc_info=True)
        return None
    finally:
        if detached_receive.done() and session.detached_upstream_receive is detached_receive:
            session.detached_upstream_receive = None
    return detached_message


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
    _proxy_cleanup_tasks: set[asyncio.Task[None]]
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

    async def _settle_or_release_failed_websocket_reservation(
        self,
        *,
        request_state: _WebSocketRequestState,
        reservation: ApiKeyUsageReservationData | None,
        api_key: ApiKeyData | None,
        error_code: str,
        error_message: str,
    ) -> None: ...

    async def _claim_http_bridge_discarded_accounting(
        self,
        session: _HTTPBridgeSession,
        *,
        owner_already_acquired: bool = False,
    ) -> tuple[_DiscardedRequestAccounting, ...]: ...

    async def _handoff_http_bridge_discarded_accounting_reconciliation(
        self,
        session: _HTTPBridgeSession,
    ) -> None: ...

    async def _process_http_bridge_upstream_text(
        self,
        session: _HTTPBridgeSession,
        text: str,
    ) -> None: ...

    async def _close_http_bridge_session(
        self,
        session: _HTTPBridgeSession,
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
        lifecycle_lock_held: bool = False,
        error_code: str = "stream_incomplete",
        error_message: str = "HTTP bridge session closed before response.completed",
    ) -> None: ...

    async def _transfer_submitted_http_bridge_pending_accounting(
        self,
        session: _HTTPBridgeSession,
    ) -> None: ...

    async def _seal_http_bridge_session_for_close(
        self,
        session: _HTTPBridgeSession,
        *,
        lifecycle_lock_held: bool = False,
    ) -> None: ...

    async def _detach_http_bridge_session_for_background_close(
        self,
        session: _HTTPBridgeSession,
    ) -> None: ...

    async def _unregister_http_bridge_turn_states(self, session: _HTTPBridgeSession) -> None: ...

    async def _unregister_http_bridge_previous_response_ids(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_turn_states_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _unregister_http_bridge_previous_response_ids_locked(self, session: _HTTPBridgeSession) -> None: ...

    def _detach_http_bridge_session_indexes_locked(self, session: _HTTPBridgeSession) -> bool: ...

    def _mark_stale_durable_http_bridge_session_locked(
        self,
        session: _HTTPBridgeSession,
    ) -> bool: ...

    async def _settle_durable_http_bridge_session_refresh(self, session: _HTTPBridgeSession) -> None: ...

    async def _release_durable_http_bridge_session_ownership(self, session: _HTTPBridgeSession) -> None: ...

    def _defer_next_durable_http_bridge_session_refresh(
        self,
        session: _HTTPBridgeSession,
        *,
        now: float | None = None,
    ) -> None: ...

    async def _refresh_durable_http_bridge_session(self, session: _HTTPBridgeSession) -> None: ...

    async def _fence_durable_http_bridge_session_before_submit(
        self,
        session: _HTTPBridgeSession,
    ) -> None: ...

    async def _retire_http_bridge_session_after_durable_ownership_loss(
        self,
        session: _HTTPBridgeSession,
        *,
        reason: str,
        error_message: str,
    ) -> None: ...

    def _schedule_http_bridge_session_close(
        self,
        session: _HTTPBridgeSession,
        *,
        reason: str,
        error_code: str | None = None,
        error_message: str | None = None,
        skip_reader_task: bool = False,
    ) -> asyncio.Future[None]: ...


class _HTTPBridgeLifecycleMixin:
    async def close_all_http_bridge_sessions(self: _HTTPBridgeLifecycleService) -> None:
        async with self._http_bridge_lock:
            sessions_to_close = list(self._http_bridge_sessions.values())
            inflight_futures = list(self._http_bridge_inflight_sessions.values())
            for session in sessions_to_close:
                self._detach_http_bridge_session_indexes_locked(session)
            self._http_bridge_inflight_sessions.clear()
            self._http_bridge_turn_state_index.clear()
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
            session.closed = True
            session.pending_changed.set()
            self._schedule_http_bridge_session_close(
                session,
                reason="shutdown",
                error_code="upstream_unavailable",
                error_message="HTTP responses session bridge is shutting down",
            )
        await _close_tracked_background_tasks(
            self._http_bridge_background_close_tasks,
            label="HTTP bridge background close tasks",
            timeout_seconds=_HTTP_BRIDGE_SHUTDOWN_CLOSE_TIMEOUT_SECONDS,
        )

    async def mark_http_bridge_draining(self: _HTTPBridgeLifecycleService) -> None:
        try:
            await _await_operation_before_hard_timeout(
                self._durable_bridge.mark_instance_draining(
                    instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                ),
                timeout_seconds=_HTTP_BRIDGE_SHUTDOWN_MARK_DRAINING_TIMEOUT_SECONDS,
                tasks=self._proxy_cleanup_tasks,
                label="durable HTTP bridge shutdown draining mark",
            )
        except TimeoutError:
            logger.error("Hard timeout marking durable HTTP bridge sessions draining; continuing shutdown")
        except Exception:
            logger.warning("Failed to mark durable HTTP bridge sessions draining", exc_info=True)

    async def _prune_http_bridge_sessions_locked(
        self: _HTTPBridgeLifecycleService,
    ) -> list[_HTTPBridgeSession]:
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
        stale_sessions: list[_HTTPBridgeSession] = []
        for key in stale_keys:
            session = self._http_bridge_sessions.get(key)
            if session is not None:
                self._detach_http_bridge_session_indexes_locked(session)
                _log_http_bridge_event(
                    "evict_idle",
                    key,
                    account_id=session.account.id,
                    model=session.request_model,
                    cache_key_family=key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                )
                stale_sessions.append(session)
        return stale_sessions

    async def _detach_http_bridge_session_for_background_close(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
    ) -> None:
        await self._seal_http_bridge_session_for_close(session)
        session_lease = session.account_model_session_lease
        if session_lease is not None:
            session.account_model_session_lease = None
            session_lease.release()
        await self._unregister_http_bridge_turn_states(session)
        await self._unregister_http_bridge_previous_response_ids(session)
        await self._release_durable_http_bridge_session_ownership(session)

    async def _release_durable_http_bridge_session_ownership(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
    ) -> None:
        session_id = session.durable_session_id
        owner_epoch = session.durable_owner_epoch
        if session_id is None or owner_epoch is None:
            return
        await self._settle_durable_http_bridge_session_refresh(session)
        try:
            await self._durable_bridge.release_live_session(
                session_id=session_id,
                instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                owner_epoch=owner_epoch,
                draining=shutdown_state.is_bridge_drain_active(),
            )
        except Exception:
            logger.warning("Failed to release durable HTTP bridge session", exc_info=True)
            return
        session.durable_session_id = None
        session.durable_owner_epoch = None
        session.durable_lease_expires_at = None

    def _schedule_http_bridge_session_close(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        reason: str,
        error_code: str | None = None,
        error_message: str | None = None,
        skip_reader_task: bool = False,
    ) -> asyncio.Future[None]:
        detached = asyncio.get_running_loop().create_future()

        async def close_session() -> None:
            started_at = time.monotonic()
            try:
                try:
                    if error_code is not None and error_message is not None:
                        await self._seal_http_bridge_session_for_close(session)
                        await self._fail_pending_websocket_requests(
                            account_id_value=session.account.id,
                            pending_requests=session.pending_requests,
                            pending_lock=session.pending_lock,
                            error_code=error_code,
                            error_message=error_message,
                            api_key=None,
                            response_create_gate=session.response_create_gate,
                        )
                    await self._detach_http_bridge_session_for_background_close(session)
                finally:
                    # Durable detachment can cooperatively accept shutdown
                    # cancellation. Socket/reader cleanup must still run under
                    # this already-enrolled close owner before cancellation is
                    # allowed to finish the task.
                    if not detached.done():
                        detached.set_result(None)
                    if skip_reader_task:
                        await self._close_http_bridge_session(session, skip_reader_task=True)
                    else:
                        await self._close_http_bridge_session(session)
            except Exception:
                if not detached.done():
                    detached.set_result(None)
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
        return detached

    async def _claim_http_bridge_discarded_accounting(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
        *,
        owner_already_acquired: bool = False,
    ) -> tuple[_DiscardedRequestAccounting, ...]:
        async with session.pending_lock:
            if session.discarded_accounting_reconciliation_owned and not owner_already_acquired:
                return ()
            session.discarded_accounting_reconciliation_owned = True
            session.discarded_response_ids.clear()
            discarded_accounting = tuple(session.discarded_request_accounting.values()) + tuple(
                session.anonymous_discarded_request_accounting.values()
            )
            session.discarded_request_accounting.clear()
            session.anonymous_discarded_request_accounting.clear()
            session.pending_changed.set()
            return discarded_accounting

    async def _handoff_http_bridge_discarded_accounting_reconciliation(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        owns_upstream_close = False

        def spawn_accounting_guardian(
            accounting: _DiscardedRequestAccounting,
        ) -> None:
            resolution_future = accounting.resolution_future
            if resolution_future is None:
                raise RuntimeError("Discarded accounting guardian requires a resolution future")
            started = False

            async def run_accounting_guardian() -> None:
                nonlocal started
                started = True
                while True:
                    try:
                        resolution = await asyncio.shield(resolution_future)
                        break
                    except asyncio.CancelledError:
                        current_task = asyncio.current_task()
                        if current_task is not None:
                            current_task.uncancel()
                        logger.warning(
                            "HTTP bridge discarded accounting guardian resolving fallback after cancellation "
                            "request_id=%s",
                            accounting.request_state.request_id,
                        )
                        if not resolution_future.done():
                            resolution_future.set_result("fallback")
                operation = (
                    self._settle_or_release_failed_websocket_reservation(
                        request_state=accounting.request_state,
                        reservation=accounting.api_key_reservation,
                        api_key=accounting.request_state.api_key or session.api_key,
                        error_code="stream_incomplete",
                        error_message="HTTP bridge session closed before discarded response completed",
                    )
                    if resolution == "fallback"
                    else resolution()
                )
                try:
                    await _await_shielded_cleanup(
                        operation,
                        label=(
                            "HTTP bridge discarded accounting guardian "
                            f"request_id={accounting.request_state.request_id}"
                        ),
                    )
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        current_task.uncancel()

            guardian_task = asyncio.create_task(
                run_accounting_guardian(),
                name=(
                    "http-bridge-discarded-accounting-guardian-"
                    f"{accounting.request_state.request_id}"
                ),
            )
            _track_existing_background_task(
                self._proxy_cleanup_tasks,
                guardian_task,
                label=(
                    "HTTP bridge discarded accounting guardian "
                    f"request_id={accounting.request_state.request_id}"
                ),
            )

            def recover_prestart_cancellation(completed: asyncio.Task[None]) -> None:
                if not completed.cancelled() or started:
                    return
                logger.warning(
                    "HTTP bridge discarded accounting guardian cancelled before start; "
                    "enrolling successor request_id=%s",
                    accounting.request_state.request_id,
                )
                spawn_accounting_guardian(accounting)

            guardian_task.add_done_callback(recover_prestart_cancellation)

        async with session.pending_lock:
            if session.discarded_accounting_reconciliation_owned:
                return
            session.discarded_accounting_reconciliation_owned = True
            if (
                not session.discarded_request_accounting
                and not session.anonymous_discarded_request_accounting
            ):
                session.discarded_response_ids.clear()
                return
            if not session.upstream_close_owned:
                session.upstream_close_owned = True
                owns_upstream_close = True
            enrolled_accounting_ids: set[int] = set()
            for accounting in (
                *session.discarded_request_accounting.values(),
                *session.anonymous_discarded_request_accounting.values(),
            ):
                accounting_id = id(accounting)
                if accounting_id in enrolled_accounting_ids:
                    continue
                enrolled_accounting_ids.add(accounting_id)
                if accounting.resolution_future is not None:
                    continue
                accounting.resolution_future = asyncio.get_running_loop().create_future()
                spawn_accounting_guardian(accounting)

        async def reconcile_late_receive() -> None:
            detached_message: UpstreamWebSocketMessage | None = None
            while True:
                try:
                    if detached_message is None:
                        detached_message = await _await_http_bridge_receive_owners(session)
                    if (
                        detached_message is not None
                        and detached_message.kind == "text"
                        and detached_message.text is not None
                    ):
                        await self._process_http_bridge_upstream_text(
                            session,
                            getattr(detached_message, "raw_text", None) or detached_message.text,
                        )
                    break
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        current_task.uncancel()
                    logger.warning(
                        "HTTP bridge discarded reconciliation retained after cancellation "
                        "account_id=%s model=%s",
                        session.account.id,
                        session.request_model,
                    )

            async def claim_and_resolve_discarded_accounting() -> None:
                discarded_accounting = await self._claim_http_bridge_discarded_accounting(
                    session,
                    owner_already_acquired=True,
                )
                for accounting in discarded_accounting:
                    resolution_future = accounting.resolution_future
                    if resolution_future is None:
                        logger.error(
                            "Discarded accounting missing pre-enrolled guardian; reservation retained "
                            "request_id=%s",
                            accounting.request_state.request_id,
                        )
                        continue
                    if not resolution_future.done():
                        resolution_future.set_result("fallback")

            try:
                await _await_shielded_cleanup(
                    claim_and_resolve_discarded_accounting(),
                    label="HTTP bridge discarded accounting resolution",
                )
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None:
                    current_task.uncancel()
            if owns_upstream_close:
                try:
                    await _await_operation_before_hard_timeout(
                        session.upstream.close(),
                        timeout_seconds=_HTTP_BRIDGE_RECONCILER_UPSTREAM_CLOSE_TIMEOUT_SECONDS,
                        tasks=self._proxy_cleanup_tasks,
                        label="HTTP bridge reconciler upstream close",
                    )
                except TimeoutError:
                    logger.warning("HTTP bridge reconciler upstream close exceeded hard observation")
                except Exception:
                    logger.debug("Failed to close reconciled HTTP bridge upstream", exc_info=True)

        def spawn_reconciliation_owner() -> None:
            started = False

            async def run_reconciliation_owner() -> None:
                nonlocal started
                started = True
                await reconcile_late_receive()

            reconciliation_task = asyncio.create_task(
                run_reconciliation_owner(),
                name=f"http-bridge-discarded-reconcile-{session.key.affinity_kind}",
            )
            _track_existing_background_task(
                self._proxy_cleanup_tasks,
                reconciliation_task,
                label=(
                    "HTTP bridge discarded reconciliation "
                    f"affinity_kind={session.key.affinity_kind}"
                ),
            )

            def recover_prestart_cancellation(completed: asyncio.Task[None]) -> None:
                if not completed.cancelled() or started:
                    return
                logger.warning(
                    "HTTP bridge discarded reconciliation cancelled before start; "
                    "enrolling successor account_id=%s model=%s",
                    session.account.id,
                    session.request_model,
                )
                spawn_reconciliation_owner()

            reconciliation_task.add_done_callback(recover_prestart_cancellation)

        spawn_reconciliation_owner()

    async def _transfer_submitted_http_bridge_pending_accounting(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> None:
        async with session.pending_lock:
            for request_state in session.pending_requests:
                if request_state.http_bridge_send_started_at is None:
                    continue
                resolution_future = request_state.discarded_accounting_resolution_future
                request_state.discarded_accounting_resolution_future = None
                discarded_accounting = _DiscardedRequestAccounting(
                    request_state=request_state,
                    api_key_reservation=request_state.api_key_reservation,
                    resolution_future=resolution_future,
                )
                request_state.api_key_reservation = None
                if request_state.response_id is not None:
                    session.discarded_response_ids.add(request_state.response_id)
                    session.discarded_request_accounting.setdefault(
                        request_state.response_id,
                        discarded_accounting,
                    )
                else:
                    session.anonymous_discarded_request_accounting.setdefault(
                        request_state.request_id,
                        discarded_accounting,
                    )
            session.queued_request_count = 0
            session.pending_changed.set()

    async def _seal_http_bridge_session_for_close(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
        *,
        lifecycle_lock_held: bool = False,
    ) -> None:
        if not lifecycle_lock_held:
            await session.lifecycle_lock.acquire()
        try:
            session.closed = True
            session.pending_changed.set()
            await self._transfer_submitted_http_bridge_pending_accounting(session)
        finally:
            if not lifecycle_lock_held:
                session.lifecycle_lock.release()

    async def _close_http_bridge_session(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        turn_state_lock_held: bool = False,
        skip_reader_task: bool = False,
        lifecycle_lock_held: bool = False,
        error_code: str = "stream_incomplete",
        error_message: str = "HTTP bridge session closed before response.completed",
    ) -> None:
        caller_cancellation: asyncio.CancelledError | None = None
        await self._seal_http_bridge_session_for_close(
            session,
            lifecycle_lock_held=lifecycle_lock_held,
        )
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
        pending_requests = getattr(session, "pending_requests", None)
        pending_lock = getattr(session, "pending_lock", None)
        response_create_gate = getattr(session, "response_create_gate", None)

        # Preserve discarded-request ownership until every receive owner has
        # stopped. A cancellation-suppressing receive may already hold a
        # terminal event carrying authoritative usage, and must get the first
        # opportunity to reconcile it before close falls back to failure
        # settlement.
        upstream_reader = session.upstream_reader
        if upstream_reader is not None and not skip_reader_task:
            upstream_reader.cancel()
        receive_quiesced = False
        detached_message: UpstreamWebSocketMessage | None = None
        try:
            receive_quiesced, detached_message = await _observe_http_bridge_receive_owners(
                session,
                timeout_seconds=_HTTP_BRIDGE_CLOSE_RECEIVE_OBSERVATION_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError as cancellation:
            caller_cancellation = cancellation
        if detached_message is not None and detached_message.kind == "text" and detached_message.text is not None:
            await self._process_http_bridge_upstream_text(
                session,
                getattr(detached_message, "raw_text", None) or detached_message.text,
            )

        if pending_requests is not None and pending_lock is not None:
            await self._fail_pending_websocket_requests(
                account_id_value=session.account.id,
                pending_requests=pending_requests,
                pending_lock=pending_lock,
                error_code=error_code,
                error_message=error_message,
                api_key=None,
                response_create_gate=response_create_gate,
            )
        if receive_quiesced:
            discarded_accounting = await self._claim_http_bridge_discarded_accounting(session)
            for accounting in discarded_accounting:
                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    self._settle_or_release_failed_websocket_reservation(
                        request_state=accounting.request_state,
                        reservation=accounting.api_key_reservation,
                        api_key=accounting.request_state.api_key or session.api_key,
                        error_code="stream_incomplete",
                        error_message="HTTP bridge session closed before discarded response completed",
                    ),
                    name=f"http-bridge-discarded-settlement-{accounting.request_state.request_id}",
                    label=(
                        "HTTP bridge discarded settlement "
                        f"request_id={accounting.request_state.request_id}"
                    ),
                )
        else:
            await self._handoff_http_bridge_discarded_accounting_reconciliation(session)
        # All database-backed settlement is enrolled before the durable/socket
        # cleanup barrier, or its single late-reconciliation owner is already
        # tracked by the same cleanup registry.
        await self._release_durable_http_bridge_session_ownership(session)
        if not session.upstream_close_owned:
            try:
                await session.upstream.close()
            except Exception:
                logger.debug("Failed to close HTTP bridge upstream websocket", exc_info=True)
        _log_http_bridge_event(
            "close",
            session.key,
            account_id=session.account.id,
            model=session.request_model,
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )
        if caller_cancellation is not None:
            raise caller_cancellation

    async def _evict_http_bridge_session_after_upstream_disconnect(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        error_message: str,
    ) -> None:
        async with self._http_bridge_lock:
            self._detach_http_bridge_session_indexes_locked(session)
            self._schedule_http_bridge_session_close(
                session,
                reason="upstream-disconnect",
                error_code="stream_incomplete",
                error_message=error_message,
                skip_reader_task=True,
            )
        _log_http_bridge_event(
            "evict_upstream_disconnected",
            session.key,
            account_id=session.account.id,
            model=session.request_model,
            detail=error_message,
            cache_key_family=session.key.affinity_kind,
            model_class=_extract_model_class(session.request_model) if session.request_model else None,
        )

    async def _register_http_bridge_turn_state(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
        turn_state: str,
    ) -> None:
        async with self._http_bridge_lock:
            if session.closed:
                return
            current = self._http_bridge_sessions.get(session.key)
            if current is not session and (
                current is not None or session.key not in self._http_bridge_inflight_sessions
            ):
                return
            session.downstream_turn_state_aliases.add(turn_state)
            if session.downstream_turn_state is None:
                session.downstream_turn_state = turn_state
            for alias in session.downstream_turn_state_aliases:
                self._http_bridge_turn_state_index[_http_bridge_turn_state_alias_key(alias, session.key.api_key_id)] = (
                    session.key
                )
        session_id = session.durable_session_id
        owner_epoch = session.durable_owner_epoch
        if session_id is not None and owner_epoch is not None:

            async def persist_turn_state() -> None:
                await self._durable_bridge.register_turn_state(
                    session_id=session_id,
                    api_key_id=session.key.api_key_id,
                    instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                    owner_epoch=owner_epoch,
                    turn_state=turn_state,
                    lease_ttl_seconds=_http_bridge_durable_lease_ttl_seconds(),
                )
                self._defer_next_durable_http_bridge_session_refresh(session)

            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                persist_turn_state(),
                name=f"bridge-turn-state-{session_id}",
                label=f"durable HTTP bridge turn-state alias session_id={session_id}",
            )

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
            current = self._http_bridge_sessions.get(session.key)
            if current is not session and (
                current is not None or session.key not in self._http_bridge_inflight_sessions
            ):
                return
            alias_key = _http_bridge_previous_response_alias_key(stripped_response_id, session.key.api_key_id)
            self._http_bridge_previous_response_index[alias_key] = session.key
            session.previous_response_ids.add(stripped_response_id)
        session_id = session.durable_session_id
        owner_epoch = session.durable_owner_epoch
        if session_id is not None and owner_epoch is not None:

            async def persist_previous_response_id() -> None:
                await self._durable_bridge.register_previous_response_id(
                    session_id=session_id,
                    api_key_id=session.key.api_key_id,
                    instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                    owner_epoch=owner_epoch,
                    response_id=stripped_response_id,
                    lease_ttl_seconds=_http_bridge_durable_lease_ttl_seconds(),
                    input_item_count=input_item_count,
                    input_full_fingerprint=input_full_fingerprint,
                )
                self._defer_next_durable_http_bridge_session_refresh(session)

            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                persist_previous_response_id(),
                name=f"bridge-previous-response-{session_id}",
                label=f"durable HTTP bridge previous_response_id alias session_id={session_id}",
            )

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
        current = self._http_bridge_sessions.get(session.key)
        if current is not None and current is not session:
            session.downstream_turn_state_aliases.clear()
            return
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
        current = self._http_bridge_sessions.get(session.key)
        if current is not None and current is not session:
            session.previous_response_ids.clear()
            return
        response_ids = tuple(session.previous_response_ids)
        for response_id in response_ids:
            self._http_bridge_previous_response_index.pop(
                _http_bridge_previous_response_alias_key(response_id, session.key.api_key_id),
                None,
            )
        session.previous_response_ids.clear()

    def _detach_http_bridge_session_indexes_locked(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> bool:
        if self._http_bridge_sessions.get(session.key) is not session:
            session.closed = True
            session.pending_changed.set()
            return False
        self._http_bridge_sessions.pop(session.key, None)
        session.closed = True
        session.pending_changed.set()
        self._unregister_http_bridge_turn_states_locked(session)
        self._unregister_http_bridge_previous_response_ids_locked(session)
        return True

    def _mark_stale_durable_http_bridge_session_locked(
        self: _HTTPBridgeLifecycleService,
        session: _HTTPBridgeSession,
    ) -> bool:
        session.durable_ownership_lost = True
        session.durable_lease_expires_at = None
        session.durable_ownership_retirement_scheduled = True
        return self._detach_http_bridge_session_indexes_locked(session)

    def _promote_http_bridge_session_to_codex_affinity(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        turn_state: str,
        settings: Settings,
    ) -> None:
        session.affinity = _merge_affinity_required_wire_api(
            _AffinityPolicy(key=turn_state, kind=StickySessionKind.CODEX_SESSION),
            session.affinity,
        )
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
            session.durable_lease_expires_at = lookup.lease_expires_at
            session.durable_ownership_lost = False
            session.durable_ownership_retirement_scheduled = False
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
                    instance_id=current_instance,
                    owner_epoch=lookup.owner_epoch,
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
        async with session.durable_renew_lock:
            session_id = session.durable_session_id
            owner_epoch = session.durable_owner_epoch
            if session_id is None or owner_epoch is None:
                return
            try:
                lookup = await self._durable_bridge.renew_live_session(
                    session_id=session_id,
                    api_key_id=session.key.api_key_id,
                    instance_id=self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id,
                    owner_epoch=owner_epoch,
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
                if lookup is None:
                    session.durable_ownership_lost = True
                    logger.warning(
                        "Durable HTTP bridge lease renewal lost ownership session_id=%s owner_epoch=%s",
                        session_id,
                        owner_epoch,
                    )
                    await self._retire_http_bridge_session_after_durable_ownership_loss(
                        session,
                        reason="durable-refresh-lost",
                        error_message="HTTP bridge durable ownership was lost during renewal",
                    )
                    return
                if session.durable_session_id == session_id and session.durable_owner_epoch == owner_epoch:
                    session.durable_lease_expires_at = lookup.lease_expires_at
                    session.durable_ownership_lost = False
            except Exception:
                logger.warning("Failed to renew durable HTTP bridge session lease", exc_info=True)

    async def _retire_http_bridge_session_after_durable_ownership_loss(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
        *,
        reason: str,
        error_message: str,
    ) -> None:
        async with self._http_bridge_lock:
            self._detach_http_bridge_session_indexes_locked(session)
            if session.durable_ownership_retirement_scheduled:
                return
            session.durable_ownership_retirement_scheduled = True
            self._schedule_http_bridge_session_close(
                session,
                reason=reason,
                error_code="stream_incomplete",
                error_message=error_message,
            )

    async def _fence_durable_http_bridge_session_before_submit(
        self: _HTTPBridgeLifecycleService,
        session: "_HTTPBridgeSession",
    ) -> None:
        async with session.durable_renew_lock:
            if session.durable_ownership_lost:
                await self._retire_http_bridge_session_after_durable_ownership_loss(
                    session,
                    reason="durable-submit-fence-lost",
                    error_message="HTTP bridge durable ownership was lost before submission",
                )
                raise ProxyResponseError(
                    409,
                    openai_error(
                        "bridge_instance_mismatch",
                        "HTTP bridge session ownership changed before submission; retry the request",
                        error_type="server_error",
                    ),
                )
            session_id = session.durable_session_id
            owner_epoch = session.durable_owner_epoch
            if session_id is None or owner_epoch is None:
                return
            lease_expires_at = session.durable_lease_expires_at
            if lease_expires_at is not None and to_utc_naive(lease_expires_at) > utcnow():
                return

            current_instance = self._http_bridge_runtime_settings().http_responses_session_bridge_instance_id
            try:
                lookup = await self._durable_bridge.renew_live_session(
                    session_id=session_id,
                    api_key_id=session.key.api_key_id,
                    instance_id=current_instance,
                    owner_epoch=owner_epoch,
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                session.durable_ownership_lost = True
                await self._retire_http_bridge_session_after_durable_ownership_loss(
                    session,
                    reason="durable-submit-fence-unavailable",
                    error_message="HTTP bridge durable ownership could not be verified",
                )
                raise ProxyResponseError(
                    503,
                    openai_error(
                        "upstream_unavailable",
                        "HTTP bridge durable ownership could not be verified",
                        error_type="server_error",
                    ),
                ) from exc

            ownership_matches = (
                lookup is not None
                and lookup.owner_instance_id == current_instance
                and lookup.owner_epoch == owner_epoch
                and lookup.lease_expires_at is not None
                and to_utc_naive(lookup.lease_expires_at) > utcnow()
            )
            if not ownership_matches:
                session.durable_ownership_lost = True
                _log_http_bridge_event(
                    "owner_mismatch_retry",
                    session.key,
                    account_id=session.account.id,
                    model=session.request_model,
                    detail=(f"current_instance={current_instance}, owner_epoch={owner_epoch}, outcome=submit_fenced"),
                    cache_key_family=session.key.affinity_kind,
                    model_class=_extract_model_class(session.request_model) if session.request_model else None,
                    owner_check_applied=True,
                )
                if PROMETHEUS_AVAILABLE and bridge_instance_mismatch_total is not None:
                    bridge_instance_mismatch_total.labels(outcome="fail_closed").inc()
                await self._retire_http_bridge_session_after_durable_ownership_loss(
                    session,
                    reason="durable-submit-fence-lost",
                    error_message="HTTP bridge durable ownership was lost before submission",
                )
                raise ProxyResponseError(
                    409,
                    openai_error(
                        "bridge_instance_mismatch",
                        "HTTP bridge session ownership changed before submission; retry the request",
                        error_type="server_error",
                    ),
                )

            assert lookup is not None
            session.durable_lease_expires_at = lookup.lease_expires_at
            session.durable_ownership_lost = False
            self._defer_next_durable_http_bridge_session_refresh(session)

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
        _track_existing_background_task(
            self._proxy_cleanup_tasks,
            task,
            label=f"durable HTTP bridge lease refresh session_id={session.durable_session_id}",
        )

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
