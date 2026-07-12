from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Protocol, cast
from uuid import uuid4

from app.core.openai.requests import ResponsesRequest
from app.core.openai.response_create import _slim_response_create_payload_for_upstream
from app.core.types import JsonValue
from app.core.utils.request_id import ensure_request_id, get_request_id
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _normalize_session_id,
    _owner_lookup_session_id_from_headers,
    _response_create_client_metadata,
)
from app.modules.proxy._service.http_bridge.stream_policy import _fingerprint_input_items
from app.modules.proxy._service.service_tier import _normalize_service_tier_value
from app.modules.proxy._service.support import (
    _REQUEST_TRANSPORT_HTTP,
    _release_websocket_response_create_gate,
    _WebSocketRequestState,
)
from app.modules.proxy.work_admission import WorkAdmissionController

logger = logging.getLogger("app.modules.proxy.service")


class _ResponseCreateRuntimeService(Protocol):
    def _get_work_admission(self) -> WorkAdmissionController: ...

    @staticmethod
    def _response_create_max_bytes_compatible() -> int: ...

    @staticmethod
    def _enforce_response_create_size_limit_compatible(
        request_state: _WebSocketRequestState,
    ) -> None: ...

    def _prepare_response_bridge_request_state(
        self,
        payload: ResponsesRequest,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        include_type_field: bool,
        attach_event_queue: bool,
        transport: str,
        client_metadata: Mapping[str, JsonValue] | None,
        session_id: str | None = None,
        request_id: str | None = None,
        request_log_id: str | None = None,
    ) -> tuple[_WebSocketRequestState, str]: ...


class _ResponseCreateRuntimeMixin:
    def _prepare_http_bridge_request(
        self: _ResponseCreateRuntimeService,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        request_id: str | None = None,
    ) -> tuple[_WebSocketRequestState, str]:
        return self._prepare_response_bridge_request_state(
            payload,
            api_key=api_key,
            api_key_reservation=api_key_reservation,
            include_type_field=True,
            attach_event_queue=True,
            transport=_REQUEST_TRANSPORT_HTTP,
            client_metadata=_response_create_client_metadata(payload.to_payload(), headers=headers),
            session_id=_owner_lookup_session_id_from_headers(headers),
            request_log_id=request_id or get_request_id() or ensure_request_id(None),
        )

    def _prepare_response_bridge_request_state(
        self: _ResponseCreateRuntimeService,
        payload: ResponsesRequest,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        include_type_field: bool,
        attach_event_queue: bool,
        transport: str,
        client_metadata: Mapping[str, JsonValue] | None,
        session_id: str | None = None,
        request_id: str | None = None,
        request_log_id: str | None = None,
    ) -> tuple[_WebSocketRequestState, str]:
        upstream_payload = dict(payload.to_payload())
        upstream_payload.pop("stream", None)
        upstream_payload.pop("background", None)
        if include_type_field:
            upstream_payload["type"] = "response.create"
        if client_metadata:
            upstream_payload["client_metadata"] = client_metadata
        forwarded_service_tier = _normalize_service_tier_value(upstream_payload.get("service_tier"))
        input_item_count = 0
        input_full_fingerprint: str | None = None
        payload_input = payload.input
        if isinstance(payload_input, list):
            payload_input_list = cast(list[JsonValue], payload_input)
            input_item_count = len(payload_input_list)
            if input_item_count > 0:
                input_full_fingerprint = _fingerprint_input_items(payload_input_list)

        request_state = _WebSocketRequestState(
            request_id=request_id or f"ws_{uuid4().hex}",
            request_log_id=request_log_id,
            model=payload.model,
            service_tier=forwarded_service_tier,
            reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
            api_key_reservation=api_key_reservation,
            started_at=time.monotonic(),
            requested_service_tier=forwarded_service_tier,
            awaiting_response_created=True,
            event_queue=asyncio.Queue() if attach_event_queue else None,
            transport=transport,
            api_key=api_key,
            previous_response_id=payload.previous_response_id,
            session_id=_normalize_session_id(session_id),
            input_item_count=input_item_count,
            input_full_fingerprint=input_full_fingerprint,
        )
        text_data = json.dumps(upstream_payload, ensure_ascii=True, separators=(",", ":"))
        payload_size = len(text_data.encode("utf-8"))
        max_bytes = self._response_create_max_bytes_compatible()
        if payload_size > max_bytes:
            slimmed_payload, slim_summary = _slim_response_create_payload_for_upstream(
                upstream_payload,
                max_bytes=max_bytes,
            )
            if slim_summary is not None:
                upstream_payload = slimmed_payload
                text_data = json.dumps(upstream_payload, ensure_ascii=True, separators=(",", ":"))
                logger.warning(
                    (
                        "Slimmed response.create request_id=%s request_log_id=%s transport=%s "
                        "original_bytes=%s slimmed_bytes=%s "
                        "historical_tool_outputs_slimmed=%s historical_images_slimmed=%s"
                    ),
                    request_state.request_id,
                    request_state.request_log_id,
                    transport,
                    payload_size,
                    len(text_data.encode("utf-8")),
                    slim_summary["historical_tool_outputs_slimmed"],
                    slim_summary["historical_images_slimmed"],
                )
        request_state.request_text = text_data
        self._enforce_response_create_size_limit_compatible(request_state)
        return request_state, text_data

    async def _acquire_request_state_response_create_admission(
        self: _ResponseCreateRuntimeService,
        request_state: _WebSocketRequestState,
        *,
        response_create_gate: asyncio.Semaphore | None,
        compact: bool = False,
    ) -> None:
        request_state.response_create_gate = response_create_gate
        if response_create_gate is not None:
            request_state.http_bridge_gate_wait_started_at = time.monotonic()
            await response_create_gate.acquire()
            request_state.http_bridge_gate_acquired_at = time.monotonic()
            request_state.response_create_gate_acquired = True
        request_state.awaiting_response_created = True
        try:
            request_state.response_create_admission = await self._get_work_admission().acquire_response_create(
                compact=compact
            )
            request_state.http_bridge_admission_acquired_at = time.monotonic()
        except BaseException:
            _release_websocket_response_create_gate(request_state, response_create_gate)
            raise
