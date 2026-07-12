from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.http_bridge import runtime


def _runtime_session(**changes: object) -> runtime.HTTPBridgeRuntimeSessionSnapshot:
    base = runtime.HTTPBridgeRuntimeSessionSnapshot(
        affinity_kind="prompt_cache",
        affinity_key_hash="affinity",
        key_strength="soft",
        shard_index=0,
        parallel_index=0,
        account_id="account-a",
        account_label="a@example.com",
        account_status="active",
        model="gpt-5.5",
        codex_session=False,
        closed=False,
        pending_request_count=0,
        queued_request_count=0,
        last_used_ago_ms=100,
        idle_ttl_seconds=120.0,
        reconnect_requested=False,
        prewarmed=False,
        previous_response_count=0,
        turn_state_alias_count=0,
        upstream_reconnect_count=0,
        has_last_completed_response=False,
    )
    return replace(base, **changes)


def test_proxy_service_reexports_runtime_contracts() -> None:
    assert proxy_service.HTTPBridgeRequestStatusSnapshot is runtime.HTTPBridgeRequestStatusSnapshot
    assert proxy_service.HTTPBridgeRuntimeConfigSnapshot is runtime.HTTPBridgeRuntimeConfigSnapshot
    assert proxy_service.HTTPBridgeRuntimeGroupSnapshot is runtime.HTTPBridgeRuntimeGroupSnapshot
    assert proxy_service.HTTPBridgeRuntimeShardFamilySnapshot is runtime.HTTPBridgeRuntimeShardFamilySnapshot
    assert proxy_service.HTTPBridgeRuntimeSnapshot is runtime.HTTPBridgeRuntimeSnapshot
    assert proxy_service.UpstreamEgressRuntimeSnapshot is runtime.UpstreamEgressRuntimeSnapshot


@pytest.mark.parametrize(
    ("latency_p95_ms", "success_rate_percent", "expected"),
    [
        (None, None, "unknown"),
        (14_999, 98.0, "ok"),
        (15_000, 100.0, "warning"),
        (1_000, 97.99, "warning"),
        (30_000, 100.0, "critical"),
        (1_000, 94.99, "critical"),
    ],
)
def test_http_bridge_runtime_health_thresholds(
    latency_p95_ms: int | None,
    success_rate_percent: float | None,
    expected: str,
) -> None:
    assert (
        runtime._http_bridge_runtime_health_status(
            latency_first_token_p95_ms=latency_p95_ms,
            success_rate_percent=success_rate_percent,
        )
        == expected
    )


def test_http_bridge_runtime_summary_groups_and_orders_sessions() -> None:
    busy = _runtime_session(
        key_strength="hard",
        codex_session=True,
        pending_request_count=1,
        queued_request_count=1,
        last_used_ago_ms=500,
    )
    idle_shard = _runtime_session(
        affinity_key_hash="shard",
        shard_index=1,
        parallel_index=1,
        account_id="account-b",
        account_label="b@example.com",
        last_used_ago_ms=10,
        reconnect_requested=True,
        prewarmed=True,
    )

    summary = runtime._summarize_http_bridge_runtime_sessions(
        [
            runtime._HTTPBridgeRuntimeSessionObservation(
                snapshot=idle_shard,
                family_hash="family",
                active_account=True,
            ),
            runtime._HTTPBridgeRuntimeSessionObservation(
                snapshot=busy,
                family_hash="family",
                active_account=True,
            ),
        ]
    )

    assert summary.total_sessions == 2
    assert summary.pending_requests == 1
    assert summary.queued_requests == 1
    assert summary.busy_sessions == 1
    assert summary.codex_sessions == 1
    assert summary.hard_sessions == 1
    assert summary.soft_sessions == 1
    assert summary.soft_shard_sessions == 1
    assert summary.busy_parallel_sessions == 1
    assert summary.reconnect_requested_sessions == 1
    assert summary.prewarmed_sessions == 1
    assert summary.active_session_counts_by_account == {"account-a": 1, "account-b": 1}
    assert summary.idle_session_counts_by_account == {"account-b": 1}
    assert summary.by_account[0].key == "account-a"
    assert summary.shard_families[0].sessions == 2
    assert summary.sessions[0] is busy


def test_http_bridge_runtime_capacity_includes_reclaimable_idle_sessions() -> None:
    capacity = runtime._http_bridge_account_model_capacity_snapshot(
        active_account_ids={"account-a", "account-b", "account-c"},
        active_session_counts_by_account={"account-a": 1, "account-b": 1},
        idle_session_counts_by_account={"account-b": 1, "inactive": 4},
        account_model_session_limit=20,
    )

    assert capacity.account_model_session_capacity == 60
    assert capacity.free_account_model_session_slots == 58
    assert capacity.reclaimable_idle_sessions == 1
    assert capacity.available_parallel_capacity == 59
