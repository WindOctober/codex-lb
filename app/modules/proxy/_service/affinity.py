from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from uuid import uuid4

from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
from app.core.config.settings import Settings
from app.core.openai.requests import ResponsesCompactRequest, ResponsesRequest
from app.core.types import JsonValue
from app.core.utils.json_guards import is_json_mapping
from app.db.models import StickySessionKind
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.support import _AffinityPolicy, _HTTPBridgeSession


def _normalize_session_id(session_id: str | None) -> str | None:
    if not isinstance(session_id, str):
        return None
    stripped = session_id.strip()
    return stripped or None


def _prompt_cache_key_from_request_model(payload: ResponsesRequest | ResponsesCompactRequest) -> str | None:
    typed_value = getattr(payload, "prompt_cache_key", None)
    if isinstance(typed_value, str) and typed_value:
        return typed_value
    if not payload.model_extra:
        return None
    extra_value = payload.model_extra.get("prompt_cache_key")
    if isinstance(extra_value, str) and extra_value:
        return extra_value
    camel_value = payload.model_extra.get("promptCacheKey")
    if isinstance(camel_value, str) and camel_value:
        return camel_value
    return None


def _extract_model_class(model: str) -> str:
    if "codex" in model:
        return "codex"
    if "mini" in model:
        return "mini"
    return "std"


def _derive_prompt_cache_key(
    payload: ResponsesRequest | ResponsesCompactRequest,
    api_key: ApiKeyData | None,
) -> str:
    parts: list[str] = []
    model = getattr(payload, "model", None)
    model_class = _extract_model_class(model) if isinstance(model, str) and model else None

    if api_key is not None:
        parts.append(api_key.id[:12])

    instructions = getattr(payload, "instructions", None)
    if isinstance(instructions, str) and instructions:
        parts.append(sha256(instructions[:512].encode()).hexdigest()[:12])

    first_user_text = _extract_first_user_input(payload)
    if first_user_text:
        parts.append(sha256(first_user_text[:512].encode()).hexdigest()[:12])

    if not parts:
        random_suffix = uuid4().hex[:24]
        return f"{model_class}-{random_suffix}" if model_class is not None else random_suffix

    return "-".join([model_class, *parts]) if model_class is not None else "-".join(parts)


def _resolve_prompt_cache_key(
    payload: ResponsesRequest | ResponsesCompactRequest,
    *,
    openai_cache_affinity: bool,
    api_key: ApiKeyData | None,
    settings: Settings,
) -> tuple[str | None, str]:
    cache_key = _prompt_cache_key_from_request_model(payload)
    if isinstance(cache_key, str):
        stripped = cache_key.strip()
        if stripped:
            if stripped != cache_key:
                payload.prompt_cache_key = stripped
            return stripped, "payload"
    if not openai_cache_affinity or not settings.openai_prompt_cache_key_derivation_enabled:
        return None, "none"
    cache_key = _derive_prompt_cache_key(payload, api_key)
    payload.prompt_cache_key = cache_key
    return cache_key, "derived"


def _sticky_key_for_responses_request(
    payload: ResponsesRequest,
    headers: Mapping[str, str],
    *,
    codex_session_affinity: bool,
    openai_cache_affinity: bool,
    openai_cache_affinity_max_age_seconds: int,
    sticky_threads_enabled: bool,
    settings: Settings,
    api_key: ApiKeyData | None = None,
) -> _AffinityPolicy:
    cache_key, _ = _resolve_prompt_cache_key(
        payload,
        openai_cache_affinity=openai_cache_affinity,
        api_key=api_key,
        settings=settings,
    )
    turn_state_key = _sticky_key_from_turn_state_header(headers)
    if turn_state_key:
        return _AffinityPolicy(key=turn_state_key, kind=StickySessionKind.CODEX_SESSION)
    if codex_session_affinity:
        session_key = _sticky_key_from_session_header(headers)
        if session_key:
            return _AffinityPolicy(key=session_key, kind=StickySessionKind.CODEX_SESSION)
    if openai_cache_affinity:
        return _AffinityPolicy(
            key=cache_key,
            kind=StickySessionKind.PROMPT_CACHE,
            max_age_seconds=openai_cache_affinity_max_age_seconds,
        )
    if sticky_threads_enabled:
        return _AffinityPolicy(
            key=cache_key,
            kind=StickySessionKind.STICKY_THREAD,
            reallocate_sticky=True,
        )
    return _AffinityPolicy()


def _sticky_key_for_compact_request(
    payload: ResponsesCompactRequest,
    headers: Mapping[str, str],
    *,
    codex_session_affinity: bool,
    openai_cache_affinity: bool,
    openai_cache_affinity_max_age_seconds: int,
    sticky_threads_enabled: bool,
    settings: Settings,
    api_key: ApiKeyData | None = None,
) -> _AffinityPolicy:
    cache_key, _ = _resolve_prompt_cache_key(
        payload,
        openai_cache_affinity=openai_cache_affinity,
        api_key=api_key,
        settings=settings,
    )
    if codex_session_affinity:
        session_key = _sticky_key_from_session_header(headers)
        if session_key:
            return _AffinityPolicy(key=session_key, kind=StickySessionKind.CODEX_SESSION)
    if openai_cache_affinity:
        return _AffinityPolicy(
            key=cache_key,
            kind=StickySessionKind.PROMPT_CACHE,
            max_age_seconds=openai_cache_affinity_max_age_seconds,
        )
    if sticky_threads_enabled:
        return _AffinityPolicy(
            key=cache_key,
            kind=StickySessionKind.STICKY_THREAD,
            reallocate_sticky=True,
        )
    return _AffinityPolicy()


def _extract_first_user_input(payload: ResponsesRequest | ResponsesCompactRequest) -> str | None:
    input_value = getattr(payload, "input", None)
    if isinstance(input_value, str):
        return input_value[:512]
    if not isinstance(input_value, list):
        return None
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content[:512]
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        return text[:512]
        return json.dumps(item, sort_keys=True, ensure_ascii=False)[:512]
    return None


def _sticky_key_from_session_header(headers: Mapping[str, str]) -> str | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    for key in ("session_id", "x-codex-session-id", "x-codex-conversation-id"):
        value = normalized.get(key)
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _sticky_key_from_turn_state_header(headers: Mapping[str, str]) -> str | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    value = normalized.get("x-codex-turn-state")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _owner_lookup_session_id_from_headers(headers: Mapping[str, str]) -> str | None:
    turn_state = _sticky_key_from_turn_state_header(headers)
    if turn_state is not None:
        return turn_state
    return _sticky_key_from_session_header(headers)


def _response_create_client_metadata(
    payload: Mapping[str, JsonValue],
    *,
    headers: Mapping[str, str],
) -> Mapping[str, JsonValue] | None:
    raw_value = payload.get("client_metadata")
    client_metadata: dict[str, JsonValue] = {}
    if is_json_mapping(raw_value):
        for key, value in raw_value.items():
            if isinstance(key, str):
                client_metadata[key] = value

    normalized_headers = {key.lower(): value for key, value in headers.items()}
    turn_metadata = normalized_headers.get("x-codex-turn-metadata")
    if isinstance(turn_metadata, str) and turn_metadata.strip():
        client_metadata.setdefault("x-codex-turn-metadata", turn_metadata)

    return client_metadata or None


def ensure_downstream_turn_state(headers: Mapping[str, str]) -> str:
    existing = _sticky_key_from_turn_state_header(headers)
    return existing if existing is not None else f"turn_{uuid4().hex}"


def ensure_http_downstream_turn_state(headers: Mapping[str, str]) -> str:
    existing = _sticky_key_from_turn_state_header(headers)
    return existing if existing is not None else f"http_turn_{uuid4().hex}"


def build_downstream_turn_state_accept_headers(turn_state: str) -> list[tuple[bytes, bytes]]:
    return [(b"x-codex-turn-state", turn_state.encode("utf-8"))]


def build_downstream_turn_state_response_headers(turn_state: str) -> dict[str, str]:
    return {"x-codex-turn-state": turn_state}


def _upstream_turn_state_from_socket(upstream: UpstreamResponsesWebSocket | None) -> str | None:
    if upstream is None:
        return None
    getter = getattr(upstream, "response_header", None)
    if not callable(getter):
        return None
    value = getter("x-codex-turn-state")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _headers_with_turn_state(headers: Mapping[str, str], turn_state: str | None) -> dict[str, str]:
    forwarded = dict(headers)
    if turn_state:
        forwarded["x-codex-turn-state"] = turn_state
    return forwarded


def _preferred_http_bridge_reconnect_turn_state(session: _HTTPBridgeSession) -> str | None:
    if (
        session.codex_session
        and session.downstream_turn_state is not None
        and session.affinity.kind == StickySessionKind.CODEX_SESSION
        and session.affinity.key == session.downstream_turn_state
    ):
        return session.downstream_turn_state
    return session.upstream_turn_state
