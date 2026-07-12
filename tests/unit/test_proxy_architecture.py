from __future__ import annotations

import ast
from pathlib import Path

PROXY_DIR = Path(__file__).resolve().parents[2] / "app" / "modules" / "proxy"
CORE_RESPONSE_CREATE_PATH = Path(__file__).resolve().parents[2] / "app" / "core" / "openai" / "response_create.py"
CORE_PROXY_CLIENT_PATH = Path(__file__).resolve().parents[2] / "app" / "core" / "clients" / "proxy.py"
SERVICE_PATH = PROXY_DIR / "service.py"
SERVICE_PACKAGE = PROXY_DIR / "_service"
AFFINITY_PATH = SERVICE_PACKAGE / "affinity.py"
ACCOUNT_FRESHNESS_PATH = SERVICE_PACKAGE / "account_freshness.py"
RESPONSE_CREATE_RUNTIME_PATH = SERVICE_PACKAGE / "response_create_runtime.py"
SESSION_ACQUIRE_PATH = SERVICE_PACKAGE / "http_bridge" / "session_acquire.py"
UPSTREAM_WEBSOCKET_PATH = SERVICE_PACKAGE / "upstream_websocket.py"
WEBSOCKET_EVENTS_PATH = SERVICE_PACKAGE / "websocket" / "events.py"
# This is a regression guard against responsibility moving back into the
# facade, not a design target for individual refactors.
MAX_SERVICE_LINES = 5_000

REQUIRED_IMPLEMENTATION_FILES = {
    "account_freshness.py",
    "api_key_usage.py",
    "affinity.py",
    "budget.py",
    "compact.py",
    "concurrency.py",
    "continuity.py",
    "observability.py",
    "rate_limits.py",
    "request_logging.py",
    "response_create.py",
    "response_create_runtime.py",
    "service_tier.py",
    "streaming.py",
    "support.py",
    "transcription.py",
    "upstream_account.py",
    "upstream_websocket.py",
    "websocket/connection.py",
    "websocket/events.py",
    "websocket/orchestration.py",
    "websocket/relay.py",
    "http_bridge/keys.py",
    "http_bridge/capacity.py",
    "http_bridge/lifecycle.py",
    "http_bridge/ownership.py",
    "http_bridge/owner_resolution.py",
    "http_bridge/policy.py",
    "http_bridge/request_submit.py",
    "http_bridge/runtime.py",
    "http_bridge/runtime_collection.py",
    "http_bridge/session_create.py",
    "http_bridge/session_acquire.py",
    "http_bridge/stream.py",
    "http_bridge/stream_policy.py",
    "http_bridge/upstream_events.py",
}

REQUIRED_FACADE_NAMES = {
    "ACCOUNT_PROVIDER_API_KEY",
    "AuthManager",
    "ProxyService",
    "RefreshError",
    "HTTPBridgeRequestStatusSnapshot",
    "HTTPBridgeRuntimeSnapshot",
    "_AffinityPolicy",
    "_HTTPBridgeSession",
    "_HTTPBridgeSessionKey",
    "_WebSocketRequestState",
    "_build_http_bridge_prewarm_text",
    "_websocket_disabled_for_account",
    "_derive_prompt_cache_key",
    "_extract_first_user_input",
    "_fingerprint_input_items",
    "_owner_lookup_session_id_from_headers",
    "_release_websocket_response_create_gate",
    "_response_create_client_metadata",
    "_slim_response_create_payload_for_upstream",
    "build_downstream_turn_state_accept_headers",
    "build_downstream_turn_state_response_headers",
    "ensure_downstream_turn_state",
    "ensure_http_downstream_turn_state",
    "_websocket_receive_timeout_for_pending_requests",
    "connect_responses_websocket",
}

REMOVED_PROXY_REDUNDANT_NAMES = {
    "_TEXT_DELTA_EVENT_TYPES",
    "_account_supports_http_bridge_request_model",
    "_call_core_compact_responses",
    "_match_websocket_request_state_for_previous_response_error",
    "_request_budget_seconds",
    "_resolve_prompt_cache_key",
    "_usage_window_row_from_entry",
}

REMOVED_SESSION_ACQUIRE_NAMES = {"logger", "logging"}

MOVED_REQUEST_SUBMIT_METHODS = {
    "_cleanup_http_bridge_submit_interruption",
    "_detach_http_bridge_request",
    "_maybe_prewarm_http_bridge_session",
    "_reconnect_http_bridge_session",
    "_retry_http_bridge_precreated_request",
    "_retry_http_bridge_request_on_fresh_upstream",
    "_retry_http_bridge_terminal_failure",
    "_submit_http_bridge_request",
}

MOVED_UPSTREAM_EVENT_METHODS = {
    "_process_http_bridge_upstream_text",
    "_relay_http_bridge_upstream_messages",
}

MOVED_LIFECYCLE_METHODS = {
    "_claim_durable_http_bridge_session",
    "_close_http_bridge_session",
    "_defer_next_durable_http_bridge_session_refresh",
    "_detach_http_bridge_session_for_background_close",
    "_evict_http_bridge_session_after_upstream_disconnect",
    "_prune_http_bridge_sessions_locked",
    "_promote_http_bridge_session_to_codex_affinity",
    "_refresh_durable_http_bridge_session",
    "_register_http_bridge_previous_response_id",
    "_register_http_bridge_turn_state",
    "_schedule_durable_http_bridge_session_refresh",
    "_schedule_http_bridge_session_close",
    "_settle_durable_http_bridge_session_refresh",
    "_unregister_http_bridge_previous_response_ids",
    "_unregister_http_bridge_previous_response_ids_locked",
    "_unregister_http_bridge_turn_states",
    "_unregister_http_bridge_turn_states_locked",
    "close_all_http_bridge_sessions",
    "mark_http_bridge_draining",
}

MOVED_CAPACITY_METHODS = {
    "_acquire_http_bridge_submit_lease_locked",
    "_evict_http_bridge_idle_session_for_account_model_capacity",
    "_evict_http_bridge_parallel_prompt_cache_pressure_locked",
    "_evict_http_bridge_pressure",
    "_http_bridge_pending_count",
    "_http_bridge_pressure_capacity_locked",
    "_http_bridge_pressure_current_count_locked",
    "_http_bridge_replacement_busy_count",
    "_release_http_bridge_submit_lease",
    "_resolve_http_bridge_pressure_capacity_hint",
    "_restore_http_bridge_session_after_reconnect",
    "_select_http_bridge_busy_parallel_key_locked",
    "_select_http_bridge_soft_shard_key_locked",
}

MOVED_SESSION_CREATE_METHODS = {
    "_create_http_bridge_session",
}

MOVED_SESSION_ACQUIRE_METHODS = {
    "_get_or_create_http_bridge_session",
}

MOVED_STREAM_METHODS = {
    "_reset_http_bridge_session_after_local_terminal_error",
    "_stream_http_bridge_session_events",
    "_stream_via_http_bridge",
}

MOVED_STREAM_POLICY_FUNCTIONS = {
    "_effective_http_bridge_idle_ttl_seconds",
    "_fingerprint_input_items",
    "_http_bridge_is_context_overflow_error",
    "_http_bridge_payload_looks_like_full_resend",
    "_http_bridge_payload_without_previous_response_id",
    "_http_bridge_request_stage",
    "_http_bridge_runtime_config",
    "_http_bridge_should_attempt_local_bootstrap_rebind",
    "_http_bridge_should_attempt_local_previous_response_recovery",
    "_http_bridge_should_rollover_after_context_overflow",
    "_input_prefix_matches_stored_context",
    "_make_http_bridge_session_key",
}

MOVED_ORDINARY_STREAM_METHODS = {
    "_stream_once",
    "_stream_with_retry",
}

MOVED_ORDINARY_STREAM_HELPERS = {
    "_account_upstream_base_url",
    "_account_upstream_wire_api",
    "_call_core_stream_responses",
    "_is_text_content_part",
    "_log_direct_sse_latency_breakdown",
    "_push_stream_attempt_timeout_overrides",
    "_resolve_upstream_stream_transport",
    "_should_retry_stream_error",
    "_should_suppress_text_done_event",
    "_upstream_account_header_value",
    "_websocket_disabled_for_account",
}

MOVED_WEBSOCKET_CONNECTION_METHODS = {
    "_connect_proxy_websocket",
    "_decide_websocket_failover_action",
    "_emit_websocket_connect_timeout",
    "_retry_websocket_connect_after_401",
    "_select_websocket_connect_account",
    "_try_open_websocket_connect_attempt",
}

MOVED_WEBSOCKET_RELAY_METHODS = {
    "_downstream_websocket_is_idle",
    "_emit_websocket_connect_failure",
    "_emit_websocket_proxy_request_timeout",
    "_emit_websocket_terminal_error",
    "_fail_expired_pending_websocket_requests",
    "_fail_pending_websocket_requests",
    "_finalize_websocket_request_state",
    "_next_websocket_receive_timeout",
    "_process_upstream_websocket_text",
    "_relay_upstream_websocket_messages",
    "_send_downstream_websocket_bytes",
    "_send_downstream_websocket_text",
    "_write_websocket_connect_failure",
}

MOVED_WEBSOCKET_RELAY_HELPERS = {
    "_websocket_receive_timeout_for_pending_requests",
}

MOVED_WEBSOCKET_ORCHESTRATION_METHODS = {
    "_prepare_websocket_response_create_request",
    "proxy_responses_websocket",
}

MOVED_WEBSOCKET_ORCHESTRATION_HELPERS = {
    "_app_error_to_websocket_event",
    "_is_websocket_response_create",
    "_parse_websocket_payload",
    "_response_create_client_metadata",
}

MOVED_RESPONSE_CREATE_POLICY_FUNCTIONS = {
    "_is_inline_image_reference",
    "_json_size_bytes",
    "_json_value_contains_input_image_part",
    "_response_create_inline_image_notice_item",
    "_response_create_inline_image_notice_part",
    "_response_create_recent_suffix_start",
    "_response_create_too_large_error_envelope",
    "_responses_request_contains_input_image",
    "_responses_request_uses_image_generation",
    "_safe_dump_slug",
    "_should_dump_oversized_response_create",
    "_should_slim_historical_tool_output",
    "_slim_historical_response_content",
    "_slim_historical_response_content_part",
    "_slim_historical_response_input_item",
    "_slim_response_create_payload_for_upstream",
    "_summarize_response_create_input",
    "_summarize_response_create_payload",
}

MOVED_RATE_LIMIT_METHODS = {
    "_build_additional_rate_limits",
    "_compute_rate_limit_headers",
    "_latest_usage_entries",
    "_latest_usage_rows",
    "_refresh_usage",
    "get_rate_limit_payload",
    "rate_limit_headers",
}

MOVED_REQUEST_LOGGING_METHODS = {
    "_write_request_log",
    "_write_stream_preflight_error",
    "rewrite_request_log_model",
}

MOVED_API_KEY_USAGE_METHODS = {
    "_release_websocket_reservation",
    "_reserve_websocket_api_key_usage",
    "_settle_compact_api_key_usage",
    "_settle_stream_api_key_usage",
}

MOVED_COMPACT_METHODS = {"compact_responses"}

MOVED_ACCOUNT_FRESHNESS_METHODS = {
    "_ensure_fresh",
    "_ensure_fresh_with_budget",
}

MOVED_RESPONSE_CREATE_RUNTIME_METHODS = {
    "_acquire_request_state_response_create_admission",
    "_prepare_http_bridge_request",
    "_prepare_response_bridge_request_state",
}

REQUIRED_RESPONSE_CREATE_COMPATIBILITY_METHODS = {
    "_enforce_response_create_size_limit_compatible",
    "_response_create_max_bytes_compatible",
}

MOVED_UPSTREAM_WEBSOCKET_METHODS = {
    "_open_upstream_websocket",
    "_open_upstream_websocket_with_budget",
}

REQUIRED_UPSTREAM_WEBSOCKET_COMPATIBILITY_METHODS = {
    "_connect_responses_websocket_compatible",
}

MOVED_RUNTIME_COLLECTION_METHODS = {
    "_http_bridge_account_model_capacity_snapshot",
    "_http_bridge_runtime_health_snapshot",
    "get_http_bridge_account_runtime_snapshot",
    "get_http_bridge_request_status",
    "get_http_bridge_runtime_snapshot",
}

MOVED_RUNTIME_COLLECTION_HELPERS = {
    "_upstream_egress_runtime_snapshot",
}

MOVED_TRANSCRIPTION_METHODS = {
    "transcribe",
}

MOVED_CONCURRENCY_METHODS = {
    "_account_model_concurrency_limit",
    "_account_model_concurrency_overload",
    "_acquire_http_bridge_connect_account_model_concurrency",
    "_full_account_model_concurrency_account_ids",
    "_full_http_bridge_connect_account_ids",
    "_full_http_bridge_session_account_ids",
    "_http_bridge_account_model_connect_limit",
    "_http_bridge_account_model_session_limit",
    "_http_bridge_session_request_budget_full",
    "_release_request_account_model_concurrency",
    "_try_acquire_account_model_concurrency",
    "_try_acquire_http_bridge_connect_account_model_concurrency",
    "_try_acquire_http_bridge_session_account_model_concurrency",
}

MOVED_CONTINUITY_METHODS = {
    "_durable_account_binding",
    "_remember_websocket_previous_response_owner",
    "_remember_websocket_previous_response_owner_miss",
    "_resolve_websocket_previous_response_owner",
}

MOVED_CONTINUITY_HELPERS = {
    "_previous_response_owner_lookup_failed_error_envelope",
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _defined_or_imported_names(module: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_proxy_service_architecture_ratchets() -> None:
    account_freshness = _parse(ACCOUNT_FRESHNESS_PATH)
    service = _parse(SERVICE_PATH)
    session_acquire = _parse(SESSION_ACQUIRE_PATH)
    response_create_runtime = _parse(RESPONSE_CREATE_RUNTIME_PATH)
    upstream_websocket = _parse(UPSTREAM_WEBSOCKET_PATH)
    assert len(SERVICE_PATH.read_text().splitlines()) <= MAX_SERVICE_LINES

    proxy_service = next(
        node for node in service.body if isinstance(node, ast.ClassDef) and node.name == "ProxyService"
    )
    base_names = {base.id for base in proxy_service.bases if isinstance(base, ast.Name)}
    assert "_HTTPBridgeRequestSubmitMixin" in base_names
    assert "_HTTPBridgeUpstreamEventsMixin" in base_names
    assert "_HTTPBridgeLifecycleMixin" in base_names
    assert "_HTTPBridgeCapacityMixin" in base_names
    assert "_HTTPBridgeSessionCreateMixin" in base_names
    assert "_HTTPBridgeSessionAcquireMixin" in base_names
    assert "_HTTPBridgeOwnerResolutionMixin" in base_names
    assert "_HTTPBridgeStreamMixin" in base_names
    assert "_StreamingMixin" in base_names
    assert "_WebSocketConnectionMixin" in base_names
    assert "_WebSocketRelayMixin" in base_names
    assert "_WebSocketOrchestrationMixin" in base_names
    assert "_RateLimitRuntimeMixin" in base_names
    assert "_RequestLoggingMixin" in base_names
    assert "_ApiKeyUsageRuntimeMixin" in base_names
    assert "_AccountFreshnessMixin" in base_names
    assert "_CompactRuntimeMixin" in base_names
    assert "_ResponseCreateRuntimeMixin" in base_names
    assert "_UpstreamWebSocketRuntimeMixin" in base_names
    assert "_HTTPBridgeRuntimeCollectionMixin" in base_names
    assert "_TranscriptionRuntimeMixin" in base_names
    assert "_ConcurrencyRuntimeMixin" in base_names
    assert "_ContinuityRuntimeMixin" in base_names

    local_methods = {
        node.name for node in proxy_service.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    account_freshness_mixin = next(
        node
        for node in account_freshness.body
        if isinstance(node, ast.ClassDef) and node.name == "_AccountFreshnessMixin"
    )
    account_freshness_methods = {
        node.name for node in account_freshness_mixin.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    response_create_mixin = next(
        node
        for node in response_create_runtime.body
        if isinstance(node, ast.ClassDef) and node.name == "_ResponseCreateRuntimeMixin"
    )
    response_create_methods = {
        node.name for node in response_create_mixin.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    upstream_websocket_mixin = next(
        node
        for node in upstream_websocket.body
        if isinstance(node, ast.ClassDef) and node.name == "_UpstreamWebSocketRuntimeMixin"
    )
    upstream_websocket_methods = {
        node.name for node in upstream_websocket_mixin.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert MOVED_RESPONSE_CREATE_RUNTIME_METHODS <= response_create_methods
    assert MOVED_ACCOUNT_FRESHNESS_METHODS <= account_freshness_methods
    assert MOVED_UPSTREAM_WEBSOCKET_METHODS <= upstream_websocket_methods
    assert REQUIRED_RESPONSE_CREATE_COMPATIBILITY_METHODS <= local_methods
    assert REQUIRED_UPSTREAM_WEBSOCKET_COMPATIBILITY_METHODS <= local_methods
    assert not local_methods & MOVED_REQUEST_SUBMIT_METHODS
    assert not local_methods & MOVED_UPSTREAM_EVENT_METHODS
    assert not local_methods & MOVED_LIFECYCLE_METHODS
    assert not local_methods & MOVED_CAPACITY_METHODS
    assert not local_methods & MOVED_SESSION_CREATE_METHODS
    assert not local_methods & MOVED_SESSION_ACQUIRE_METHODS
    assert not local_methods & MOVED_STREAM_METHODS
    assert not local_methods & MOVED_ORDINARY_STREAM_METHODS
    assert not local_methods & MOVED_WEBSOCKET_CONNECTION_METHODS
    assert not local_methods & MOVED_WEBSOCKET_RELAY_METHODS
    assert not local_methods & MOVED_WEBSOCKET_ORCHESTRATION_METHODS
    assert not local_methods & MOVED_RATE_LIMIT_METHODS
    assert not local_methods & MOVED_REQUEST_LOGGING_METHODS
    assert not local_methods & MOVED_API_KEY_USAGE_METHODS
    assert not local_methods & MOVED_COMPACT_METHODS
    assert not local_methods & MOVED_ACCOUNT_FRESHNESS_METHODS
    assert not local_methods & MOVED_RESPONSE_CREATE_RUNTIME_METHODS
    assert not local_methods & MOVED_UPSTREAM_WEBSOCKET_METHODS
    assert not local_methods & MOVED_RUNTIME_COLLECTION_METHODS
    assert not local_methods & MOVED_TRANSCRIPTION_METHODS
    assert not local_methods & MOVED_CONCURRENCY_METHODS
    assert not local_methods & MOVED_CONTINUITY_METHODS

    local_functions = {node.name for node in service.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert not local_functions & MOVED_STREAM_POLICY_FUNCTIONS
    assert not local_functions & MOVED_ORDINARY_STREAM_HELPERS
    assert not local_functions & MOVED_WEBSOCKET_RELAY_HELPERS
    assert not local_functions & MOVED_WEBSOCKET_ORCHESTRATION_HELPERS
    assert not local_functions & MOVED_RESPONSE_CREATE_POLICY_FUNCTIONS
    assert not local_functions & MOVED_RUNTIME_COLLECTION_HELPERS
    assert not local_functions & MOVED_CONTINUITY_HELPERS
    assert not local_functions & REQUIRED_UPSTREAM_WEBSOCKET_COMPATIBILITY_METHODS

    missing_facade_names = REQUIRED_FACADE_NAMES - _defined_or_imported_names(service)
    assert not missing_facade_names
    assert not REMOVED_PROXY_REDUNDANT_NAMES & _defined_or_imported_names(service)
    assert not REMOVED_SESSION_ACQUIRE_NAMES & _defined_or_imported_names(session_acquire)


def test_proxy_internal_modules_do_not_import_service_monolith() -> None:
    existing = {
        str(path.relative_to(SERVICE_PACKAGE)) for path in SERVICE_PACKAGE.rglob("*.py") if path.name != "__init__.py"
    }
    assert REQUIRED_IMPLEMENTATION_FILES <= existing

    violations: list[str] = []
    for path in SERVICE_PACKAGE.rglob("*.py"):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module == "app.modules.proxy.service":
                violations.append(str(path.relative_to(PROXY_DIR)))
            elif isinstance(node, ast.Import):
                if any(alias.name == "app.modules.proxy.service" for alias in node.names):
                    violations.append(str(path.relative_to(PROXY_DIR)))
    assert not violations


def test_response_create_policy_has_one_canonical_implementation() -> None:
    canonical = _parse(CORE_RESPONSE_CREATE_PATH)
    core_client = _parse(CORE_PROXY_CLIENT_PATH)
    service = _parse(SERVICE_PATH)

    canonical_functions = {
        node.name for node in canonical.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert MOVED_RESPONSE_CREATE_POLICY_FUNCTIONS <= canonical_functions

    for module in (core_client, service):
        local_functions = {
            node.name for node in module.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert not local_functions & MOVED_RESPONSE_CREATE_POLICY_FUNCTIONS

    for node in ast.walk(canonical):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.modules.proxy.service"
        elif isinstance(node, ast.Import):
            assert all(alias.name != "app.modules.proxy.service" for alias in node.names)


def test_session_id_normalizer_has_one_transport_neutral_owner() -> None:
    affinity = _parse(AFFINITY_PATH)
    websocket_events = _parse(WEBSOCKET_EVENTS_PATH)

    affinity_functions = {
        node.name for node in affinity.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    websocket_functions = {
        node.name for node in websocket_events.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "_normalize_session_id" in affinity_functions
    assert "_normalize_session_id" not in websocket_functions

    compatibility_imports = {
        alias.asname or alias.name
        for node in websocket_events.body
        if isinstance(node, ast.ImportFrom) and node.module == "app.modules.proxy._service.affinity"
        for alias in node.names
    }
    assert "_normalize_session_id" in compatibility_imports
