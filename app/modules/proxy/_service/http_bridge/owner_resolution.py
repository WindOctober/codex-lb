from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import Settings
from app.core.errors import openai_error
from app.core.metrics.prometheus import (
    PROMETHEUS_AVAILABLE,
    bridge_durable_recover_total,
    bridge_local_rebind_total,
    bridge_owner_mismatch_total,
    bridge_prompt_cache_locality_miss_total,
    bridge_soft_local_rebind_total,
)
from app.modules.proxy._service.affinity import _extract_model_class
from app.modules.proxy._service.http_bridge.keys import _http_bridge_key_strength
from app.modules.proxy._service.http_bridge.ownership import (
    _durable_bridge_lookup_active_owner,
    _http_bridge_can_local_recover_without_ring,
    _http_bridge_can_single_instance_owner_takeover_without_anchor,
    _http_bridge_can_single_instance_prompt_cache_takeover_without_anchor,
    _http_bridge_has_durable_recovery_anchor,
    _http_bridge_owner_check_required,
    _http_bridge_owner_lookup_unavailable_error_envelope,
)
from app.modules.proxy._service.observability import _log_http_bridge_event
from app.modules.proxy._service.support import (
    _HTTPBridgeOwnerForward,
    _HTTPBridgeSessionKey,
)
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeLookup
from app.modules.proxy.ring_membership import RingMembershipService

logger = logging.getLogger("app.modules.proxy.service")


@dataclass(frozen=True, slots=True)
class _HTTPBridgeOwnerResolution:
    owner_check_required: bool
    owner_forward: _HTTPBridgeOwnerForward | None
    force_durable_takeover: bool


class _HTTPBridgeOwnerResolutionService(Protocol):
    _ring_membership: RingMembershipService | None

    async def _http_bridge_owner_instance_compatible(
        self,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
    ) -> str | None: ...

    async def _active_http_bridge_instance_ring_compatible(
        self,
        settings: Settings,
    ) -> tuple[str, tuple[str, ...]]: ...

    @staticmethod
    def _record_http_bridge_continuity_fail_closed(
        *,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None,
        upstream_error_code: str | None = None,
    ) -> None: ...


class _HTTPBridgeOwnerResolutionMixin:
    async def _resolve_http_bridge_owner(
        self: _HTTPBridgeOwnerResolutionService,
        *,
        key: _HTTPBridgeSessionKey,
        settings: Settings,
        headers: dict[str, str],
        request_model: str | None,
        previous_response_id: str | None,
        durable_lookup: DurableBridgeLookup | None,
        gateway_safe_mode: bool,
        allow_previous_response_recovery_rebind: bool,
        allow_bootstrap_owner_rebind: bool,
        allow_forward_to_owner: bool,
        forwarded_request: bool,
        incoming_turn_state: str | None,
        incoming_session_key: str | None,
    ) -> _HTTPBridgeOwnerResolution:
        owner_forward: _HTTPBridgeOwnerForward | None = None
        force_durable_takeover = False
        owner_check_required = _http_bridge_owner_check_required(
            key,
            gateway_safe_mode=gateway_safe_mode,
        )
        if owner_check_required or key.affinity_kind == "prompt_cache":
            owner_instance = _durable_bridge_lookup_active_owner(durable_lookup)
            hard_continuity_lookup = owner_check_required or previous_response_id is not None
            ring_lookup_failed = False
            if owner_instance is None:
                try:
                    owner_instance = await self._http_bridge_owner_instance_compatible(key, settings)
                except Exception as exc:
                    ring_lookup_failed = True
                    if hard_continuity_lookup:
                        self._record_http_bridge_continuity_fail_closed(
                            reason="owner_metadata_unavailable",
                            previous_response_id=previous_response_id,
                            session_id=incoming_turn_state or incoming_session_key,
                            upstream_error_code="owner_lookup_failed",
                        )
                        raise ProxyResponseError(
                            502,
                            _http_bridge_owner_lookup_unavailable_error_envelope(),
                        ) from exc
                    if _http_bridge_can_local_recover_without_ring(
                        key=key,
                        headers=headers,
                        previous_response_id=previous_response_id,
                        durable_lookup=durable_lookup,
                    ):
                        logger.warning(
                            "Bridge owner lookup failed; allowing local recovery path",
                            exc_info=True,
                        )
                        owner_instance = settings.http_responses_session_bridge_instance_id
                    else:
                        raise
            try:
                current_instance, ring = await self._active_http_bridge_instance_ring_compatible(settings)
            except Exception as exc:
                if hard_continuity_lookup:
                    self._record_http_bridge_continuity_fail_closed(
                        reason="owner_metadata_unavailable",
                        previous_response_id=previous_response_id,
                        session_id=incoming_turn_state or incoming_session_key,
                        upstream_error_code="ring_lookup_failed",
                    )
                    raise ProxyResponseError(
                        502,
                        _http_bridge_owner_lookup_unavailable_error_envelope(),
                    ) from exc
                if ring_lookup_failed or _http_bridge_can_local_recover_without_ring(
                    key=key,
                    headers=headers,
                    previous_response_id=previous_response_id,
                    durable_lookup=durable_lookup,
                ):
                    logger.warning("Bridge ring lookup failed; falling back to local recovery ring", exc_info=True)
                    current_instance = settings.http_responses_session_bridge_instance_id
                    ring = (current_instance,)
                else:
                    raise
            owner_mismatch = owner_instance is not None and owner_instance != current_instance
            if owner_mismatch and (len(ring) > 1 or durable_lookup is not None):
                if PROMETHEUS_AVAILABLE and bridge_owner_mismatch_total is not None:
                    bridge_owner_mismatch_total.labels(strength=_http_bridge_key_strength(key)).inc()
                if (
                    owner_check_required
                    and not (previous_response_id is not None and allow_previous_response_recovery_rebind)
                    and not allow_bootstrap_owner_rebind
                ):
                    _log_http_bridge_event(
                        "owner_mismatch",
                        key,
                        account_id=None,
                        model=request_model,
                        detail=(
                            f"expected_instance={owner_instance}, current_instance={current_instance}, outcome=forward"
                        ),
                        cache_key_family=key.affinity_kind,
                        model_class=_extract_model_class(request_model) if request_model else None,
                        owner_check_applied=True,
                    )
                    if allow_forward_to_owner:
                        if forwarded_request:
                            _log_http_bridge_event(
                                "owner_mismatch_forward_loop",
                                key,
                                account_id=None,
                                model=request_model,
                                detail=(
                                    "expected_instance="
                                    f"{owner_instance}, current_instance={current_instance}, "
                                    "outcome=forward_loop_prevented"
                                ),
                                cache_key_family=key.affinity_kind,
                                model_class=_extract_model_class(request_model) if request_model else None,
                                owner_check_applied=True,
                            )
                            raise ProxyResponseError(
                                503,
                                openai_error(
                                    "bridge_forward_loop_prevented",
                                    (
                                        "HTTP bridge request was forwarded back to a non-owner instance; "
                                        "refusing takeover to avoid a forward loop"
                                    ),
                                    error_type="server_error",
                                ),
                            )
                        elif self._ring_membership is None:
                            if _http_bridge_has_durable_recovery_anchor(
                                previous_response_id=previous_response_id,
                                durable_lookup=durable_lookup,
                            ):
                                if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                                    bridge_durable_recover_total.labels(path="owner_missing").inc()
                                _log_http_bridge_event(
                                    "owner_mismatch_local_recover",
                                    key,
                                    account_id=None,
                                    model=request_model,
                                    detail=(
                                        "expected_instance="
                                        f"{owner_instance}, current_instance={current_instance}, "
                                        "outcome=local_recover_no_ring"
                                    ),
                                    cache_key_family=key.affinity_kind,
                                    model_class=_extract_model_class(request_model) if request_model else None,
                                    owner_check_applied=True,
                                )
                                force_durable_takeover = True
                            elif _http_bridge_can_single_instance_owner_takeover_without_anchor(
                                key=key,
                                owner_instance=owner_instance,
                                current_instance=current_instance,
                                ring=ring,
                            ):
                                if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                                    bridge_durable_recover_total.labels(path="restart_takeover").inc()
                                _log_http_bridge_event(
                                    "owner_mismatch_local_recover",
                                    key,
                                    account_id=None,
                                    model=request_model,
                                    detail=(
                                        "expected_instance="
                                        f"{owner_instance}, current_instance={current_instance}, "
                                        "outcome=single_instance_takeover_no_anchor"
                                    ),
                                    cache_key_family=key.affinity_kind,
                                    model_class=_extract_model_class(request_model) if request_model else None,
                                    owner_check_applied=True,
                                )
                                force_durable_takeover = True
                            else:
                                _log_http_bridge_event(
                                    "owner_mismatch_local_recover",
                                    key,
                                    account_id=None,
                                    model=request_model,
                                    detail=(
                                        "expected_instance="
                                        f"{owner_instance}, current_instance={current_instance}, "
                                        "outcome=local_recover_no_ring"
                                    ),
                                    cache_key_family=key.affinity_kind,
                                    model_class=_extract_model_class(request_model) if request_model else None,
                                    owner_check_applied=True,
                                )
                                force_durable_takeover = True
                        else:
                            assert owner_instance is not None
                            owner_endpoint = await self._ring_membership.resolve_endpoint(owner_instance)
                            if owner_endpoint is None:
                                if _http_bridge_has_durable_recovery_anchor(
                                    previous_response_id=previous_response_id,
                                    durable_lookup=durable_lookup,
                                ):
                                    if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                                        bridge_durable_recover_total.labels(path="owner_missing").inc()
                                    _log_http_bridge_event(
                                        "owner_endpoint_missing_local_recover",
                                        key,
                                        account_id=None,
                                        model=request_model,
                                        detail=(
                                            "expected_instance="
                                            f"{owner_instance}, current_instance={current_instance}, "
                                            "outcome=local_recover"
                                        ),
                                        cache_key_family=key.affinity_kind,
                                        model_class=_extract_model_class(request_model) if request_model else None,
                                        owner_check_applied=True,
                                    )
                                    force_durable_takeover = True
                                else:
                                    _log_http_bridge_event(
                                        "owner_mismatch_local_recover",
                                        key,
                                        account_id=None,
                                        model=request_model,
                                        detail=(
                                            "expected_instance="
                                            f"{owner_instance}, current_instance={current_instance}, "
                                            "outcome=local_recover_no_endpoint"
                                        ),
                                        cache_key_family=key.affinity_kind,
                                        model_class=_extract_model_class(request_model) if request_model else None,
                                        owner_check_applied=True,
                                    )
                                    force_durable_takeover = True
                            else:
                                owner_forward = _HTTPBridgeOwnerForward(
                                    owner_instance=owner_instance,
                                    owner_endpoint=owner_endpoint,
                                    key=key,
                                )
                    else:
                        if _http_bridge_has_durable_recovery_anchor(
                            previous_response_id=previous_response_id,
                            durable_lookup=durable_lookup,
                        ):
                            if PROMETHEUS_AVAILABLE and bridge_durable_recover_total is not None:
                                bridge_durable_recover_total.labels(path="owner_missing").inc()
                            _log_http_bridge_event(
                                "owner_mismatch_local_recover",
                                key,
                                account_id=None,
                                model=request_model,
                                detail=(
                                    "expected_instance="
                                    f"{owner_instance}, current_instance={current_instance}, "
                                    "outcome=local_recover"
                                ),
                                cache_key_family=key.affinity_kind,
                                model_class=_extract_model_class(request_model) if request_model else None,
                                owner_check_applied=True,
                            )
                            force_durable_takeover = True
                        else:
                            _log_http_bridge_event(
                                "owner_mismatch_local_recover",
                                key,
                                account_id=None,
                                model=request_model,
                                detail=(
                                    "expected_instance="
                                    f"{owner_instance}, current_instance={current_instance}, "
                                    "outcome=local_recover_no_forward"
                                ),
                                cache_key_family=key.affinity_kind,
                                model_class=_extract_model_class(request_model) if request_model else None,
                                owner_check_applied=True,
                            )
                            force_durable_takeover = True
                else:
                    _log_http_bridge_event(
                        "prompt_cache_locality_miss",
                        key,
                        account_id=None,
                        model=request_model,
                        detail=(
                            "expected_instance="
                            f"{owner_instance}, current_instance={current_instance}, "
                            "outcome=local_rebind"
                        ),
                        cache_key_family=key.affinity_kind,
                        model_class=_extract_model_class(request_model) if request_model else None,
                        owner_check_applied=False,
                    )
                    if _http_bridge_can_single_instance_prompt_cache_takeover_without_anchor(
                        key=key,
                        owner_instance=owner_instance,
                        current_instance=current_instance,
                        ring=ring,
                    ):
                        force_durable_takeover = True
                    elif allow_previous_response_recovery_rebind or allow_bootstrap_owner_rebind:
                        force_durable_takeover = True
                    _log_http_bridge_event(
                        "soft_locality_rebind",
                        key,
                        account_id=None,
                        model=request_model,
                        detail=(
                            "expected_instance="
                            f"{owner_instance}, current_instance={current_instance}, outcome=local_rebind"
                        ),
                        cache_key_family=key.affinity_kind,
                        model_class=_extract_model_class(request_model) if request_model else None,
                        owner_check_applied=False,
                    )
                    if PROMETHEUS_AVAILABLE:
                        if bridge_prompt_cache_locality_miss_total is not None:
                            bridge_prompt_cache_locality_miss_total.inc()
                        if bridge_soft_local_rebind_total is not None:
                            bridge_soft_local_rebind_total.inc()
                        if bridge_local_rebind_total is not None:
                            bridge_local_rebind_total.labels(reason="prompt_cache_locality_miss").inc()

        return _HTTPBridgeOwnerResolution(
            owner_check_required=owner_check_required,
            owner_forward=owner_forward,
            force_durable_takeover=force_durable_takeover,
        )
