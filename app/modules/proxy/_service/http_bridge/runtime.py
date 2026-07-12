from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config.settings import Settings
from app.db.models import DashboardSettings


@dataclass(frozen=True, slots=True)
class HTTPBridgeRequestStatusSnapshot:
    request_id: str
    observed_state: str
    state_detail: str | None
    live: bool
    matched_by: str | None = None
    account_id: str | None = None
    request_model: str | None = None
    session_affinity_kind: str | None = None
    session_affinity_key_hash: str | None = None
    session_api_key_id: str | None = None
    session_codex: bool = False
    session_closed: bool = False
    reconnect_requested: bool = False
    queued_request_count: int = 0
    pending_request_count: int = 0
    last_used_ago_ms: int | None = None
    last_upstream_event_ago_ms: int | None = None
    last_downstream_emit_ago_ms: int | None = None
    upstream_turn_state: str | None = None
    downstream_turn_state: str | None = None
    matched_request_id: str | None = None
    matched_response_id: str | None = None
    matched_previous_response_id: str | None = None
    awaiting_response_created: bool = False
    replay_count: int = 0
    downstream_connected: bool | None = None
    request_age_ms: int | None = None
    request_last_upstream_event_ago_ms: int | None = None
    request_last_downstream_emit_ago_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _HTTPBridgePressureCapacityHint:
    capacity: int
    account_ids: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeConfigSnapshot:
    enabled: bool
    max_sessions: int
    queue_limit: int
    account_model_session_limit: int
    soft_shard_pending_limit: int
    soft_shard_max_shards: int
    idle_ttl_seconds: float
    codex_idle_ttl_seconds: float
    prompt_cache_idle_ttl_seconds: float
    gateway_safe_mode: bool


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeGroupSnapshot:
    key: str
    label: str
    sessions: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    reconnect_requested_sessions: int


@dataclass(frozen=True, slots=True)
class HTTPBridgeAccountRuntimeSnapshot:
    account_id: str
    sessions: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    reconnect_requested_sessions: int


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeShardFamilySnapshot:
    family_hash: str
    affinity_kind: str
    sessions: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    shard_sessions: int
    parallel_sessions: int
    accounts: list[str]
    models: list[str]


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeSessionSnapshot:
    affinity_kind: str
    affinity_key_hash: str | None
    key_strength: str
    shard_index: int
    parallel_index: int
    account_id: str | None
    account_label: str | None
    account_status: str | None
    model: str | None
    codex_session: bool
    closed: bool
    pending_request_count: int
    queued_request_count: int
    last_used_ago_ms: int | None
    idle_ttl_seconds: float
    reconnect_requested: bool
    prewarmed: bool
    previous_response_count: int
    turn_state_alias_count: int
    upstream_reconnect_count: int
    has_last_completed_response: bool


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeHealthHistoryBucket:
    bucket_start: datetime
    latency_first_token_p50_ms: int | None
    latency_first_token_p95_ms: int | None
    success_count: int
    error_count: int
    status: str


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeHealthSnapshot:
    anchor_at: datetime | None
    latency_first_token_p50_ms: int | None
    latency_first_token_p95_ms: int | None
    latency_first_token_p99_ms: int | None
    endpoint_ping_ms: int | None
    success_rate_percent: float | None
    success_count: int
    request_count: int
    next_update_seconds: int
    status: str
    history: list[HTTPBridgeRuntimeHealthHistoryBucket]


@dataclass(frozen=True, slots=True)
class UpstreamEgressRuntimeSnapshot:
    mode: str
    selected_route: str
    proxy_configured: bool
    direct_ok: bool | None
    proxy_ok: bool | None
    direct_latency_ms: int | None
    proxy_latency_ms: int | None
    direct_failure_streak: int
    direct_success_streak: int
    last_probe_ago_ms: int | None
    last_switch_ago_ms: int | None


@dataclass(frozen=True, slots=True)
class HTTPBridgeRuntimeSnapshot:
    config: HTTPBridgeRuntimeConfigSnapshot
    total_sessions: int
    active_sessions: int
    closed_sessions: int
    inflight_session_creations: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    prompt_cache_sessions: int
    hard_sessions: int
    soft_sessions: int
    soft_shard_sessions: int
    busy_parallel_sessions: int
    reconnect_requested_sessions: int
    prewarmed_sessions: int
    capacity_used_percent: float
    available_parallel_capacity: int | None
    free_account_model_session_slots: int | None
    reclaimable_idle_sessions: int
    account_model_session_capacity: int | None
    health: HTTPBridgeRuntimeHealthSnapshot
    upstream_egress: UpstreamEgressRuntimeSnapshot
    by_account: list[HTTPBridgeRuntimeGroupSnapshot]
    by_affinity_kind: list[HTTPBridgeRuntimeGroupSnapshot]
    by_model: list[HTTPBridgeRuntimeGroupSnapshot]
    shard_families: list[HTTPBridgeRuntimeShardFamilySnapshot]
    sessions: list[HTTPBridgeRuntimeSessionSnapshot]


@dataclass(frozen=True, slots=True)
class _HTTPBridgeRuntimeConfig:
    enabled: bool
    idle_ttl_seconds: float
    codex_idle_ttl_seconds: float
    max_sessions: int
    queue_limit: int
    account_model_session_limit: int
    soft_shard_pending_limit: int
    soft_shard_max_shards: int
    prompt_cache_idle_ttl_seconds: float
    gateway_safe_mode: bool


def _http_bridge_runtime_config(
    dashboard_settings: DashboardSettings,
    app_settings: Settings,
) -> _HTTPBridgeRuntimeConfig:
    return _HTTPBridgeRuntimeConfig(
        enabled=app_settings.http_responses_session_bridge_enabled,
        idle_ttl_seconds=app_settings.http_responses_session_bridge_idle_ttl_seconds,
        codex_idle_ttl_seconds=app_settings.http_responses_session_bridge_codex_idle_ttl_seconds,
        max_sessions=app_settings.http_responses_session_bridge_max_sessions,
        queue_limit=app_settings.http_responses_session_bridge_queue_limit,
        account_model_session_limit=app_settings.proxy_http_bridge_account_model_session_limit,
        soft_shard_pending_limit=app_settings.http_responses_session_bridge_soft_shard_pending_limit,
        soft_shard_max_shards=app_settings.http_responses_session_bridge_soft_shard_max_shards,
        prompt_cache_idle_ttl_seconds=float(
            dashboard_settings.http_responses_session_bridge_prompt_cache_idle_ttl_seconds,
        ),
        gateway_safe_mode=dashboard_settings.http_responses_session_bridge_gateway_safe_mode,
    )


@dataclass(frozen=True, slots=True)
class _HTTPBridgeAccountModelCapacitySnapshot:
    available_parallel_capacity: int | None
    free_account_model_session_slots: int | None
    reclaimable_idle_sessions: int
    account_model_session_capacity: int | None


@dataclass(frozen=True, slots=True)
class _HTTPBridgeRuntimeSessionObservation:
    snapshot: HTTPBridgeRuntimeSessionSnapshot
    family_hash: str
    active_account: bool


@dataclass(frozen=True, slots=True)
class _HTTPBridgeRuntimeSessionSummary:
    total_sessions: int
    active_sessions: int
    closed_sessions: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    prompt_cache_sessions: int
    hard_sessions: int
    soft_sessions: int
    soft_shard_sessions: int
    busy_parallel_sessions: int
    reconnect_requested_sessions: int
    prewarmed_sessions: int
    active_session_counts_by_account: dict[str, int]
    idle_session_counts_by_account: dict[str, int]
    by_account: list[HTTPBridgeRuntimeGroupSnapshot]
    by_affinity_kind: list[HTTPBridgeRuntimeGroupSnapshot]
    by_model: list[HTTPBridgeRuntimeGroupSnapshot]
    shard_families: list[HTTPBridgeRuntimeShardFamilySnapshot]
    sessions: list[HTTPBridgeRuntimeSessionSnapshot]


@dataclass(slots=True)
class _HTTPBridgeRuntimeGroupAccumulator:
    key: str
    label: str
    sessions: int = 0
    pending_requests: int = 0
    queued_requests: int = 0
    busy_sessions: int = 0
    codex_sessions: int = 0
    reconnect_requested_sessions: int = 0

    def add(self, session: HTTPBridgeRuntimeSessionSnapshot) -> None:
        self.sessions += 1
        self.pending_requests += session.pending_request_count
        self.queued_requests += session.queued_request_count
        if session.pending_request_count > 0:
            self.busy_sessions += 1
        if session.codex_session:
            self.codex_sessions += 1
        if session.reconnect_requested:
            self.reconnect_requested_sessions += 1

    def snapshot(self) -> HTTPBridgeRuntimeGroupSnapshot:
        return HTTPBridgeRuntimeGroupSnapshot(
            key=self.key,
            label=self.label,
            sessions=self.sessions,
            pending_requests=self.pending_requests,
            queued_requests=self.queued_requests,
            busy_sessions=self.busy_sessions,
            codex_sessions=self.codex_sessions,
            reconnect_requested_sessions=self.reconnect_requested_sessions,
        )


@dataclass(slots=True)
class _HTTPBridgeRuntimeShardFamilyAccumulator:
    family_hash: str
    affinity_kind: str
    sessions: int = 0
    pending_requests: int = 0
    queued_requests: int = 0
    busy_sessions: int = 0
    codex_sessions: int = 0
    shard_sessions: int = 0
    parallel_sessions: int = 0
    accounts: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)

    def add(self, session: HTTPBridgeRuntimeSessionSnapshot) -> None:
        self.sessions += 1
        self.pending_requests += session.pending_request_count
        self.queued_requests += session.queued_request_count
        if session.pending_request_count > 0:
            self.busy_sessions += 1
        if session.codex_session:
            self.codex_sessions += 1
        if session.shard_index > 0:
            self.shard_sessions += 1
        if session.parallel_index > 0:
            self.parallel_sessions += 1
        if session.account_id:
            self.accounts.add(session.account_id)
        if session.model:
            self.models.add(session.model)

    def snapshot(self) -> HTTPBridgeRuntimeShardFamilySnapshot:
        return HTTPBridgeRuntimeShardFamilySnapshot(
            family_hash=self.family_hash,
            affinity_kind=self.affinity_kind,
            sessions=self.sessions,
            pending_requests=self.pending_requests,
            queued_requests=self.queued_requests,
            busy_sessions=self.busy_sessions,
            codex_sessions=self.codex_sessions,
            shard_sessions=self.shard_sessions,
            parallel_sessions=self.parallel_sessions,
            accounts=sorted(self.accounts),
            models=sorted(self.models),
        )


def _summarize_http_bridge_runtime_sessions(
    observations: list[_HTTPBridgeRuntimeSessionObservation],
) -> _HTTPBridgeRuntimeSessionSummary:
    by_account: dict[str, _HTTPBridgeRuntimeGroupAccumulator] = {}
    by_affinity_kind: dict[str, _HTTPBridgeRuntimeGroupAccumulator] = {}
    by_model: dict[str, _HTTPBridgeRuntimeGroupAccumulator] = {}
    shard_families: dict[str, _HTTPBridgeRuntimeShardFamilyAccumulator] = {}
    active_session_counts_by_account: dict[str, int] = {}
    idle_session_counts_by_account: dict[str, int] = {}

    active_sessions = 0
    closed_sessions = 0
    pending_requests = 0
    queued_requests = 0
    busy_sessions = 0
    codex_sessions = 0
    prompt_cache_sessions = 0
    hard_sessions = 0
    soft_sessions = 0
    soft_shard_sessions = 0
    busy_parallel_sessions = 0
    reconnect_requested_sessions = 0
    prewarmed_sessions = 0

    for observation in observations:
        session = observation.snapshot
        if session.closed:
            closed_sessions += 1
        else:
            active_sessions += 1
        pending_requests += session.pending_request_count
        queued_requests += session.queued_request_count
        if session.pending_request_count > 0:
            busy_sessions += 1
        if session.codex_session:
            codex_sessions += 1
        if session.affinity_kind == "prompt_cache":
            prompt_cache_sessions += 1
        if session.key_strength == "hard":
            hard_sessions += 1
        else:
            soft_sessions += 1
        if session.shard_index > 0:
            soft_shard_sessions += 1
        if session.parallel_index > 0:
            busy_parallel_sessions += 1
        if session.reconnect_requested:
            reconnect_requested_sessions += 1
        if session.prewarmed:
            prewarmed_sessions += 1

        if observation.active_account and session.account_id:
            active_session_counts_by_account[session.account_id] = (
                active_session_counts_by_account.get(session.account_id, 0) + 1
            )
            if session.pending_request_count == 0:
                idle_session_counts_by_account[session.account_id] = (
                    idle_session_counts_by_account.get(session.account_id, 0) + 1
                )

        account_key = session.account_id or "unknown"
        account_group = by_account.setdefault(
            account_key,
            _HTTPBridgeRuntimeGroupAccumulator(
                key=account_key,
                label=session.account_label or account_key,
            ),
        )
        account_group.add(session)

        affinity_group = by_affinity_kind.setdefault(
            session.affinity_kind,
            _HTTPBridgeRuntimeGroupAccumulator(
                key=session.affinity_kind,
                label=session.affinity_kind,
            ),
        )
        affinity_group.add(session)

        model_key = session.model or "unknown"
        model_group = by_model.setdefault(
            model_key,
            _HTTPBridgeRuntimeGroupAccumulator(key=model_key, label=model_key),
        )
        model_group.add(session)

        family = shard_families.setdefault(
            observation.family_hash,
            _HTTPBridgeRuntimeShardFamilyAccumulator(
                family_hash=observation.family_hash,
                affinity_kind=session.affinity_kind,
            ),
        )
        family.add(session)

    sessions = sorted(
        (observation.snapshot for observation in observations),
        key=lambda item: (
            -item.pending_request_count,
            -item.queued_request_count,
            item.last_used_ago_ms if item.last_used_ago_ms is not None else 0,
            item.affinity_key_hash or "",
        ),
    )
    return _HTTPBridgeRuntimeSessionSummary(
        total_sessions=len(observations),
        active_sessions=active_sessions,
        closed_sessions=closed_sessions,
        pending_requests=pending_requests,
        queued_requests=queued_requests,
        busy_sessions=busy_sessions,
        codex_sessions=codex_sessions,
        prompt_cache_sessions=prompt_cache_sessions,
        hard_sessions=hard_sessions,
        soft_sessions=soft_sessions,
        soft_shard_sessions=soft_shard_sessions,
        busy_parallel_sessions=busy_parallel_sessions,
        reconnect_requested_sessions=reconnect_requested_sessions,
        prewarmed_sessions=prewarmed_sessions,
        active_session_counts_by_account=active_session_counts_by_account,
        idle_session_counts_by_account=idle_session_counts_by_account,
        by_account=_sorted_runtime_groups(by_account.values()),
        by_affinity_kind=_sorted_runtime_groups(by_affinity_kind.values()),
        by_model=_sorted_runtime_groups(by_model.values()),
        shard_families=_sorted_runtime_shard_families(shard_families.values()),
        sessions=sessions,
    )


def _http_bridge_account_model_capacity_snapshot(
    *,
    active_account_ids: set[str],
    active_session_counts_by_account: dict[str, int],
    idle_session_counts_by_account: dict[str, int],
    account_model_session_limit: int,
) -> _HTTPBridgeAccountModelCapacitySnapshot:
    if account_model_session_limit <= 0:
        return _HTTPBridgeAccountModelCapacitySnapshot(
            available_parallel_capacity=None,
            free_account_model_session_slots=None,
            reclaimable_idle_sessions=sum(idle_session_counts_by_account.values()),
            account_model_session_capacity=None,
        )

    total_capacity = len(active_account_ids) * account_model_session_limit
    free_slots = sum(
        max(0, account_model_session_limit - active_session_counts_by_account.get(account_id, 0))
        for account_id in active_account_ids
    )
    reclaimable_idle_sessions = sum(
        idle_count
        for account_id, idle_count in idle_session_counts_by_account.items()
        if account_id in active_account_ids
    )
    return _HTTPBridgeAccountModelCapacitySnapshot(
        available_parallel_capacity=free_slots + reclaimable_idle_sessions,
        free_account_model_session_slots=free_slots,
        reclaimable_idle_sessions=reclaimable_idle_sessions,
        account_model_session_capacity=total_capacity,
    )


def _build_http_bridge_runtime_snapshot(
    *,
    config: _HTTPBridgeRuntimeConfig,
    summary: _HTTPBridgeRuntimeSessionSummary,
    inflight_session_creations: int,
    capacity: _HTTPBridgeAccountModelCapacitySnapshot,
    health: HTTPBridgeRuntimeHealthSnapshot,
    upstream_egress: UpstreamEgressRuntimeSnapshot,
    session_sample_limit: int,
) -> HTTPBridgeRuntimeSnapshot:
    capacity_used_percent = (
        (summary.total_sessions + inflight_session_creations) / config.max_sessions * 100.0
        if config.max_sessions > 0
        else 0.0
    )
    session_limit = max(1, min(session_sample_limit, 500))
    return HTTPBridgeRuntimeSnapshot(
        config=HTTPBridgeRuntimeConfigSnapshot(
            enabled=config.enabled,
            max_sessions=config.max_sessions,
            queue_limit=config.queue_limit,
            account_model_session_limit=config.account_model_session_limit,
            soft_shard_pending_limit=config.soft_shard_pending_limit,
            soft_shard_max_shards=config.soft_shard_max_shards,
            idle_ttl_seconds=config.idle_ttl_seconds,
            codex_idle_ttl_seconds=config.codex_idle_ttl_seconds,
            prompt_cache_idle_ttl_seconds=config.prompt_cache_idle_ttl_seconds,
            gateway_safe_mode=config.gateway_safe_mode,
        ),
        total_sessions=summary.total_sessions,
        active_sessions=summary.active_sessions,
        closed_sessions=summary.closed_sessions,
        inflight_session_creations=inflight_session_creations,
        pending_requests=summary.pending_requests,
        queued_requests=summary.queued_requests,
        busy_sessions=summary.busy_sessions,
        codex_sessions=summary.codex_sessions,
        prompt_cache_sessions=summary.prompt_cache_sessions,
        hard_sessions=summary.hard_sessions,
        soft_sessions=summary.soft_sessions,
        soft_shard_sessions=summary.soft_shard_sessions,
        busy_parallel_sessions=summary.busy_parallel_sessions,
        reconnect_requested_sessions=summary.reconnect_requested_sessions,
        prewarmed_sessions=summary.prewarmed_sessions,
        capacity_used_percent=round(capacity_used_percent, 2),
        available_parallel_capacity=capacity.available_parallel_capacity,
        free_account_model_session_slots=capacity.free_account_model_session_slots,
        reclaimable_idle_sessions=capacity.reclaimable_idle_sessions,
        account_model_session_capacity=capacity.account_model_session_capacity,
        health=health,
        upstream_egress=upstream_egress,
        by_account=summary.by_account,
        by_affinity_kind=summary.by_affinity_kind,
        by_model=summary.by_model,
        shard_families=summary.shard_families,
        sessions=summary.sessions[:session_limit],
    )


def _sorted_runtime_groups(
    groups: Iterable[_HTTPBridgeRuntimeGroupAccumulator],
) -> list[HTTPBridgeRuntimeGroupSnapshot]:
    return [
        group.snapshot()
        for group in sorted(
            groups,
            key=lambda item: (-item.pending_requests, -item.sessions, item.label),
        )
    ]


def _sorted_runtime_shard_families(
    families: Iterable[_HTTPBridgeRuntimeShardFamilyAccumulator],
) -> list[HTTPBridgeRuntimeShardFamilySnapshot]:
    return [
        family.snapshot()
        for family in sorted(
            families,
            key=lambda item: (-item.pending_requests, -item.sessions, item.family_hash),
        )
    ]


def _http_bridge_runtime_health_status(
    *,
    latency_first_token_p95_ms: int | None,
    success_rate_percent: float | None,
) -> str:
    if latency_first_token_p95_ms is None and success_rate_percent is None:
        return "unknown"
    if (success_rate_percent is not None and success_rate_percent < 95.0) or (
        latency_first_token_p95_ms is not None and latency_first_token_p95_ms >= 30_000
    ):
        return "critical"
    if (success_rate_percent is not None and success_rate_percent < 98.0) or (
        latency_first_token_p95_ms is not None and latency_first_token_p95_ms >= 15_000
    ):
        return "warning"
    return "ok"
