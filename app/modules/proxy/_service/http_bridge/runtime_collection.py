from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

import anyio

from app.core.config.settings import Settings
from app.core.egress import get_upstream_egress_runtime
from app.db.models import AccountStatus, DashboardSettings
from app.modules.proxy._service.http_bridge.keys import (
    _http_bridge_busy_parallel_index,
    _http_bridge_family_affinity_key,
    _http_bridge_soft_shard_index,
)
from app.modules.proxy._service.http_bridge.policy import (
    _account_status_value,
    _http_bridge_account_label,
)
from app.modules.proxy._service.http_bridge.runtime import (
    HTTPBridgeAccountRuntimeSnapshot,
    HTTPBridgeRequestStatusSnapshot,
    HTTPBridgeRuntimeHealthHistoryBucket,
    HTTPBridgeRuntimeHealthSnapshot,
    HTTPBridgeRuntimeSessionSnapshot,
    HTTPBridgeRuntimeSnapshot,
    UpstreamEgressRuntimeSnapshot,
    _build_http_bridge_runtime_snapshot,
    _http_bridge_account_model_capacity_snapshot,
    _http_bridge_runtime_config,
    _http_bridge_runtime_health_status,
    _HTTPBridgeAccountModelCapacitySnapshot,
    _HTTPBridgeRuntimeSessionObservation,
    _summarize_http_bridge_runtime_sessions,
)
from app.modules.proxy._service.observability import _hash_identifier, _monotonic_age_ms
from app.modules.proxy._service.support import _HTTPBridgeSession, _HTTPBridgeSessionKey
from app.modules.proxy.repo_bundle import ProxyRepoFactory
from app.modules.request_logs.repository import RequestLatencyHealthSnapshot

logger = logging.getLogger("app.modules.proxy.service")


def _upstream_egress_runtime_snapshot(
    *,
    now: float,
    settings: Settings,
) -> UpstreamEgressRuntimeSnapshot:
    runtime = get_upstream_egress_runtime()
    if runtime is None:
        return UpstreamEgressRuntimeSnapshot(
            mode=settings.upstream_egress_mode,
            selected_route="proxy" if settings.upstream_egress_mode == "proxy" else "direct",
            proxy_configured=settings.upstream_proxy_url is not None,
            direct_ok=None,
            proxy_ok=None,
            direct_latency_ms=None,
            proxy_latency_ms=None,
            direct_failure_streak=0,
            direct_success_streak=0,
            last_probe_ago_ms=None,
            last_switch_ago_ms=None,
        )
    snapshot = runtime.snapshot()
    return UpstreamEgressRuntimeSnapshot(
        mode=snapshot.mode,
        selected_route=snapshot.selected_route,
        proxy_configured=snapshot.proxy_configured,
        direct_ok=snapshot.direct_ok,
        proxy_ok=snapshot.proxy_ok,
        direct_latency_ms=snapshot.direct_latency_ms,
        proxy_latency_ms=snapshot.proxy_latency_ms,
        direct_failure_streak=snapshot.direct_failure_streak,
        direct_success_streak=snapshot.direct_success_streak,
        last_probe_ago_ms=_monotonic_age_ms(now, snapshot.last_probe_at),
        last_switch_ago_ms=_monotonic_age_ms(now, snapshot.last_switch_at),
    )


class _HTTPBridgeRuntimeCollectionService(Protocol):
    _repo_factory: ProxyRepoFactory
    _http_bridge_lock: anyio.Lock
    _http_bridge_sessions: dict[_HTTPBridgeSessionKey, _HTTPBridgeSession]
    _http_bridge_inflight_sessions: dict[_HTTPBridgeSessionKey, asyncio.Future[_HTTPBridgeSession]]

    def _proxy_runtime_settings(self) -> Settings: ...

    async def _proxy_dashboard_settings(self) -> DashboardSettings: ...

    async def _http_bridge_runtime_health_snapshot(
        self,
        *,
        snapshot_started_at: float,
    ) -> HTTPBridgeRuntimeHealthSnapshot: ...

    async def _http_bridge_account_model_capacity_snapshot(
        self,
        *,
        active_session_counts_by_account: dict[str, int],
        idle_session_counts_by_account: dict[str, int],
        account_model_session_limit: int,
    ) -> _HTTPBridgeAccountModelCapacitySnapshot: ...


class _HTTPBridgeRuntimeCollectionMixin:
    async def get_http_bridge_request_status(
        self: _HTTPBridgeRuntimeCollectionService,
        request_id: str,
    ) -> HTTPBridgeRequestStatusSnapshot | None:
        target = (request_id or "").strip()
        if not target:
            return None

        runtime_settings = self._proxy_runtime_settings()
        now = time.monotonic()
        stall_threshold_ms = int(max(30_000.0, min(runtime_settings.stream_idle_timeout_seconds * 500.0, 120_000.0)))
        best_match: tuple[int, HTTPBridgeRequestStatusSnapshot] | None = None

        async with self._http_bridge_lock:
            sessions = list(self._http_bridge_sessions.values())

        for session in sessions:
            async with session.pending_lock:
                pending_requests = list(session.pending_requests)

            for request_state in pending_requests:
                match_kind = None
                priority = 0
                if request_state.request_id == target:
                    match_kind = "request_id"
                    priority = 100
                elif request_state.response_id == target:
                    match_kind = "response_id"
                    priority = 95
                elif request_state.previous_response_id == target:
                    match_kind = "previous_response_id"
                    priority = 80
                if match_kind is None:
                    continue

                observed_state = "active_streaming"
                state_detail = "HTTP bridge session is still tracking this live request."
                request_age_ms = _monotonic_age_ms(now, request_state.started_at)
                if session.upstream_control.reconnect_requested:
                    observed_state = "active_reconnecting"
                    state_detail = "Upstream reconnect has been requested for this live session."
                elif request_state.awaiting_response_created:
                    observed_state = "active_waiting_response_created"
                    state_detail = "Request is queued and still waiting for upstream response.created."
                    if request_age_ms is not None and request_age_ms >= stall_threshold_ms:
                        state_detail = (
                            "Request is still waiting for upstream response.created and has been quiet for a while."
                        )

                candidate = HTTPBridgeRequestStatusSnapshot(
                    request_id=target,
                    observed_state=observed_state,
                    state_detail=state_detail,
                    live=True,
                    matched_by=match_kind,
                    account_id=session.account.id,
                    request_model=session.request_model,
                    session_affinity_kind=session.key.affinity_kind,
                    session_affinity_key_hash=(
                        _hash_identifier(session.key.affinity_key) if session.key.affinity_key else None
                    ),
                    session_api_key_id=session.key.api_key_id,
                    session_codex=session.codex_session,
                    session_closed=session.closed,
                    reconnect_requested=session.upstream_control.reconnect_requested,
                    queued_request_count=session.queued_request_count,
                    pending_request_count=len(pending_requests),
                    last_used_ago_ms=_monotonic_age_ms(now, session.last_used_at),
                    last_upstream_event_ago_ms=None,
                    last_downstream_emit_ago_ms=None,
                    upstream_turn_state=session.upstream_turn_state,
                    downstream_turn_state=session.downstream_turn_state,
                    matched_request_id=request_state.request_id,
                    matched_response_id=request_state.response_id,
                    matched_previous_response_id=request_state.previous_response_id,
                    awaiting_response_created=request_state.awaiting_response_created,
                    replay_count=request_state.replay_count,
                    downstream_connected=None,
                    request_age_ms=request_age_ms,
                    request_last_upstream_event_ago_ms=None,
                    request_last_downstream_emit_ago_ms=None,
                )
                if best_match is None or priority > best_match[0]:
                    best_match = (priority, candidate)

        return best_match[1] if best_match is not None else None

    async def get_http_bridge_account_runtime_snapshot(
        self: _HTTPBridgeRuntimeCollectionService,
    ) -> dict[str, HTTPBridgeAccountRuntimeSnapshot]:
        async with self._http_bridge_lock:
            session_items = list(self._http_bridge_sessions.values())

        by_account: dict[str, HTTPBridgeAccountRuntimeSnapshot] = {}
        for session in session_items:
            if session.closed or not session.account.id:
                continue
            async with session.pending_lock:
                pending_count = len(session.pending_requests)
                queued_count = session.queued_request_count
            account_id = session.account.id
            current = by_account.get(account_id)
            if current is None:
                by_account[account_id] = HTTPBridgeAccountRuntimeSnapshot(
                    account_id=account_id,
                    sessions=1,
                    pending_requests=pending_count,
                    queued_requests=queued_count,
                    busy_sessions=1 if pending_count > 0 else 0,
                    codex_sessions=1 if session.codex_session else 0,
                    reconnect_requested_sessions=1 if session.upstream_control.reconnect_requested else 0,
                )
                continue
            by_account[account_id] = HTTPBridgeAccountRuntimeSnapshot(
                account_id=account_id,
                sessions=current.sessions + 1,
                pending_requests=current.pending_requests + pending_count,
                queued_requests=current.queued_requests + queued_count,
                busy_sessions=current.busy_sessions + (1 if pending_count > 0 else 0),
                codex_sessions=current.codex_sessions + (1 if session.codex_session else 0),
                reconnect_requested_sessions=current.reconnect_requested_sessions
                + (1 if session.upstream_control.reconnect_requested else 0),
            )
        return by_account

    async def get_http_bridge_runtime_snapshot(
        self: _HTTPBridgeRuntimeCollectionService,
        *,
        session_sample_limit: int = 100,
    ) -> HTTPBridgeRuntimeSnapshot:
        snapshot_started_at = time.monotonic()
        dashboard_settings = await self._proxy_dashboard_settings()
        app_settings = self._proxy_runtime_settings()
        runtime_config = _http_bridge_runtime_config(dashboard_settings, app_settings)
        now = time.monotonic()

        async with self._http_bridge_lock:
            session_items = list(self._http_bridge_sessions.items())
            inflight_count = len(self._http_bridge_inflight_sessions)

        observations: list[_HTTPBridgeRuntimeSessionObservation] = []

        for key, session in session_items:
            async with session.pending_lock:
                pending_count = len(session.pending_requests)
                queued_count = session.queued_request_count

            shard_index = _http_bridge_soft_shard_index(key)
            parallel_index = _http_bridge_busy_parallel_index(key)
            account_label = _http_bridge_account_label(session.account)
            account_status = _account_status_value(session.account.status)
            observations.append(
                _HTTPBridgeRuntimeSessionObservation(
                    family_hash=_hash_identifier(_http_bridge_family_affinity_key(key)),
                    active_account=(
                        not session.closed
                        and session.account.id is not None
                        and account_status == AccountStatus.ACTIVE.value
                    ),
                    snapshot=HTTPBridgeRuntimeSessionSnapshot(
                        affinity_kind=key.affinity_kind,
                        affinity_key_hash=_hash_identifier(key.affinity_key) if key.affinity_key else None,
                        key_strength=key.strength or "soft",
                        shard_index=shard_index,
                        parallel_index=parallel_index,
                        account_id=session.account.id,
                        account_label=account_label,
                        account_status=account_status,
                        model=session.request_model,
                        codex_session=session.codex_session,
                        closed=session.closed,
                        pending_request_count=pending_count,
                        queued_request_count=queued_count,
                        last_used_ago_ms=_monotonic_age_ms(now, session.last_used_at),
                        idle_ttl_seconds=session.idle_ttl_seconds,
                        reconnect_requested=session.upstream_control.reconnect_requested,
                        prewarmed=session.prewarmed,
                        previous_response_count=len(session.previous_response_ids),
                        turn_state_alias_count=len(session.downstream_turn_state_aliases),
                        upstream_reconnect_count=session.upstream_reconnect_count,
                        has_last_completed_response=session.last_completed_response_id is not None,
                    ),
                ),
            )

        summary = _summarize_http_bridge_runtime_sessions(observations)
        capacity_snapshot = await self._http_bridge_account_model_capacity_snapshot(
            active_session_counts_by_account=summary.active_session_counts_by_account,
            idle_session_counts_by_account=summary.idle_session_counts_by_account,
            account_model_session_limit=runtime_config.account_model_session_limit,
        )
        health = await self._http_bridge_runtime_health_snapshot(snapshot_started_at=snapshot_started_at)
        upstream_egress = _upstream_egress_runtime_snapshot(
            now=snapshot_started_at,
            settings=app_settings,
        )
        return _build_http_bridge_runtime_snapshot(
            config=runtime_config,
            summary=summary,
            inflight_session_creations=inflight_count,
            capacity=capacity_snapshot,
            health=health,
            upstream_egress=upstream_egress,
            session_sample_limit=session_sample_limit,
        )

    async def _http_bridge_runtime_health_snapshot(
        self: _HTTPBridgeRuntimeCollectionService,
        *,
        snapshot_started_at: float,
    ) -> HTTPBridgeRuntimeHealthSnapshot:
        try:
            async with self._repo_factory() as repos:
                latency = await repos.request_logs.bridge_latency_health_snapshot()
        except Exception:
            logger.warning("Failed to load HTTP bridge latency health snapshot", exc_info=True)
            latency = RequestLatencyHealthSnapshot(
                anchor_at=None,
                latency_first_token_p50_ms=None,
                latency_first_token_p95_ms=None,
                latency_first_token_p99_ms=None,
                success_rate_percent=None,
                success_count=0,
                request_count=0,
                history=[],
            )
        endpoint_ping_ms = _monotonic_age_ms(time.monotonic(), snapshot_started_at)
        return HTTPBridgeRuntimeHealthSnapshot(
            anchor_at=latency.anchor_at,
            latency_first_token_p50_ms=latency.latency_first_token_p50_ms,
            latency_first_token_p95_ms=latency.latency_first_token_p95_ms,
            latency_first_token_p99_ms=latency.latency_first_token_p99_ms,
            endpoint_ping_ms=endpoint_ping_ms,
            success_rate_percent=latency.success_rate_percent,
            success_count=latency.success_count,
            request_count=latency.request_count,
            next_update_seconds=60,
            status=_http_bridge_runtime_health_status(
                latency_first_token_p95_ms=latency.latency_first_token_p95_ms,
                success_rate_percent=latency.success_rate_percent,
            ),
            history=[
                HTTPBridgeRuntimeHealthHistoryBucket(
                    bucket_start=bucket.bucket_start,
                    latency_first_token_p50_ms=bucket.latency_first_token_p50_ms,
                    latency_first_token_p95_ms=bucket.latency_first_token_p95_ms,
                    success_count=bucket.success_count,
                    error_count=bucket.error_count,
                    status=bucket.status,
                )
                for bucket in latency.history
            ],
        )

    async def _http_bridge_account_model_capacity_snapshot(
        self: _HTTPBridgeRuntimeCollectionService,
        *,
        active_session_counts_by_account: dict[str, int],
        idle_session_counts_by_account: dict[str, int],
        account_model_session_limit: int,
    ) -> _HTTPBridgeAccountModelCapacitySnapshot:
        if account_model_session_limit <= 0:
            return _http_bridge_account_model_capacity_snapshot(
                active_account_ids=set(),
                active_session_counts_by_account=active_session_counts_by_account,
                idle_session_counts_by_account=idle_session_counts_by_account,
                account_model_session_limit=account_model_session_limit,
            )

        active_account_ids: set[str] = set()
        try:
            async with self._repo_factory() as repos:
                accounts = await repos.accounts.list_accounts()
                for account in accounts:
                    if account.id is None or _account_status_value(account.status) != AccountStatus.ACTIVE.value:
                        continue
                    active_account_ids.add(account.id)
        except Exception:
            logger.warning("Failed to load accounts for HTTP bridge capacity snapshot", exc_info=True)
            active_account_ids = set(active_session_counts_by_account)

        return _http_bridge_account_model_capacity_snapshot(
            active_account_ids=active_account_ids,
            active_session_counts_by_account=active_session_counts_by_account,
            idle_session_counts_by_account=idle_session_counts_by_account,
            account_model_session_limit=account_model_session_limit,
        )
