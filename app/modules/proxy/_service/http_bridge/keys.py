from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from app.core.openai.requests import ResponsesRequest
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import (
    _sticky_key_from_session_header,
    _sticky_key_from_turn_state_header,
)
from app.modules.proxy._service.http_bridge.ownership import (
    _forwarded_http_bridge_session_key,
)
from app.modules.proxy._service.support import (
    _HTTP_BRIDGE_BUSY_PARALLEL_MARKER,
    _HTTP_BRIDGE_SOFT_SHARD_MARKER,
    _AffinityPolicy,
    _HTTPBridgeSession,
    _HTTPBridgeSessionKey,
)


def _make_http_bridge_session_key(
    payload: ResponsesRequest,
    *,
    headers: Mapping[str, str],
    affinity: _AffinityPolicy,
    api_key: ApiKeyData | None,
    request_id: str,
    allow_forwarded_affinity_headers: bool = False,
    forwarded_affinity_kind: str | None = None,
    forwarded_affinity_key: str | None = None,
) -> _HTTPBridgeSessionKey:
    forwarded_key = (
        _forwarded_http_bridge_session_key(
            headers,
            api_key,
            forwarded_affinity_kind=forwarded_affinity_kind,
            forwarded_affinity_key=forwarded_affinity_key,
        )
        if allow_forwarded_affinity_headers
        else None
    )
    if forwarded_key is not None:
        return forwarded_key
    turn_state_key = _sticky_key_from_turn_state_header(headers)
    if turn_state_key is not None:
        affinity_key = turn_state_key
        affinity_kind = "turn_state_header"
        strength: Literal["hard", "soft"] = "hard"
    else:
        session_key = _sticky_key_from_session_header(headers)
        if session_key is not None:
            affinity_key = session_key
            affinity_kind = "session_header"
            strength = "hard"
        else:
            affinity_key = affinity.key or request_id
            affinity_kind = affinity.kind.value if affinity.kind is not None else "request"
            strength = "soft"
    return _HTTPBridgeSessionKey(
        affinity_kind=affinity_kind,
        affinity_key=affinity_key,
        api_key_id=api_key.id if api_key is not None else None,
        strength=strength,
    )


def _http_bridge_turn_state_alias_key(turn_state: str, api_key_id: str | None) -> tuple[str, str | None]:
    return (turn_state, api_key_id)


def _http_bridge_previous_response_alias_key(response_id: str, api_key_id: str | None) -> tuple[str, str | None]:
    return (response_id.strip(), api_key_id)


def _http_bridge_soft_sharding_allowed(
    key: _HTTPBridgeSessionKey,
    *,
    incoming_turn_state: str | None,
    previous_response_id: str | None,
    forwarded_request: bool,
) -> bool:
    return (
        key.affinity_kind == "prompt_cache"
        and key.strength != "hard"
        and incoming_turn_state is None
        and previous_response_id is None
        and not forwarded_request
        and _http_bridge_soft_shard_index(key) == 0
    )


def _http_bridge_soft_shard_key(base_key: _HTTPBridgeSessionKey, shard_index: int) -> _HTTPBridgeSessionKey:
    if shard_index <= 0:
        return base_key
    return _HTTPBridgeSessionKey(
        affinity_kind=base_key.affinity_kind,
        affinity_key=f"{base_key.affinity_key}{_HTTP_BRIDGE_SOFT_SHARD_MARKER}{shard_index}",
        api_key_id=base_key.api_key_id,
        strength=base_key.strength,
    )


def _http_bridge_busy_parallel_key(base_key: _HTTPBridgeSessionKey, shard_index: int) -> _HTTPBridgeSessionKey:
    if shard_index <= 0:
        return base_key
    marker_index = base_key.affinity_key.rfind(_HTTP_BRIDGE_BUSY_PARALLEL_MARKER)
    base_affinity_key = base_key.affinity_key if marker_index < 0 else base_key.affinity_key[:marker_index]
    return _HTTPBridgeSessionKey(
        affinity_kind=base_key.affinity_kind,
        affinity_key=f"{base_affinity_key}{_HTTP_BRIDGE_BUSY_PARALLEL_MARKER}{shard_index}",
        api_key_id=base_key.api_key_id,
        strength=base_key.strength,
    )


def _http_bridge_parallel_batch_key(
    session: _HTTPBridgeSession,
    *,
    batch_window_seconds: float,
) -> tuple[str, str | None, int]:
    bucket = int(session.created_at // batch_window_seconds)
    return (session.request_model or "unknown", session.key.api_key_id, bucket)


def _http_bridge_busy_parallel_index(key: _HTTPBridgeSessionKey) -> int:
    marker_index = key.affinity_key.rfind(_HTTP_BRIDGE_BUSY_PARALLEL_MARKER)
    if marker_index < 0:
        return 0
    raw_index = key.affinity_key[marker_index + len(_HTTP_BRIDGE_BUSY_PARALLEL_MARKER) :]
    try:
        return int(raw_index)
    except ValueError:
        return 0


def _http_bridge_soft_shard_index(key: _HTTPBridgeSessionKey) -> int:
    marker_index = key.affinity_key.rfind(_HTTP_BRIDGE_SOFT_SHARD_MARKER)
    if marker_index < 0:
        return 0
    raw_index = key.affinity_key[marker_index + len(_HTTP_BRIDGE_SOFT_SHARD_MARKER) :]
    try:
        return int(raw_index)
    except ValueError:
        return 0


def _http_bridge_family_affinity_key(key: _HTTPBridgeSessionKey) -> str:
    cut_indexes = [
        index
        for index in (
            key.affinity_key.rfind(_HTTP_BRIDGE_SOFT_SHARD_MARKER),
            key.affinity_key.rfind(_HTTP_BRIDGE_BUSY_PARALLEL_MARKER),
        )
        if index >= 0
    ]
    if not cut_indexes:
        return key.affinity_key
    return key.affinity_key[: min(cut_indexes)]


def _http_bridge_key_strength(key: _HTTPBridgeSessionKey) -> str:
    return key.strength or "soft"
