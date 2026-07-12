from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Literal, Protocol

from app.core.account_groups import account_builtin_group_names
from app.core.openai.model_registry import get_model_registry
from app.core.resilience.overload import is_local_overload_error_code
from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account, AccountStatus
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.support import (
    _HARD_HTTP_BRIDGE_AFFINITY_KINDS,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
)
from app.modules.proxy.load_balancer import AccountSelection

logger = logging.getLogger("app.modules.proxy.service")

_HTTP_BRIDGE_ACCOUNT_SELECTION_RECLAIM_MESSAGES = frozenset({"No available accounts"})


class _ModelPlanRegistry(Protocol):
    def plan_types_for_model(self, slug: str) -> frozenset[str] | None: ...


def _should_reclaim_http_bridge_idle_session_for_selection(selection: AccountSelection) -> bool:
    if is_local_overload_error_code(selection.error_code):
        return True
    if selection.error_code not in {None, "no_accounts"}:
        return False
    return selection.error_message in _HTTP_BRIDGE_ACCOUNT_SELECTION_RECLAIM_MESSAGES


def _http_bridge_pressure_evictable_prompt_cache_session(
    session: "_HTTPBridgeSession",
    *,
    pending_count: int,
    now: float,
    min_idle_seconds: float,
) -> bool:
    if session.closed or pending_count > 0 or session.submit_lease_count > 0:
        return False
    if session.client_kind == "interactive":
        return False
    if session.key.affinity_kind != "prompt_cache" or session.key.strength != "soft":
        return False
    if session.upstream_control.reconnect_requested:
        return False
    if now - session.last_used_at < min_idle_seconds:
        return False
    return (
        session.codex_session
        or bool(session.previous_response_ids)
        or bool(session.downstream_turn_state_aliases)
        or session.downstream_turn_state is not None
        or session.last_completed_response_id is not None
    )


def _http_bridge_client_kind(
    headers: Mapping[str, str],
    *,
    key: "_HTTPBridgeSessionKey",
) -> Literal["batch", "interactive", "unknown"]:
    header_text = " ".join(
        value
        for name, value in headers.items()
        if name.lower()
        in {
            "user-agent",
            "x-codex-client",
            "x-codex-client-kind",
            "x-codex-app",
            "x-codex-source",
            "x-client-name",
        }
    ).lower()
    if any(marker in header_text for marker in ("vscode", "vs-code", "visual studio code", "code-server")):
        return "interactive"
    if key.affinity_kind in _HARD_HTTP_BRIDGE_AFFINITY_KINDS:
        return "interactive"
    if any(marker in header_text for marker in ("codex-exec", "codex exec", "codex_cli", "codex-cli")):
        return "batch"
    if key.affinity_kind == "prompt_cache" and key.strength == "soft":
        return "batch"
    return "unknown"


def _http_bridge_client_eviction_priority(client_kind: str) -> int:
    if client_kind == "batch":
        return 0
    if client_kind == "unknown":
        return 1
    return 2


def _account_status_value(status: AccountStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if isinstance(status, AccountStatus) else str(status)


def _http_bridge_account_label(account: Account) -> str:
    email = (account.email or "").strip()
    return email or account.id


def _http_bridge_session_allows_api_key(session: "_HTTPBridgeSession", api_key: ApiKeyData | None) -> bool:
    if api_key is None:
        return True
    return _api_key_allows_account(api_key, session.account)


def _api_key_allows_account(api_key: ApiKeyData, account: Account) -> bool:
    if api_key.account_assignment_scope_enabled and account.id not in api_key.assigned_account_ids:
        return False
    if not api_key.allowed_groups:
        return True
    return bool(_account_group_names(account) & set(api_key.allowed_groups))


def _account_group_names(account: Account) -> set[str]:
    memberships = account.__dict__.get("group_memberships", [])
    groups = {
        membership.group_name.strip().lower()
        for membership in memberships
        if getattr(membership, "group_name", None) and membership.group_name.strip()
    }
    groups.update(account_builtin_group_names(account))
    return groups


def _http_bridge_session_reusable_for_request(
    *,
    session: "_HTTPBridgeSession",
    key: "_HTTPBridgeSessionKey",
    incoming_turn_state: str | None,
    previous_response_id: str | None,
    request_model: str | None,
    model_registry: _ModelPlanRegistry | None = None,
) -> bool:
    if not _http_bridge_session_account_supports_request_model(
        session,
        request_model,
        model_registry=model_registry,
    ):
        return False
    if key.affinity_kind != "prompt_cache":
        return True
    if incoming_turn_state is not None:
        return True
    if previous_response_id is not None:
        return True
    return not session.codex_session


def _http_bridge_session_account_supports_request_model(
    session: "_HTTPBridgeSession",
    request_model: str | None,
    *,
    model_registry: _ModelPlanRegistry | None = None,
) -> bool:
    return _account_supports_http_bridge_request_model(
        session.account,
        request_model,
        model_registry=model_registry,
    )


def _account_supports_http_bridge_request_model(
    account: Account,
    request_model: str | None,
    *,
    model_registry: _ModelPlanRegistry | None = None,
) -> bool:
    if request_model is None:
        return True
    provider_kind = getattr(account, "provider_kind", None)
    if provider_kind == ACCOUNT_PROVIDER_API_KEY:
        supported_models = _supported_models_for_account(account)
        return supported_models is None or request_model in supported_models
    plan_type = getattr(account, "plan_type", None)
    if plan_type is None:
        return True
    registry = model_registry if model_registry is not None else get_model_registry()
    allowed_plans = registry.plan_types_for_model(request_model)
    return allowed_plans is not None and plan_type in allowed_plans


def _supported_models_for_account(account: Account) -> set[str] | None:
    raw = account.supported_models_json
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid supported_models_json for account_id=%s", account.id)
        return None
    if not isinstance(payload, list):
        return None
    supported = {item.strip() for item in payload if isinstance(item, str) and item.strip()}
    return supported or set()


def _http_bridge_session_matches_preferred_account(
    *,
    session: "_HTTPBridgeSession",
    previous_response_id: str | None,
    preferred_account_id: str | None,
) -> bool:
    if previous_response_id is None or preferred_account_id is None:
        return True
    return session.account.id == preferred_account_id


def _http_bridge_soft_prompt_cache_busy_parallel_allowed(
    *,
    key: "_HTTPBridgeSessionKey",
    session: "_HTTPBridgeSession",
    incoming_turn_state: str | None,
    previous_response_id: str | None,
) -> bool:
    if key.affinity_kind != "prompt_cache":
        return False
    if key.strength == "soft":
        return True
    return (
        session.codex_session
        or bool(session.previous_response_ids)
        or bool(session.downstream_turn_state_aliases)
        or session.downstream_turn_state is not None
        or session.last_completed_response_id is not None
        or incoming_turn_state is not None
        or previous_response_id is not None
    )


def _http_bridge_eviction_priority(session: _HTTPBridgeSession) -> tuple[int, float]:
    return (0 if not session.codex_session else 1, session.last_used_at)
