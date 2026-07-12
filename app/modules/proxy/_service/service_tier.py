from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeVar, cast

from app.core.openai.models import CompactResponsePayload, OpenAIResponsePayload
from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest
from app.core.types import JsonObject, JsonValue
from app.db.models import Account
from app.modules.api_keys.service import ApiKeyData

_ServiceTierPayloadT = TypeVar("_ServiceTierPayloadT", ResponsesRequest, ResponsesCompactRequest)


def _service_tier_from_compact_payload(payload: ResponsesCompactRequest) -> str | None:
    return _normalize_service_tier_value(payload.service_tier)


def _account_uses_fast_service_tier(account: Account, *, api_key: ApiKeyData | None) -> bool:
    if api_key is not None and api_key.enforced_service_tier is not None:
        return False
    return bool(getattr(account, "fast_service_tier_enabled", False))


def _should_promote_service_tier_to_priority(value: JsonValue) -> bool:
    normalized = _normalize_service_tier_value(value)
    return normalized in (None, "default", "priority")


def _payload_with_account_service_tier(
    payload: _ServiceTierPayloadT,
    account: Account,
    *,
    api_key: ApiKeyData | None,
) -> _ServiceTierPayloadT:
    if not _account_uses_fast_service_tier(account, api_key=api_key):
        return payload
    if not _should_promote_service_tier_to_priority(payload.service_tier):
        return payload
    if payload.service_tier == "priority":
        return payload
    return payload.model_copy(update={"service_tier": "priority"})


def _http_bridge_text_with_account_service_tier(
    text_data: str,
    account: Account,
    *,
    api_key: ApiKeyData | None,
) -> tuple[str, str | None]:
    if not _account_uses_fast_service_tier(account, api_key=api_key):
        return text_data, None
    try:
        payload = json.loads(text_data)
    except json.JSONDecodeError:
        return text_data, None
    if not isinstance(payload, dict):
        return text_data, None
    if not _should_promote_service_tier_to_priority(cast(JsonObject, payload).get("service_tier")):
        return text_data, None
    if payload.get("service_tier") == "priority":
        return text_data, "priority"
    payload["service_tier"] = "priority"
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")), "priority"


def _service_tier_from_response(
    response: OpenAIResponsePayload | CompactResponsePayload | None,
) -> str | None:
    if response is None:
        return None
    extra = response.model_extra
    if not isinstance(extra, Mapping):
        return None
    return _normalize_service_tier_value(extra.get("service_tier"))


def _service_tier_from_event_payload(payload: dict[str, JsonValue] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    return _normalize_service_tier_value(response.get("service_tier"))


def _effective_service_tier(requested_service_tier: str | None, actual_service_tier: str | None) -> str | None:
    if isinstance(actual_service_tier, str):
        return actual_service_tier
    if isinstance(requested_service_tier, str):
        return requested_service_tier
    return None


def _normalize_service_tier_value(value: JsonValue) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    normalized = stripped.lower()
    if normalized == "auto":
        return "default"
    if normalized == "fast":
        return "priority"
    return stripped
