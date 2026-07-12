from __future__ import annotations

import logging
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlparse

from app.core.balancer.rendezvous_hash import select_node
from app.core.config.settings import Settings
from app.core.errors import OpenAIErrorEnvelope, openai_error
from app.core.utils.time import to_utc_naive, utcnow
from app.db.models import HttpBridgeSessionState
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _sticky_key_from_turn_state_header
from app.modules.proxy._service.support import (
    _header_value_case_insensitive,
    _HTTPBridgeSessionKey,
)
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeLookup
from app.modules.proxy.ring_membership import RingMembershipService

logger = logging.getLogger("app.modules.proxy.service")


class _BridgeRegistrationService(Protocol):
    _ring_membership: RingMembershipService | None


async def _http_bridge_should_wait_for_registration(
    service: _BridgeRegistrationService,
    key: _HTTPBridgeSessionKey,
    settings: Settings,
) -> bool:
    import app.core.startup as startup_module

    if startup_module._bridge_registration_complete:
        return False
    if key.strength != "hard":
        return False
    if _http_bridge_requires_cluster_registration(settings):
        return True
    if service._ring_membership is None:
        return False
    try:
        active_members = await service._ring_membership.list_active()
    except Exception:
        logger.debug("Skipping bridge registration gate because active ring lookup failed", exc_info=True)
        return False
    current_instance = settings.http_responses_session_bridge_instance_id
    return any(member != current_instance for member in active_members)


def _durable_bridge_lookup_active_owner(lookup: DurableBridgeLookup | None) -> str | None:
    if lookup is None or lookup.state == "closed":
        return None
    if lookup.owner_instance_id is None or lookup.lease_expires_at is None:
        return None
    if to_utc_naive(lookup.lease_expires_at) <= utcnow():
        return None
    return lookup.owner_instance_id


def _durable_bridge_lookup_allows_local_reuse(
    lookup: DurableBridgeLookup | None,
    *,
    current_instance: str,
) -> bool:
    owner_instance = _durable_bridge_lookup_active_owner(lookup)
    return owner_instance is None or owner_instance == current_instance


def _http_bridge_allow_durable_takeover(lookup: DurableBridgeLookup | None) -> bool:
    if _durable_bridge_lookup_active_owner(lookup) is None:
        return True
    return lookup is not None and lookup.state in {
        HttpBridgeSessionState.DRAINING,
        HttpBridgeSessionState.CLOSED,
    }


def _http_bridge_has_durable_recovery_anchor(
    *,
    previous_response_id: str | None,
    durable_lookup: DurableBridgeLookup | None,
) -> bool:
    if previous_response_id is not None:
        return True
    if durable_lookup is None or durable_lookup.latest_response_id is None:
        return False
    return durable_lookup.canonical_kind in {"turn_state_header", "session_header"}


def _http_bridge_can_local_recover_without_ring(
    *,
    key: _HTTPBridgeSessionKey,
    headers: Mapping[str, str],
    previous_response_id: str | None,
    durable_lookup: DurableBridgeLookup | None,
) -> bool:
    if _http_bridge_has_durable_recovery_anchor(
        previous_response_id=previous_response_id,
        durable_lookup=durable_lookup,
    ):
        return True
    return (
        key.affinity_kind == "session_header"
        and previous_response_id is None
        and _sticky_key_from_turn_state_header(headers) is None
    )


def _http_bridge_can_single_instance_owner_takeover_without_anchor(
    *,
    key: _HTTPBridgeSessionKey,
    owner_instance: str | None,
    current_instance: str,
    ring: tuple[str, ...],
) -> bool:
    return (
        key.strength == "hard"
        and owner_instance is not None
        and owner_instance != current_instance
        and ring == (current_instance,)
        and owner_instance not in ring
    )


def _http_bridge_can_single_instance_prompt_cache_takeover_without_anchor(
    *,
    key: _HTTPBridgeSessionKey,
    owner_instance: str | None,
    current_instance: str,
    ring: tuple[str, ...],
) -> bool:
    return (
        key.affinity_kind == "prompt_cache"
        and owner_instance is not None
        and owner_instance != current_instance
        and ring == (current_instance,)
        and owner_instance not in ring
    )


def _http_bridge_can_recover_during_drain(
    *,
    key: _HTTPBridgeSessionKey,
    headers: Mapping[str, str],
    previous_response_id: str | None,
    durable_lookup: DurableBridgeLookup | None,
) -> bool:
    del key, headers
    return _http_bridge_has_durable_recovery_anchor(
        previous_response_id=previous_response_id,
        durable_lookup=durable_lookup,
    )


def _forwarded_http_bridge_session_key(
    headers: Mapping[str, str],
    api_key: ApiKeyData | None,
    *,
    forwarded_affinity_kind: str | None = None,
    forwarded_affinity_key: str | None = None,
) -> _HTTPBridgeSessionKey | None:
    affinity_kind = forwarded_affinity_kind or _header_value_case_insensitive(
        headers,
        "x-codex-bridge-affinity-kind",
    )
    affinity_key = forwarded_affinity_key or _header_value_case_insensitive(
        headers,
        "x-codex-bridge-affinity-key",
    )
    if affinity_kind is None or affinity_key is None:
        return None
    strength = "hard" if affinity_kind in {"turn_state_header", "session_header"} else "soft"
    return _HTTPBridgeSessionKey(
        affinity_kind=affinity_kind,
        affinity_key=affinity_key,
        api_key_id=api_key.id if api_key is not None else None,
        strength=strength,
    )


def _http_bridge_requires_cluster_registration(settings: Settings) -> bool:
    if len(settings.http_responses_session_bridge_instance_ring) > 1:
        return True
    advertise_base_url = settings.http_responses_session_bridge_advertise_base_url
    if advertise_base_url is None:
        return False
    hostname = urlparse(advertise_base_url).hostname
    if hostname is None:
        return False
    try:
        parsed_ip = ip_address(hostname)
    except ValueError:
        return True
    return not parsed_ip.is_loopback


def _http_bridge_continuity_lost_error_envelope() -> OpenAIErrorEnvelope:
    return openai_error(
        "stream_incomplete",
        "Upstream websocket closed before response.completed",
        error_type="server_error",
    )


def _http_bridge_owner_lookup_unavailable_error_envelope() -> OpenAIErrorEnvelope:
    return openai_error(
        "upstream_unavailable",
        "HTTP bridge owner metadata unavailable; retry later.",
        error_type="server_error",
    )


def _http_bridge_busy_session_error_envelope() -> OpenAIErrorEnvelope:
    return openai_error(
        "upstream_unavailable",
        "HTTP bridge session is busy with another continuity request; retry later.",
        error_type="server_error",
    )


def _normalized_http_bridge_instance_ring(settings: Settings) -> tuple[str, tuple[str, ...]]:
    instance_id = settings.http_responses_session_bridge_instance_id.strip() or "codex-lb"
    ring_entries = [entry.strip() for entry in settings.http_responses_session_bridge_instance_ring if entry.strip()]
    if not ring_entries:
        ring_entries.append(instance_id)
    return instance_id, tuple(sorted(set(ring_entries)))


async def _active_http_bridge_instance_ring(
    settings: Settings,
    ring_membership: RingMembershipService | None,
) -> tuple[str, tuple[str, ...]]:
    instance_id, static_ring = _normalized_http_bridge_instance_ring(settings)
    if ring_membership is None:
        return instance_id, static_ring
    try:
        active_members = await ring_membership.list_active(require_endpoint=True)
    except Exception:
        logger.warning("Bridge ring lookup failed - refusing to fall back to static ring", exc_info=True)
        raise
    if not active_members:
        return instance_id, (instance_id,)
    normalized_members = tuple(
        sorted({member.strip() for member in active_members if isinstance(member, str) and member.strip()})
    )
    return (instance_id, normalized_members) if normalized_members else (instance_id, static_ring)


async def _http_bridge_owner_instance(
    key: _HTTPBridgeSessionKey,
    settings: Settings,
    ring_membership: RingMembershipService | None = None,
) -> str | None:
    instance_id, ring = await _active_http_bridge_instance_ring(settings, ring_membership)
    if len(ring) <= 1:
        return instance_id
    hash_input = f"{key.affinity_kind}:{key.affinity_key}:{key.api_key_id or ''}"
    return select_node(hash_input, ring)


def _http_bridge_owner_check_required(
    key: _HTTPBridgeSessionKey,
    *,
    gateway_safe_mode: bool,
) -> bool:
    return key.strength == "hard" or (gateway_safe_mode and key.affinity_kind == "sticky_thread")
