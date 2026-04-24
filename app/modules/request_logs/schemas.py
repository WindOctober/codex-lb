from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.modules.shared.schemas import DashboardModel


class RequestLogEntry(DashboardModel):
    requested_at: datetime
    account_id: str | None = None
    plan_type: str | None = None
    api_key_name: str | None = None
    request_id: str
    model: str
    transport: str | None = None
    service_tier: str | None = None
    requested_service_tier: str | None = None
    actual_service_tier: str | None = None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_effort: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    latency_first_token_ms: int | None = None


class RequestLogsResponse(DashboardModel):
    requests: list[RequestLogEntry] = Field(default_factory=list)
    total: int
    has_more: bool


class RequestLogModelOption(DashboardModel):
    model: str
    reasoning_effort: str | None = None


class RequestLogFilterOptionsResponse(DashboardModel):
    account_ids: list[str] = Field(default_factory=list)
    model_options: list[RequestLogModelOption] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)


class RequestLogSessionStatusResponse(DashboardModel):
    request_id: str
    log_found: bool
    log_status: str | None = None
    log_error_code: str | None = None
    observed_state: str
    state_detail: str | None = None
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
