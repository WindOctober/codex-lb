from __future__ import annotations

from datetime import datetime
from typing import List, Literal

from pydantic import Field

from app.modules.accounts.schemas import AccountSummary
from app.modules.shared.schemas import DashboardModel
from app.modules.usage.schemas import MetricsTrends, UsageWindow, UsageWindowResponse

DashboardOverviewTimeframeKey = Literal["1d", "7d", "30d"]


class DashboardOverviewTimeframe(DashboardModel):
    key: DashboardOverviewTimeframeKey
    window_minutes: int = Field(alias="windowMinutes")
    bucket_seconds: int = Field(alias="bucketSeconds")
    bucket_count: int = Field(alias="bucketCount")


class DashboardUsageCost(DashboardModel):
    currency: str
    total_usd: float = Field(alias="totalUsd")


class DashboardUsageMetrics(DashboardModel):
    requests: int | None = None
    tokens: int | None = None
    cached_input_tokens: int | None = Field(default=None, alias="cachedInputTokens")
    error_rate: float | None = Field(default=None, alias="errorRate")
    error_count: int | None = Field(default=None, alias="errorCount")
    top_error: str | None = None


class DashboardOverviewSummary(DashboardModel):
    primary_window: UsageWindow
    secondary_window: UsageWindow | None = None
    cost: DashboardUsageCost
    metrics: DashboardUsageMetrics | None = None


class DashboardUsageWindows(DashboardModel):
    primary: UsageWindowResponse
    secondary: UsageWindowResponse | None = None


class DepletionResponse(DashboardModel):
    risk: float
    risk_level: str  # "safe" | "warning" | "danger" | "critical"
    burn_rate: float
    safe_usage_percent: float
    projected_exhaustion_at: datetime | None = None
    seconds_until_exhaustion: float | None = None


class DashboardBridgeRuntimeConfig(DashboardModel):
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


class DashboardBridgeRuntimeGroup(DashboardModel):
    key: str
    label: str
    sessions: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    reconnect_requested_sessions: int


class DashboardBridgeRuntimeShardFamily(DashboardModel):
    family_hash: str
    affinity_kind: str
    sessions: int
    pending_requests: int
    queued_requests: int
    busy_sessions: int
    codex_sessions: int
    shard_sessions: int
    parallel_sessions: int
    accounts: List[str]
    models: List[str]


class DashboardBridgeRuntimeSession(DashboardModel):
    affinity_kind: str
    affinity_key_hash: str | None = None
    key_strength: str
    shard_index: int
    parallel_index: int
    account_id: str | None = None
    account_label: str | None = None
    account_status: str | None = None
    model: str | None = None
    codex_session: bool
    closed: bool
    pending_request_count: int
    queued_request_count: int
    last_used_ago_ms: int | None = None
    idle_ttl_seconds: float
    reconnect_requested: bool
    prewarmed: bool
    previous_response_count: int
    turn_state_alias_count: int
    upstream_reconnect_count: int
    has_last_completed_response: bool


class DashboardBridgeRuntimeHealthHistoryBucket(DashboardModel):
    bucket_start: datetime
    latency_first_token_p50_ms: int | None = None
    latency_first_token_p95_ms: int | None = None
    success_count: int
    error_count: int
    status: str


class DashboardBridgeRuntimeHealth(DashboardModel):
    anchor_at: datetime | None = None
    latency_first_token_p50_ms: int | None = None
    latency_first_token_p95_ms: int | None = None
    latency_first_token_p99_ms: int | None = None
    endpoint_ping_ms: int | None = None
    success_rate_percent: float | None = None
    success_count: int
    request_count: int
    next_update_seconds: int
    status: str
    history: List[DashboardBridgeRuntimeHealthHistoryBucket]


class DashboardUpstreamEgressRuntime(DashboardModel):
    mode: str
    selected_route: str
    proxy_configured: bool
    direct_ok: bool | None = None
    proxy_ok: bool | None = None
    direct_latency_ms: int | None = None
    proxy_latency_ms: int | None = None
    direct_failure_streak: int
    direct_success_streak: int
    last_probe_ago_ms: int | None = None
    last_switch_ago_ms: int | None = None


class DashboardBridgeRuntimeResponse(DashboardModel):
    config: DashboardBridgeRuntimeConfig
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
    available_parallel_capacity: int | None = None
    free_account_model_session_slots: int | None = None
    reclaimable_idle_sessions: int
    account_model_session_capacity: int | None = None
    health: DashboardBridgeRuntimeHealth
    upstream_egress: DashboardUpstreamEgressRuntime
    by_account: List[DashboardBridgeRuntimeGroup]
    by_affinity_kind: List[DashboardBridgeRuntimeGroup]
    by_model: List[DashboardBridgeRuntimeGroup]
    shard_families: List[DashboardBridgeRuntimeShardFamily]
    sessions: List[DashboardBridgeRuntimeSession]


class DashboardOverviewResponse(DashboardModel):
    last_sync_at: datetime | None = None
    timeframe: DashboardOverviewTimeframe
    accounts: List[AccountSummary] = Field(default_factory=list)
    grouped_accounts: List[AccountSummary] = Field(default_factory=list)
    summary: DashboardOverviewSummary
    windows: DashboardUsageWindows
    trends: MetricsTrends
    depletion_primary: DepletionResponse | None = None
    depletion_secondary: DepletionResponse | None = None
