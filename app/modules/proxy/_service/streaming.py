from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Protocol

import aiohttp
import anyio

from app.core.auth.refresh import RefreshError
from app.core.balancer import failover_decision
from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.proxy import (
    ProxyResponseError,
)
from app.core.config.settings import Settings
from app.core.crypto import TokenEncryptor
from app.core.errors import response_failed_event
from app.core.openai.parsing import parse_sse_event
from app.core.openai.requests import ResponsesRequest
from app.core.types import JsonValue
from app.core.utils.request_id import ensure_request_id
from app.core.utils.retry import backoff_seconds
from app.core.utils.sse import format_sse_event, parse_sse_data_json
from app.db.models import Account, DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData, ApiKeysService, ApiKeyUsageCharge, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _owner_lookup_session_id_from_headers,
    _prompt_cache_key_from_request_model,
    _sticky_key_for_responses_request,
)
from app.modules.proxy._service.budget import (
    _proxy_request_timeout_event,
    _raise_proxy_budget_exhausted,
)
from app.modules.proxy._service.observability import (
    _elapsed_ms,
    _maybe_log_proxy_request_shape,
    _maybe_log_proxy_service_tier_trace,
)
from app.modules.proxy._service.service_tier import (
    _payload_with_account_service_tier,
    _service_tier_from_event_payload,
)
from app.modules.proxy._service.support import (
    _ACCOUNT_RECOVERY_RETRY_CODES,
    _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES,
    _REQUEST_TRANSPORT_HTTP,
    _apply_usage_charges_to_settlement,
    _await_operation_before_hard_timeout,
    _event_type_from_payload,
    _is_account_neutral_error_code,
    _RetryableStreamError,
    _routing_strategy,
    _schedule_tracked_background_task,
    _should_penalize_stream_error,
    _stream_settlement_error_payload,
    _stream_settlement_has_authoritative_usage,
    _StreamSettlement,
    _TerminalStreamError,
    _TransientStreamError,
    _usage_charge_from_response_usage,
)
from app.modules.proxy._service.upstream_account import (
    _account_upstream_base_url,
    _account_upstream_wire_api,
    _upstream_account_header_value,
)
from app.modules.proxy._service.websocket.events import (
    _build_rewritten_stream_response_failed_event,
    _http_error_status_from_payload,
    _should_failover_first_event_failure,
)
from app.modules.proxy.helpers import (
    _apply_error_metadata,
    _normalize_error_code,
    _parse_openai_error,
    _upstream_error_from_openai,
)
from app.modules.proxy.load_balancer import AccountSelection, LoadBalancer
from app.modules.proxy.repo_bundle import ProxyRepoFactory
from app.modules.proxy.work_admission import AdmissionLease, WorkAdmissionController

logger = logging.getLogger("app.modules.proxy.service")

_STREAM_MAX_ACCOUNT_ATTEMPTS = 3
_STREAM_TERMINAL_LOG_TIMEOUT_SECONDS = 0.1
_TRANSIENT_RETRY_CODES = frozenset({"server_error"})
_TEXT_DELTA_EVENT_TYPES = frozenset({"response.output_text.delta", "response.refusal.delta"})
_TEXT_DONE_CONTENT_PART_TYPES = frozenset({"output_text", "refusal"})


def _resolve_upstream_stream_transport(upstream_stream_transport: str) -> str | None:
    if upstream_stream_transport == "default":
        return None
    return upstream_stream_transport


def _stream_request_budget_seconds(settings: object, *, request_transport: str) -> float:
    if request_transport == _REQUEST_TRANSPORT_HTTP:
        budget = getattr(settings, "http_responses_stream_request_budget_seconds", None)
        if budget is not None:
            return float(budget)
    return float(getattr(settings, "proxy_request_budget_seconds"))


def _should_retry_stream_error(code: str) -> bool:
    return code in _ACCOUNT_RECOVERY_RETRY_CODES


def _should_suppress_text_done_event(
    *,
    event_type: str | None,
    payload: dict[str, JsonValue] | None,
    suppress_text_done_events: bool,
    saw_text_delta: bool,
) -> bool:
    if not suppress_text_done_events or not saw_text_delta or event_type is None:
        return False
    if event_type == "response.output_text.done":
        return True
    if event_type == "response.content_part.done":
        return _is_text_content_part(payload)
    return False


def _is_text_content_part(payload: dict[str, JsonValue] | None) -> bool:
    if payload is None:
        return False
    part = payload.get("part")
    if not isinstance(part, dict):
        return False
    part_type = part.get("type")
    return isinstance(part_type, str) and part_type in _TEXT_DONE_CONTENT_PART_TYPES


def _log_direct_sse_latency_breakdown(
    *,
    request_id: str,
    response_id: str | None,
    session_id: str | None,
    account_id: str,
    model: str,
    event_type: str | None,
    first_upstream_event_type: str | None,
    request_started_at: float,
    stream_once_started_at: float,
    admission_wait_started_at: float | None,
    admission_acquired_at: float | None,
    stream_created_at: float | None,
    upstream_first_event_at: float | None,
    first_text_at: float,
    request_transport: str,
) -> None:
    logger.warning(
        "direct_sse_latency_breakdown request_id=%s response_id=%s session_id=%s account_id=%s"
        " model=%s transport=%s event_type=%s first_upstream_event_type=%s total_to_first_text_ms=%s"
        " pre_stream_once_local_ms=%s response_create_admission_wait_ms=%s stream_factory_ms=%s"
        " upstream_first_event_ms=%s upstream_first_text_ms=%s first_event_to_first_text_ms=%s",
        request_id,
        response_id,
        session_id,
        account_id,
        model,
        request_transport,
        event_type,
        first_upstream_event_type,
        _elapsed_ms(request_started_at, first_text_at),
        _elapsed_ms(request_started_at, stream_once_started_at),
        _elapsed_ms(admission_wait_started_at, admission_acquired_at),
        _elapsed_ms(admission_acquired_at, stream_created_at),
        _elapsed_ms(request_started_at, upstream_first_event_at),
        _elapsed_ms(request_started_at, first_text_at),
        _elapsed_ms(upstream_first_event_at, first_text_at),
    )


class _StreamingService(Protocol):
    _encryptor: TokenEncryptor
    _load_balancer: LoadBalancer
    _repo_factory: ProxyRepoFactory
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

    async def _close_direct_stream_iterator(
        self,
        stream: AsyncIterator[str],
        *,
        label: str,
    ) -> None: ...

    @staticmethod
    def _proxy_runtime_settings() -> Settings: ...

    @staticmethod
    async def _proxy_dashboard_settings() -> DashboardSettings: ...

    @staticmethod
    def _record_continuity_fail_closed_compatible(
        *,
        surface: str,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None = None,
        upstream_error_code: str | None = None,
    ) -> None: ...

    def _core_stream_responses_compatible(
        self,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
        raise_for_status: bool,
        upstream_stream_transport_override: str | None = None,
    ) -> AsyncIterator[str]: ...

    @staticmethod
    def _remaining_budget_seconds_compatible(deadline: float) -> float: ...

    @staticmethod
    def _push_stream_attempt_timeout_overrides_compatible(
        timeout_seconds: float,
    ) -> tuple[float | None, float | None, float | None]: ...

    @staticmethod
    def _pop_stream_timeout_overrides_compatible(
        tokens: tuple[float | None, float | None, float | None],
    ) -> None: ...

    @staticmethod
    def _rewrite_previous_response_stream_error_compatible(
        *,
        previous_response_id: str | None,
        preferred_account_id: str | None,
        error_code: str | None,
        error_type: str | None,
        error_message: str | None,
        error_param: str | None,
    ) -> tuple[str, str, str | None] | None: ...

    async def _select_account_with_budget_compatible(
        self,
        deadline: float,
        **kwargs: object,
    ) -> AccountSelection: ...

    async def _resolve_websocket_previous_response_owner(
        self,
        *,
        previous_response_id: str | None,
        api_key: ApiKeyData | None,
        session_id: str | None = None,
        surface: str,
    ) -> str | None: ...

    async def _ensure_fresh_with_budget(
        self,
        account: Account,
        *,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> Account: ...

    async def _handle_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        http_status: int | None = None,
    ) -> ClassifiedFailure: ...

    def _classify_and_schedule_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        *,
        http_status: int | None = None,
        additional_error_count: int = 0,
    ) -> ClassifiedFailure: ...

    async def _settle_stream_api_key_usage(
        self,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool: ...

    async def _settle_stream_api_key_usage_with_fallback(
        self,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool: ...

    def _get_work_admission(self) -> WorkAdmissionController: ...

    async def _write_stream_preflight_error(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        model: str | None,
        start: float,
        error_code: str,
        error_message: str,
        reasoning_effort: str | None,
        service_tier: str | None,
        transport: str,
    ) -> None: ...

    def _schedule_stream_preflight_error_before_deadline(
        self,
        *,
        deadline: float,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        model: str | None,
        start: float,
        error_code: str,
        error_message: str,
        reasoning_effort: str | None,
        service_tier: str | None,
        transport: str,
    ) -> None: ...

    async def _release_unsettled_stream_reservation(
        self,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> None: ...

    def _schedule_terminal_stream_log_before_deadline(
        self,
        *,
        deadline: float,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        payload: ResponsesRequest,
        start: float,
        error_code: str,
        error_message: str,
        request_transport: str,
    ) -> None: ...

    async def _write_request_log(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        model: str | None,
        latency_ms: int,
        status: str,
        latency_first_token_ms: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        reasoning_effort: str | None = None,
        transport: str | None = None,
        service_tier: str | None = None,
        requested_service_tier: str | None = None,
        actual_service_tier: str | None = None,
        session_id: str | None = None,
    ) -> None: ...

    def _stream_once(
        self,
        account: Account,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        request_id: str,
        allow_retry: bool,
        *,
        request_started_at: float,
        allow_transient_retry: bool = False,
        api_key: ApiKeyData | None,
        settlement: _StreamSettlement,
        suppress_text_done_events: bool,
        upstream_stream_transport: str | None,
        request_transport: str,
        preferred_account_id: str | None = None,
        request_deadline_at: float,
    ) -> AsyncIterator[str]: ...


class _StreamingMixin:
    async def _close_direct_stream_iterator(
        self: _StreamingService,
        stream: AsyncIterator[str],
        *,
        label: str,
    ) -> None:
        close = getattr(stream, "aclose", None)
        if not callable(close):
            return
        try:
            await _await_operation_before_hard_timeout(
                close(),
                timeout_seconds=1.0,
                tasks=self._proxy_cleanup_tasks,
                label=label,
            )
        except TimeoutError:
            logger.warning("Timed out closing %s; close remains tracked", label)

    def _schedule_stream_preflight_error_before_deadline(
        self: _StreamingService,
        *,
        deadline: float,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        model: str | None,
        start: float,
        error_code: str,
        error_message: str,
        reasoning_effort: str | None,
        service_tier: str | None,
        transport: str,
    ) -> None:
        async def write() -> None:
            remaining = self._remaining_budget_seconds_compatible(deadline)
            if remaining <= 0:
                logger.warning(
                    "Skipping stream preflight error log after request deadline request_id=%s error_code=%s",
                    request_id,
                    error_code,
                )
                return
            try:
                await _await_operation_before_hard_timeout(
                    self._write_stream_preflight_error(
                        account_id=account_id,
                        api_key=api_key,
                        request_id=request_id,
                        model=model,
                        start=start,
                        error_code=error_code,
                        error_message=error_message,
                        reasoning_effort=reasoning_effort,
                        service_tier=service_tier,
                        transport=transport,
                    ),
                    timeout_seconds=min(remaining, _STREAM_TERMINAL_LOG_TIMEOUT_SECONDS),
                    tasks=self._proxy_cleanup_tasks,
                    label=f"stream preflight error log request_id={request_id}",
                )
            except TimeoutError:
                logger.warning(
                    "Stream preflight error log exceeded request deadline request_id=%s error_code=%s",
                    request_id,
                    error_code,
                )

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            write(),
            name=f"stream-preflight-log-{request_id}",
            label=f"stream preflight error log request_id={request_id} code={error_code}",
        )

    def _schedule_terminal_stream_log_before_deadline(
        self: _StreamingService,
        *,
        deadline: float,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        payload: ResponsesRequest,
        start: float,
        error_code: str,
        error_message: str,
        request_transport: str,
    ) -> None:
        async def write() -> None:
            remaining = self._remaining_budget_seconds_compatible(deadline)
            if remaining <= 0:
                logger.warning(
                    "Skipping terminal stream log after request deadline request_id=%s error_code=%s",
                    request_id,
                    error_code,
                )
                return
            try:
                await _await_operation_before_hard_timeout(
                    self._write_request_log(
                        account_id=account_id,
                        api_key=api_key,
                        request_id=request_id,
                        model=payload.model,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        status="error",
                        error_code=error_code,
                        error_message=error_message,
                        reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                        transport=request_transport,
                        service_tier=payload.service_tier,
                        requested_service_tier=payload.service_tier,
                    ),
                    timeout_seconds=min(remaining, _STREAM_TERMINAL_LOG_TIMEOUT_SECONDS),
                    tasks=self._proxy_cleanup_tasks,
                    label=f"terminal stream log request_id={request_id}",
                )
            except TimeoutError:
                logger.warning(
                    "Terminal stream log exceeded bounded tail budget request_id=%s error_code=%s",
                    request_id,
                    error_code,
                )

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            write(),
            name=f"terminal-stream-log-{request_id}",
            label=f"terminal stream log request_id={request_id} code={error_code}",
        )

    async def _release_unsettled_stream_reservation(
        self: _StreamingService,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        request_id: str,
    ) -> None:
        if api_key is None or api_key_reservation is None:
            return
        for attempt in range(2):
            try:
                with anyio.CancelScope(shield=True):
                    async with self._repo_factory() as repos:
                        api_keys_service = ApiKeysService(repos.api_keys)
                        await api_keys_service.release_usage_reservation(
                            api_key_reservation.reservation_id,
                        )
                return
            except asyncio.CancelledError:
                if attempt > 0:
                    raise
                logger.warning(
                    "Stream API key reservation release cancelled; retrying key_id=%s request_id=%s",
                    api_key.id,
                    request_id,
                )
            except Exception:
                if attempt > 0:
                    raise
                logger.warning(
                    "Failed to release stream API key reservation; retrying key_id=%s request_id=%s",
                    api_key.id,
                    request_id,
                    exc_info=True,
                )

    async def _stream_with_retry(
        self: _StreamingService,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool,
        propagate_http_errors: bool,
        openai_cache_affinity: bool,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        suppress_text_done_events: bool,
        request_transport: str,
        request_started_at: float | None = None,
        request_deadline_at: float | None = None,
    ) -> AsyncIterator[str]:
        request_id = ensure_request_id()
        if (request_started_at is None) != (request_deadline_at is None):
            raise RuntimeError("Direct Responses request timing must be provided as a complete pair")
        start = request_started_at if request_started_at is not None else time.monotonic()
        base_settings = self._proxy_runtime_settings()
        deadline = (
            request_deadline_at
            if request_deadline_at is not None
            else start
            + _stream_request_budget_seconds(
                base_settings,
                request_transport=request_transport,
            )
        )
        settled = False
        settlement = _StreamSettlement()
        attempt_usage_charges: list[ApiKeyUsageCharge] = []
        captured_attempt_settlements: list[_StreamSettlement] = []

        def capture_attempt_usage(attempt_settlement: _StreamSettlement) -> None:
            if any(existing is attempt_settlement for existing in captured_attempt_settlements):
                return
            captured_attempt_settlements.append(attempt_settlement)
            attempt_usage_charges.extend(attempt_settlement.usage_charges)

        def apply_attempt_usage() -> bool:
            capture_attempt_usage(settlement)
            return _apply_usage_charges_to_settlement(settlement, attempt_usage_charges)

        async def settle_terminal_reservation(
            settlement_to_settle: _StreamSettlement | None = None,
        ) -> None:
            target = settlement if settlement_to_settle is None else settlement_to_settle
            try:
                await self._settle_stream_api_key_usage_with_fallback(
                    api_key,
                    api_key_reservation,
                    target,
                    request_id,
                )
            except Exception:
                if _stream_settlement_has_authoritative_usage(target):
                    logger.error(
                        "Authoritative terminal stream settlement interrupted; reservation retained "
                        "request_id=%s",
                        request_id,
                        exc_info=True,
                    )
                else:
                    logger.warning(
                        "Terminal stream reservation settlement failed; retrying conditional release "
                        "request_id=%s",
                        request_id,
                        exc_info=True,
                    )
                    await self._release_unsettled_stream_reservation(
                        api_key=api_key,
                        api_key_reservation=api_key_reservation,
                        request_id=request_id,
                    )

        def schedule_late_usage_settlement(target: _StreamSettlement) -> bool:
            reconciliation = target.late_usage_reconciliation
            if reconciliation is None:
                return False
            prior_charges = tuple(attempt_usage_charges)
            target.status = "error"
            target.record_success = False

            async def reconcile_and_settle() -> None:
                late_settlement = await asyncio.shield(reconciliation)
                charges = list(prior_charges)
                if late_settlement is not None:
                    charges.extend(late_settlement.usage_charges)
                _apply_usage_charges_to_settlement(target, charges)
                await settle_terminal_reservation(target)

            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                reconcile_and_settle(),
                name=f"stream-late-usage-settlement-{request_id}",
                label=f"late terminal stream usage settlement request_id={request_id}",
            )
            return True

        def transfer_local_terminal_release_ownership() -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            if schedule_late_usage_settlement(settlement):
                return
            if apply_attempt_usage():
                settlement.status = "error"
                settlement.record_success = False
                _schedule_tracked_background_task(
                    self._proxy_cleanup_tasks,
                    settle_terminal_reservation(),
                    name=f"stream-reservation-settlement-{request_id}",
                    label=f"local terminal stream reservation settlement request_id={request_id}",
                )
                return
            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                self._release_unsettled_stream_reservation(
                    api_key=api_key,
                    api_key_reservation=api_key_reservation,
                    request_id=request_id,
                ),
                name=f"stream-reservation-release-{request_id}",
                label=f"local terminal stream reservation release request_id={request_id}",
            )

        dashboard_remaining = self._remaining_budget_seconds_compatible(deadline)
        dashboard_timed_out = dashboard_remaining <= 0
        if not dashboard_timed_out:
            try:
                settings = await _await_operation_before_hard_timeout(
                    self._proxy_dashboard_settings(),
                    timeout_seconds=dashboard_remaining,
                    tasks=self._proxy_cleanup_tasks,
                    label=f"direct Responses dashboard preflight request_id={request_id}",
                )
            except TimeoutError:
                dashboard_timed_out = True
            except BaseException:
                transfer_local_terminal_release_ownership()
                raise
        if dashboard_timed_out:
            self._schedule_stream_preflight_error_before_deadline(
                deadline=deadline,
                account_id=None,
                api_key=api_key,
                request_id=request_id,
                model=payload.model,
                start=start,
                error_code="upstream_request_timeout",
                error_message="Proxy request budget exhausted",
                reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                service_tier=payload.service_tier,
                transport=request_transport,
            )
            transfer_local_terminal_release_ownership()
            if propagate_http_errors:
                _raise_proxy_budget_exhausted()
            yield format_sse_event(_proxy_request_timeout_event(request_id))
            return
        prefer_earlier_reset = settings.prefer_earlier_reset_accounts
        upstream_stream_transport = _resolve_upstream_stream_transport(settings.upstream_stream_transport)
        had_prompt_cache_key = _prompt_cache_key_from_request_model(payload) is not None
        affinity = _sticky_key_for_responses_request(
            payload,
            headers,
            codex_session_affinity=codex_session_affinity,
            openai_cache_affinity=openai_cache_affinity,
            openai_cache_affinity_max_age_seconds=settings.openai_cache_affinity_max_age_seconds,
            sticky_threads_enabled=settings.sticky_threads_enabled,
            settings=base_settings,
            api_key=api_key,
        )
        sticky_key_source = "none"
        if affinity.kind == StickySessionKind.CODEX_SESSION:
            sticky_key_source = "session_header"
        elif affinity.key:
            sticky_key_source = "payload" if had_prompt_cache_key else "derived"
        _maybe_log_proxy_request_shape(
            "stream",
            payload,
            headers,
            sticky_kind=affinity.kind.value if affinity.kind is not None else None,
            sticky_key_source=sticky_key_source,
            prompt_cache_key_set=_prompt_cache_key_from_request_model(payload) is not None,
            settings=base_settings,
        )
        routing_strategy = _routing_strategy(settings)
        max_attempts = _STREAM_MAX_ACCOUNT_ATTEMPTS
        any_attempt_logged = False
        last_transient_exc: ProxyResponseError | None = None
        last_refresh_failure: tuple[str, str, str] | None = None
        excluded_account_ids: set[str] = set()
        preferred_account_id: str | None = None
        require_preferred_account = False
        terminal_account: Account | None = None
        terminal_finalization_scheduled = False

        def transfer_terminal_reservation_ownership(account: Account) -> None:
            nonlocal settled, terminal_account
            if settled:
                return
            # Transfer ownership before exposing the terminal event. The inner
            # iterator still owns populating ``settlement`` until it unwinds.
            settled = True
            terminal_account = account

        def schedule_terminal_finalization() -> None:
            nonlocal terminal_finalization_scheduled
            if not settled or terminal_account is None or terminal_finalization_scheduled:
                return
            terminal_finalization_scheduled = True
            if schedule_late_usage_settlement(settlement):
                return
            apply_attempt_usage()

            try:
                if terminal_account is not None:
                    if settlement.account_health_error:
                        self._classify_and_schedule_stream_error(
                            terminal_account,
                            _stream_settlement_error_payload(settlement),
                            settlement.error_code or "upstream_error",
                        )
                    elif settlement.record_success:
                        _schedule_tracked_background_task(
                            self._proxy_cleanup_tasks,
                            self._load_balancer.record_success(terminal_account),
                            name=f"stream-success-persist-{terminal_account.id}-{time.monotonic_ns()}",
                            label=f"stream success persistence account_id={terminal_account.id}",
                        )
            except Exception:
                logger.warning(
                    "Failed to schedule terminal account bookkeeping request_id=%s",
                    request_id,
                    exc_info=True,
                )

            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                settle_terminal_reservation(),
                name=f"stream-reservation-settlement-{request_id}",
                label=f"terminal stream reservation settlement request_id={request_id}",
            )

        try:
            if payload.previous_response_id is not None:
                owner_lookup_remaining = self._remaining_budget_seconds_compatible(deadline)
                if owner_lookup_remaining <= 0:
                    _raise_proxy_budget_exhausted()
                try:
                    preferred_account_id = await _await_operation_before_hard_timeout(
                        self._resolve_websocket_previous_response_owner(
                            previous_response_id=payload.previous_response_id,
                            api_key=api_key,
                            session_id=_owner_lookup_session_id_from_headers(headers),
                            surface="http_stream",
                        ),
                        timeout_seconds=owner_lookup_remaining,
                        tasks=self._proxy_cleanup_tasks,
                        label=f"direct Responses owner lookup request_id={request_id}",
                    )
                except TimeoutError:
                    _raise_proxy_budget_exhausted()
                require_preferred_account = preferred_account_id is not None
            for attempt in range(max_attempts):
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    logger.warning(
                        "Proxy request budget exhausted before retry request_id=%s attempt=%s",
                        request_id,
                        attempt + 1,
                    )
                    self._schedule_stream_preflight_error_before_deadline(
                        deadline=deadline,
                        account_id=None,
                        api_key=api_key,
                        request_id=request_id,
                        model=payload.model,
                        start=start,
                        error_code="upstream_request_timeout",
                        error_message="Proxy request budget exhausted",
                        reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                        service_tier=payload.service_tier,
                        transport=request_transport,
                    )
                    transfer_local_terminal_release_ownership()
                    yield format_sse_event(_proxy_request_timeout_event(request_id))
                    return
                try:
                    selection = await self._select_account_with_budget_compatible(
                        deadline,
                        request_id=request_id,
                        kind="stream",
                        api_key=api_key,
                        sticky_key=affinity.key,
                        sticky_kind=affinity.kind,
                        reallocate_sticky=affinity.reallocate_sticky,
                        sticky_max_age_seconds=affinity.max_age_seconds,
                        prefer_earlier_reset_accounts=prefer_earlier_reset,
                        routing_strategy=routing_strategy,
                        model=payload.model,
                        exclude_account_ids=excluded_account_ids,
                        preferred_account_id=preferred_account_id,
                        required_upstream_wire_api=affinity.required_upstream_wire_api,
                    )
                except ProxyResponseError as exc:
                    error = _parse_openai_error(exc.payload)
                    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                    error_message = error.message if error else None
                    if error_code == "upstream_unavailable" and error_message == "Proxy request budget exhausted":
                        self._schedule_stream_preflight_error_before_deadline(
                            deadline=deadline,
                            account_id=None,
                            api_key=api_key,
                            request_id=request_id,
                            model=payload.model,
                            start=start,
                            error_code="upstream_request_timeout",
                            error_message="Proxy request budget exhausted",
                            reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                            service_tier=payload.service_tier,
                            transport=request_transport,
                        )
                        transfer_local_terminal_release_ownership()
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return
                    event = response_failed_event(
                        error_code,
                        error_message or "Upstream unavailable",
                        error_type=(error.type or "server_error") if error else "server_error",
                        response_id=request_id,
                    )
                    _apply_error_metadata(event["response"]["error"], error)
                    transfer_local_terminal_release_ownership()
                    yield format_sse_event(event)
                    return
                account = selection.account
                if not account:
                    if require_preferred_account and preferred_account_id is not None:
                        message = "Previous response owner account is unavailable; retry later."
                        self._record_continuity_fail_closed_compatible(
                            surface="http_stream",
                            reason="owner_account_unavailable",
                            previous_response_id=payload.previous_response_id,
                            session_id=headers.get("x-codex-turn-state") or headers.get("session_id"),
                            upstream_error_code="no_accounts",
                        )
                        event = response_failed_event(
                            "upstream_unavailable",
                            message,
                            response_id=request_id,
                        )
                        transfer_local_terminal_release_ownership()
                        yield format_sse_event(event)
                        self._schedule_terminal_stream_log_before_deadline(
                            deadline=deadline,
                            account_id=preferred_account_id,
                            api_key=api_key,
                            request_id=request_id,
                            payload=payload,
                            start=start,
                            error_code="upstream_unavailable",
                            error_message=message,
                            request_transport=request_transport,
                        )
                        return
                    if last_refresh_failure is not None:
                        failed_account_id, failure_code, failure_message = last_refresh_failure
                        transfer_local_terminal_release_ownership()
                        yield format_sse_event(
                            response_failed_event(
                                failure_code,
                                failure_message,
                                response_id=request_id,
                            )
                        )
                        self._schedule_terminal_stream_log_before_deadline(
                            deadline=deadline,
                            account_id=failed_account_id,
                            api_key=api_key,
                            request_id=request_id,
                            payload=payload,
                            start=start,
                            error_code=failure_code,
                            error_message=failure_message,
                            request_transport=request_transport,
                        )
                        return
                    # If a prior attempt stored a transient 500 and the caller
                    # expects HTTP error propagation, re-raise the original error
                    # instead of returning a generic no_accounts event.
                    if propagate_http_errors and last_transient_exc is not None:
                        raise last_transient_exc
                    no_accounts_msg = selection.error_message or "No active accounts available"
                    error_code = selection.error_code or "no_accounts"
                    event = response_failed_event(
                        error_code,
                        no_accounts_msg,
                        response_id=request_id,
                    )
                    transfer_local_terminal_release_ownership()
                    yield format_sse_event(event)
                    self._schedule_terminal_stream_log_before_deadline(
                        deadline=deadline,
                        account_id=None,
                        api_key=api_key,
                        request_id=request_id,
                        payload=payload,
                        start=start,
                        error_code=error_code,
                        error_message=no_accounts_msg,
                        request_transport=request_transport,
                    )
                    return

                account_id_value = account.id
                if (
                    require_preferred_account
                    and preferred_account_id is not None
                    and account.id != preferred_account_id
                ):
                    message = "Previous response owner account is unavailable; retry later."
                    self._record_continuity_fail_closed_compatible(
                        surface="http_stream",
                        reason="owner_account_unavailable",
                        previous_response_id=payload.previous_response_id,
                        session_id=headers.get("x-codex-turn-state") or headers.get("session_id"),
                        upstream_error_code="upstream_unavailable",
                    )
                    event = response_failed_event(
                        "upstream_unavailable",
                        message,
                        response_id=request_id,
                    )
                    transfer_local_terminal_release_ownership()
                    yield format_sse_event(event)
                    self._schedule_terminal_stream_log_before_deadline(
                        deadline=deadline,
                        account_id=preferred_account_id,
                        api_key=api_key,
                        request_id=request_id,
                        payload=payload,
                        start=start,
                        error_code="upstream_unavailable",
                        error_message=message,
                        request_transport=request_transport,
                    )
                    return
                try:
                    remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                    if remaining_budget <= 0:
                        logger.warning(
                            "Proxy request budget exhausted before freshness check "
                            "request_id=%s attempt=%s account_id=%s",
                            request_id,
                            attempt + 1,
                            account.id,
                        )
                        self._schedule_stream_preflight_error_before_deadline(
                            deadline=deadline,
                            account_id=account.id,
                            api_key=api_key,
                            request_id=request_id,
                            model=payload.model,
                            start=start,
                            error_code="upstream_request_timeout",
                            error_message="Proxy request budget exhausted",
                            reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                            service_tier=payload.service_tier,
                            transport=request_transport,
                        )
                        transfer_local_terminal_release_ownership()
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return
                    try:
                        account = await self._ensure_fresh_with_budget(account, timeout_seconds=remaining_budget)
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        logger.warning(
                            "Stream refresh/connect failed request_id=%s attempt=%s account_id=%s",
                            request_id,
                            attempt + 1,
                            account.id,
                            exc_info=True,
                        )
                        self._classify_and_schedule_stream_error(
                            account,
                            {"message": "Upstream credential refresh failed"},
                            "upstream_unavailable",
                        )
                        last_refresh_failure = (
                            account.id,
                            "upstream_unavailable",
                            "Upstream credential refresh failed",
                        )
                        excluded_account_ids.add(account.id)
                        continue
                    any_attempt_logged = True
                    settlement = _StreamSettlement()
                    effective_attempt_timeout = self._remaining_budget_seconds_compatible(deadline)
                    if effective_attempt_timeout <= 0:
                        logger.warning(
                            "Proxy request budget exhausted before stream attempt "
                            "request_id=%s attempt=%s account_id=%s",
                            request_id,
                            attempt + 1,
                            account.id,
                        )
                        self._schedule_stream_preflight_error_before_deadline(
                            deadline=deadline,
                            account_id=account.id,
                            api_key=api_key,
                            request_id=request_id,
                            model=payload.model,
                            start=start,
                            error_code="upstream_request_timeout",
                            error_message="Proxy request budget exhausted",
                            reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                            service_tier=payload.service_tier,
                            transport=request_transport,
                        )
                        transfer_local_terminal_release_ownership()
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return
                    transient_retries = 0
                    allow_retry_flag = attempt < max_attempts - 1
                    while True:
                        stream_timeout_tokens = self._push_stream_attempt_timeout_overrides_compatible(
                            self._remaining_budget_seconds_compatible(deadline),
                        )
                        try:
                            settlement = _StreamSettlement()
                            child_stream = self._stream_once(
                                account,
                                payload,
                                headers,
                                request_id,
                                allow_retry_flag,
                                request_started_at=start,
                                allow_transient_retry=(
                                    transient_retries < _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES - 1 or allow_retry_flag
                                ),
                                api_key=api_key,
                                settlement=settlement,
                                suppress_text_done_events=suppress_text_done_events,
                                upstream_stream_transport=upstream_stream_transport,
                                request_transport=request_transport,
                                preferred_account_id=preferred_account_id,
                                request_deadline_at=deadline,
                            )
                            try:
                                async for line in child_stream:
                                    terminal_event = parse_sse_event(line)
                                    terminal_event_type = _event_type_from_payload(
                                        terminal_event,
                                        parse_sse_data_json(line),
                                    )
                                    if terminal_event_type in {
                                        "response.completed",
                                        "response.failed",
                                        "response.incomplete",
                                        "error",
                                    }:
                                        transfer_terminal_reservation_ownership(account)
                                    yield line
                            finally:
                                try:
                                    await self._close_direct_stream_iterator(
                                        child_stream,
                                        label=f"direct Responses attempt iterator request_id={request_id}",
                                    )
                                finally:
                                    capture_attempt_usage(settlement)
                        except (_TransientStreamError, ProxyResponseError) as tex:
                            if isinstance(tex, ProxyResponseError) and tex.status_code != 500:
                                error = _parse_openai_error(tex.payload)
                                code = _normalize_error_code(
                                    error.code if error else None,
                                    error.type if error else None,
                                )
                                if _is_account_neutral_error_code(code):
                                    raise
                                classified = self._classify_and_schedule_stream_error(
                                    account,
                                    _upstream_error_from_openai(error),
                                    code,
                                    http_status=tex.status_code,
                                )
                                if getattr(base_settings, "deterministic_failover_enabled", True):
                                    action = failover_decision(
                                        failure_class=classified["failure_class"],
                                        downstream_visible=False,
                                        candidates_remaining=max_attempts - attempt - 1,
                                    )
                                else:
                                    action = "surface"
                                logger.info(
                                    "Failover decision request_id=%s transport=stream account_id=%s "
                                    "attempt=%d failure_class=%s action=%s",
                                    request_id,
                                    account.id,
                                    attempt + 1,
                                    classified["failure_class"],
                                    action,
                                )
                                if action == "failover_next":
                                    last_transient_exc = tex
                                    excluded_account_ids.add(account.id)
                                    break
                                raise
                            transient_retries += 1
                            error_code = tex.code if isinstance(tex, _TransientStreamError) else "server_error"
                            error_payload: UpstreamError = (
                                tex.error
                                if isinstance(tex, _TransientStreamError)
                                else _upstream_error_from_openai(_parse_openai_error(tex.payload))
                            )
                            if (
                                transient_retries < _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES
                                and self._remaining_budget_seconds_compatible(deadline) > 0
                            ):
                                delay = min(
                                    backoff_seconds(transient_retries),
                                    self._remaining_budget_seconds_compatible(deadline),
                                )
                                logger.info(
                                    "Transient stream error, retrying same account "
                                    "request_id=%s account_id=%s retry=%s/%s delay=%.2fs code=%s",
                                    request_id,
                                    account.id,
                                    transient_retries,
                                    _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES,
                                    delay,
                                    error_code,
                                )
                                await asyncio.sleep(delay)
                                continue  # inner loop: retry same account
                            # Exhausted same-account retries — penalize and failover
                            logger.warning(
                                "Transient retries exhausted for account "
                                "request_id=%s account_id=%s retries=%s code=%s",
                                request_id,
                                account.id,
                                transient_retries,
                                error_code,
                            )
                            self._classify_and_schedule_stream_error(
                                account,
                                error_payload,
                                error_code,
                                # Record the remaining failures in the same background
                                # operation so failover never waits on persistence.
                                additional_error_count=transient_retries - 1,
                            )
                            # Preserve last ProxyResponseError for propagate_http_errors path.
                            if isinstance(tex, ProxyResponseError):
                                last_transient_exc = tex
                            excluded_account_ids.add(account.id)
                            break  # outer loop: select different account
                        finally:
                            self._pop_stream_timeout_overrides_compatible(stream_timeout_tokens)
                        transfer_terminal_reservation_ownership(account)
                        schedule_terminal_finalization()
                        return
                    continue  # outer loop: account failover after transient exhaustion
                except _RetryableStreamError as exc:
                    self._classify_and_schedule_stream_error(account, exc.error, exc.code)
                    excluded_account_ids.add(account.id)
                    continue
                except _TerminalStreamError:
                    transfer_terminal_reservation_ownership(account)
                    schedule_terminal_finalization()
                    return
                except ProxyResponseError as exc:
                    if exc.status_code == 401:
                        remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                        if remaining_budget <= 0:
                            logger.warning(
                                "Proxy request budget exhausted before forced refresh retry "
                                "request_id=%s attempt=%s account_id=%s",
                                request_id,
                                attempt + 1,
                                account.id,
                            )
                            self._schedule_stream_preflight_error_before_deadline(
                                deadline=deadline,
                                account_id=account.id,
                                api_key=api_key,
                                request_id=request_id,
                                model=payload.model,
                                start=start,
                                error_code="upstream_request_timeout",
                                error_message="Proxy request budget exhausted",
                                reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                                service_tier=payload.service_tier,
                                transport=request_transport,
                            )
                            transfer_local_terminal_release_ownership()
                            yield format_sse_event(_proxy_request_timeout_event(request_id))
                            return
                        try:
                            account = await self._ensure_fresh_with_budget(
                                account,
                                force=True,
                                timeout_seconds=remaining_budget,
                            )
                        except RefreshError as refresh_exc:
                            if refresh_exc.is_permanent:
                                self._classify_and_schedule_stream_error(
                                    account,
                                    {"message": refresh_exc.message},
                                    refresh_exc.code,
                                )
                            last_refresh_failure = (
                                account.id,
                                "invalid_api_key" if refresh_exc.is_permanent else "upstream_unavailable",
                                "Upstream credential refresh failed",
                            )
                            excluded_account_ids.add(account.id)
                            continue
                        except (aiohttp.ClientError, asyncio.TimeoutError):
                            logger.warning(
                                "Stream forced refresh/connect failed request_id=%s attempt=%s account_id=%s",
                                request_id,
                                attempt + 1,
                                account.id,
                                exc_info=True,
                            )
                            self._classify_and_schedule_stream_error(
                                account,
                                {"message": "Upstream credential refresh failed"},
                                "upstream_unavailable",
                            )
                            last_refresh_failure = (
                                account.id,
                                "upstream_unavailable",
                                "Upstream credential refresh failed",
                            )
                            excluded_account_ids.add(account.id)
                            continue
                        settlement = _StreamSettlement()
                        effective_attempt_timeout = self._remaining_budget_seconds_compatible(deadline)
                        if effective_attempt_timeout <= 0:
                            logger.warning(
                                "Proxy request budget exhausted before post-refresh stream attempt "
                                "request_id=%s attempt=%s account_id=%s",
                                request_id,
                                attempt + 1,
                                account.id,
                            )
                            self._schedule_stream_preflight_error_before_deadline(
                                deadline=deadline,
                                account_id=account.id,
                                api_key=api_key,
                                request_id=request_id,
                                model=payload.model,
                                start=start,
                                error_code="upstream_request_timeout",
                                error_message="Proxy request budget exhausted",
                                reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                                service_tier=payload.service_tier,
                                transport=request_transport,
                            )
                            transfer_local_terminal_release_ownership()
                            yield format_sse_event(_proxy_request_timeout_event(request_id))
                            return
                        stream_timeout_tokens = self._push_stream_attempt_timeout_overrides_compatible(
                            effective_attempt_timeout
                        )
                        try:
                            child_stream = self._stream_once(
                                account,
                                payload,
                                headers,
                                request_id,
                                False,
                                request_started_at=start,
                                api_key=api_key,
                                settlement=settlement,
                                suppress_text_done_events=suppress_text_done_events,
                                upstream_stream_transport=upstream_stream_transport,
                                request_transport=request_transport,
                                request_deadline_at=deadline,
                            )
                            try:
                                async for line in child_stream:
                                    terminal_event = parse_sse_event(line)
                                    terminal_event_type = _event_type_from_payload(
                                        terminal_event,
                                        parse_sse_data_json(line),
                                    )
                                    if terminal_event_type in {
                                        "response.completed",
                                        "response.failed",
                                        "response.incomplete",
                                        "error",
                                    }:
                                        transfer_terminal_reservation_ownership(account)
                                    yield line
                            finally:
                                try:
                                    await self._close_direct_stream_iterator(
                                        child_stream,
                                        label=f"post-refresh direct Responses iterator request_id={request_id}",
                                    )
                                finally:
                                    capture_attempt_usage(settlement)
                        finally:
                            self._pop_stream_timeout_overrides_compatible(stream_timeout_tokens)
                        transfer_terminal_reservation_ownership(account)
                        schedule_terminal_finalization()
                        return
                    error = _parse_openai_error(exc.payload)
                    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                    error_message = error.message if error else None
                    error_type = error.type if error else None
                    error_param = error.param if error else None
                    if _should_penalize_stream_error(error_code):
                        self._classify_and_schedule_stream_error(
                            account,
                            _upstream_error_from_openai(error),
                            error_code,
                        )
                    if propagate_http_errors:
                        raise
                    event = response_failed_event(
                        error_code,
                        error_message or "Upstream error",
                        error_type=error_type or "server_error",
                        response_id=request_id,
                        error_param=error_param,
                    )
                    _apply_error_metadata(event["response"]["error"], error)
                    transfer_terminal_reservation_ownership(account)
                    schedule_terminal_finalization()
                    yield format_sse_event(event)
                    return
                except RefreshError as exc:
                    if exc.is_permanent:
                        self._classify_and_schedule_stream_error(
                            account,
                            {"message": exc.message},
                            exc.code,
                        )
                    last_refresh_failure = (
                        account.id,
                        "invalid_api_key" if exc.is_permanent else "upstream_unavailable",
                        "Upstream credential refresh failed",
                    )
                    excluded_account_ids.add(account.id)
                    continue
                except Exception:
                    logger.warning(
                        "Proxy streaming failed without retry account_id=%s request_id=%s",
                        account_id_value,
                        request_id,
                        exc_info=True,
                    )
                    event = response_failed_event(
                        "upstream_error",
                        "Proxy streaming failed",
                        response_id=request_id,
                    )
                    transfer_local_terminal_release_ownership()
                    yield format_sse_event(event)
                    return
            # When HTTP error propagation is enabled and the last failure was
            # a transient 500, re-raise to preserve the upstream status/payload.
            if propagate_http_errors and last_transient_exc is not None:
                raise last_transient_exc
            if last_refresh_failure is not None:
                failed_account_id, failure_code, failure_message = last_refresh_failure
                transfer_local_terminal_release_ownership()
                yield format_sse_event(
                    response_failed_event(
                        failure_code,
                        failure_message,
                        response_id=request_id,
                    )
                )
                self._schedule_terminal_stream_log_before_deadline(
                    deadline=deadline,
                    account_id=failed_account_id,
                    api_key=api_key,
                    request_id=request_id,
                    payload=payload,
                    start=start,
                    error_code=failure_code,
                    error_message=failure_message,
                    request_transport=request_transport,
                )
                return
            retries_exhausted_msg = "No available accounts after retries"
            event = response_failed_event(
                "no_accounts",
                retries_exhausted_msg,
                response_id=request_id,
            )
            transfer_local_terminal_release_ownership()
            yield format_sse_event(event)
            if not any_attempt_logged:
                self._schedule_terminal_stream_log_before_deadline(
                    deadline=deadline,
                    account_id=None,
                    api_key=api_key,
                    request_id=request_id,
                    payload=payload,
                    start=start,
                    error_code="no_accounts",
                    error_message=retries_exhausted_msg,
                    request_transport=request_transport,
                )
        finally:
            schedule_terminal_finalization()
            if not settled and api_key is not None and api_key_reservation is not None:
                transfer_local_terminal_release_ownership()

    async def _stream_once(
        self: _StreamingService,
        account: Account,
        payload: ResponsesRequest,
        headers: Mapping[str, str],
        request_id: str,
        allow_retry: bool,
        *,
        request_started_at: float,
        allow_transient_retry: bool = False,
        api_key: ApiKeyData | None,
        settlement: _StreamSettlement,
        suppress_text_done_events: bool,
        upstream_stream_transport: str | None,
        request_transport: str,
        preferred_account_id: str | None = None,
        request_deadline_at: float,
    ) -> AsyncIterator[str]:
        account_id_value = account.id
        effective_payload = _payload_with_account_service_tier(payload, account, api_key=api_key)
        access_token = self._encryptor.decrypt(account.access_token_encrypted)
        account_id = _upstream_account_header_value(account)
        base_url = _account_upstream_base_url(account)
        wire_api = _account_upstream_wire_api(account)
        model = effective_payload.model
        requested_service_tier = effective_payload.service_tier
        service_tier = requested_service_tier
        actual_service_tier: str | None = None
        reasoning_effort = effective_payload.reasoning.effort if effective_payload.reasoning else None
        session_id = _owner_lookup_session_id_from_headers(headers)
        start = time.monotonic()
        status = "success"
        error_code = None
        error_message = None
        response_id = request_id
        usage = None
        saw_text_delta = False
        latency_first_token_ms: int | None = None
        response_create_lease = AdmissionLease(None)
        direct_admission_wait_started_at: float | None = None
        direct_admission_acquired_at: float | None = None
        direct_stream_created_at: float | None = None
        direct_upstream_first_event_at: float | None = None
        direct_upstream_first_event_type: str | None = None
        stream: AsyncIterator[str] | None = None
        stream_iterator_detached = False

        def mark_stream_iterator_detached() -> None:
            nonlocal stream_iterator_detached
            stream_iterator_detached = True

        def late_terminal_settlement(line: str) -> _StreamSettlement | None:
            late_payload = parse_sse_data_json(line)
            late_event = parse_sse_event(line)
            late_event_type = _event_type_from_payload(late_event, late_payload)
            if late_event_type not in {"response.completed", "response.failed", "response.incomplete"}:
                return None
            response = late_event.response if late_event is not None else None
            late_usage = response.usage if response is not None else None
            late_service_tier = _service_tier_from_event_payload(late_payload) or service_tier
            charge = _usage_charge_from_response_usage(
                late_usage,
                model=model,
                service_tier=late_service_tier,
            )
            if charge is None:
                return None
            late_settlement = _StreamSettlement(
                status="error",
                model=model,
                service_tier=late_service_tier,
                record_success=False,
                usage_charges=(charge,),
            )
            _apply_usage_charges_to_settlement(late_settlement, [charge])
            return late_settlement

        try:
            direct_admission_wait_started_at = time.monotonic()
            admission_remaining = self._remaining_budget_seconds_compatible(request_deadline_at)
            if admission_remaining <= 0:
                _raise_proxy_budget_exhausted()

            async def release_late_admission(lease: AdmissionLease) -> None:
                lease.release()

            try:
                response_create_lease = await _await_operation_before_hard_timeout(
                    self._get_work_admission().acquire_response_create(),
                    timeout_seconds=admission_remaining,
                    tasks=self._proxy_cleanup_tasks,
                    label=f"direct Responses admission request_id={request_id}",
                    late_result_cleanup=release_late_admission,
                )
            except TimeoutError:
                _raise_proxy_budget_exhausted()
            direct_admission_acquired_at = time.monotonic()
            stream = self._core_stream_responses_compatible(
                effective_payload,
                headers,
                access_token,
                account_id,
                base_url=base_url,
                wire_api=wire_api,
                raise_for_status=True,
                upstream_stream_transport_override=upstream_stream_transport,
            )
            direct_stream_created_at = time.monotonic()
            iterator = stream.__aiter__()
            try:
                first_event_remaining = self._remaining_budget_seconds_compatible(request_deadline_at)
                if first_event_remaining <= 0:
                    _raise_proxy_budget_exhausted()

                async def close_late_first_event() -> None:
                    if not late_first_event_reconciliation.done():
                        late_first_event_reconciliation.set_result(None)
                    await self._close_direct_stream_iterator(
                        iterator,
                        label="late first-event iterator",
                    )

                late_first_event_reconciliation: asyncio.Future[_StreamSettlement | None] = (
                    asyncio.get_running_loop().create_future()
                )

                def mark_first_event_detached() -> None:
                    mark_stream_iterator_detached()
                    settlement.late_usage_reconciliation = late_first_event_reconciliation

                async def capture_late_first_event(line: str) -> None:
                    if not late_first_event_reconciliation.done():
                        late_first_event_reconciliation.set_result(late_terminal_settlement(line))

                try:
                    first = await _await_operation_before_hard_timeout(
                        iterator.__anext__(),
                        timeout_seconds=first_event_remaining,
                        tasks=self._proxy_cleanup_tasks,
                        label=f"direct Responses first event request_id={request_id}",
                        late_result_cleanup=capture_late_first_event,
                        late_completion_cleanup=close_late_first_event,
                        on_detach=mark_first_event_detached,
                    )
                except TimeoutError:
                    stream_iterator_detached = True
                    _raise_proxy_budget_exhausted()
            except StopAsyncIteration:
                response_create_lease.release()
                return
            direct_upstream_first_event_at = time.monotonic()
            response_create_lease.release()
            first_payload = parse_sse_data_json(first)
            event = parse_sse_event(first)
            event_type = _event_type_from_payload(event, first_payload)
            direct_upstream_first_event_type = event_type
            event_service_tier = _service_tier_from_event_payload(first_payload)
            if event_service_tier is not None:
                actual_service_tier = event_service_tier
                service_tier = event_service_tier
            terminal_stream_error: _TerminalStreamError | None = None
            if event and event.type in ("response.failed", "error"):
                if event.type == "response.failed":
                    response = event.response
                    error = response.error if response else None
                    usage = response.usage if response else None
                else:
                    error = event.error
                response_id = (
                    event.response.id
                    if event.type == "response.failed" and event.response and event.response.id
                    else request_id
                )
                code = _normalize_error_code(
                    error.code if error else None,
                    error.type if error else None,
                )
                rewritten_error = self._rewrite_previous_response_stream_error_compatible(
                    previous_response_id=effective_payload.previous_response_id,
                    preferred_account_id=preferred_account_id,
                    error_code=code,
                    error_type=error.type if error else None,
                    error_message=error.message if error else None,
                    error_param=error.param if error else None,
                )
                status = "error"
                settlement.error = _upstream_error_from_openai(error)
                settlement.record_success = False
                if rewritten_error is not None:
                    rewritten_code, rewritten_message, upstream_error_code = rewritten_error
                    if upstream_error_code is not None:
                        self._classify_and_schedule_stream_error(
                            account,
                            settlement.error,
                            upstream_error_code,
                        )
                    first, event, first_payload, event_type = _build_rewritten_stream_response_failed_event(
                        response_id=response_id,
                        error_code=rewritten_code,
                        error_message=rewritten_message,
                    )
                    error_code = rewritten_code
                    error_message = rewritten_message
                    settlement.account_health_error = False
                else:
                    error_code = code
                    error_message = error.message if error else None
                    settlement.account_health_error = _should_penalize_stream_error(code)
                    if allow_transient_retry and code in _TRANSIENT_RETRY_CODES:
                        raise _TransientStreamError(code, settlement.error)
                    if allow_retry and (
                        _should_retry_stream_error(code)
                        or _should_failover_first_event_failure(
                            error_code=code,
                            error=settlement.error,
                            http_status=_http_error_status_from_payload(first_payload),
                        )
                    ):
                        raise _RetryableStreamError(code, settlement.error)
                terminal_stream_error = _TerminalStreamError(
                    error_code or code,
                    settlement.error,
                )
                if allow_retry:
                    logger.info(
                        "Not retrying non-recoverable stream failure request_id=%s account_id=%s code=%s",
                        request_id,
                        account_id_value,
                        error_code or code,
                    )

            if event and event.type in ("response.completed", "response.incomplete"):
                usage = event.response.usage if event.response else None
                if event.response and event.response.id:
                    response_id = event.response.id
                if event.type == "response.incomplete":
                    status = "error"

            if suppress_text_done_events and event_type in _TEXT_DELTA_EVENT_TYPES:
                saw_text_delta = True
            if not _should_suppress_text_done_event(
                event_type=event_type,
                payload=first_payload,
                suppress_text_done_events=suppress_text_done_events,
                saw_text_delta=saw_text_delta,
            ):
                if latency_first_token_ms is None and event_type in _TEXT_DELTA_EVENT_TYPES:
                    direct_first_text_at = time.monotonic()
                    latency_first_token_ms = int((direct_first_text_at - request_started_at) * 1000)
                    _log_direct_sse_latency_breakdown(
                        request_id=request_id,
                        response_id=response_id,
                        session_id=session_id,
                        account_id=account_id_value,
                        model=model,
                        event_type=event_type,
                        first_upstream_event_type=direct_upstream_first_event_type,
                        request_started_at=request_started_at,
                        stream_once_started_at=start,
                        admission_wait_started_at=direct_admission_wait_started_at,
                        admission_acquired_at=direct_admission_acquired_at,
                        stream_created_at=direct_stream_created_at,
                        upstream_first_event_at=direct_upstream_first_event_at,
                        first_text_at=direct_first_text_at,
                        request_transport=request_transport,
                    )
                yield first
            if terminal_stream_error is not None:
                raise terminal_stream_error

            async def hard_bounded_remaining_events() -> AsyncIterator[str]:
                nonlocal stream_iterator_detached

                while True:
                    event_remaining = self._remaining_budget_seconds_compatible(request_deadline_at)
                    if event_remaining <= 0:
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return
                    late_event_reconciliation: asyncio.Future[_StreamSettlement | None] = (
                        asyncio.get_running_loop().create_future()
                    )

                    def mark_stream_event_detached() -> None:
                        mark_stream_iterator_detached()
                        settlement.late_usage_reconciliation = late_event_reconciliation

                    async def capture_late_stream_event(line: str) -> None:
                        if not late_event_reconciliation.done():
                            late_event_reconciliation.set_result(late_terminal_settlement(line))

                    async def close_late_stream_event() -> None:
                        if not late_event_reconciliation.done():
                            late_event_reconciliation.set_result(None)
                        await self._close_direct_stream_iterator(
                            iterator,
                            label="late stream-event iterator",
                        )

                    try:
                        yield await _await_operation_before_hard_timeout(
                            iterator.__anext__(),
                            timeout_seconds=event_remaining,
                            tasks=self._proxy_cleanup_tasks,
                            label=f"direct Responses event request_id={request_id}",
                            late_result_cleanup=capture_late_stream_event,
                            late_completion_cleanup=close_late_stream_event,
                            on_detach=mark_stream_event_detached,
                        )
                    except StopAsyncIteration:
                        return
                    except TimeoutError:
                        stream_iterator_detached = True
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return

            async for line in hard_bounded_remaining_events():
                event_payload = parse_sse_data_json(line)
                event = parse_sse_event(line)
                event_type = _event_type_from_payload(event, event_payload)
                event_service_tier = _service_tier_from_event_payload(event_payload)
                if event_service_tier is not None:
                    actual_service_tier = event_service_tier
                    service_tier = event_service_tier
                if suppress_text_done_events and event_type in _TEXT_DELTA_EVENT_TYPES:
                    saw_text_delta = True
                if _should_suppress_text_done_event(
                    event_type=event_type,
                    payload=event_payload,
                    suppress_text_done_events=suppress_text_done_events,
                    saw_text_delta=saw_text_delta,
                ):
                    continue
                if event:
                    if event_type in ("response.failed", "error"):
                        status = "error"
                        if event_type == "response.failed":
                            response = event.response
                            error = response.error if response else None
                            usage = response.usage if response else None
                            if response and response.id:
                                response_id = response.id
                        else:
                            error = event.error
                        raw_error_code = _normalize_error_code(
                            error.code if error else None,
                            error.type if error else None,
                        )
                        rewritten_error = self._rewrite_previous_response_stream_error_compatible(
                            previous_response_id=effective_payload.previous_response_id,
                            preferred_account_id=preferred_account_id,
                            error_code=raw_error_code,
                            error_type=error.type if error else None,
                            error_message=error.message if error else None,
                            error_param=error.param if error else None,
                        )
                        if rewritten_error is not None:
                            response_id = (
                                event.response.id
                                if event_type == "response.failed" and event.response and event.response.id
                                else request_id
                            )
                            rewritten_code, rewritten_message, upstream_error_code = rewritten_error
                            if upstream_error_code is not None:
                                self._classify_and_schedule_stream_error(
                                    account,
                                    _upstream_error_from_openai(error),
                                    upstream_error_code,
                                )
                            line, event, event_payload, event_type = _build_rewritten_stream_response_failed_event(
                                response_id=response_id,
                                error_code=rewritten_code,
                                error_message=rewritten_message,
                            )
                            error_code = rewritten_code
                            error_message = rewritten_message
                            settlement.error = _upstream_error_from_openai(error)
                            settlement.record_success = False
                            settlement.account_health_error = False
                        else:
                            error_code = raw_error_code
                            error_message = error.message if error else None
                            settlement.error = _upstream_error_from_openai(error)
                            settlement.record_success = False
                            settlement.account_health_error = _should_penalize_stream_error(error_code)
                    if event_type in ("response.completed", "response.incomplete"):
                        response = event.response if event is not None else None
                        usage = response.usage if response else None
                        if response and response.id:
                            response_id = response.id
                        if event_type == "response.incomplete":
                            status = "error"
                if latency_first_token_ms is None and event_type in _TEXT_DELTA_EVENT_TYPES:
                    direct_first_text_at = time.monotonic()
                    latency_first_token_ms = int((direct_first_text_at - request_started_at) * 1000)
                    _log_direct_sse_latency_breakdown(
                        request_id=request_id,
                        response_id=response_id,
                        session_id=session_id,
                        account_id=account_id_value,
                        model=model,
                        event_type=event_type,
                        first_upstream_event_type=direct_upstream_first_event_type,
                        request_started_at=request_started_at,
                        stream_once_started_at=start,
                        admission_wait_started_at=direct_admission_wait_started_at,
                        admission_acquired_at=direct_admission_acquired_at,
                        stream_created_at=direct_stream_created_at,
                        upstream_first_event_at=direct_upstream_first_event_at,
                        first_text_at=direct_first_text_at,
                        request_transport=request_transport,
                    )
                yield line
        except ProxyResponseError as exc:
            response_create_lease.release()
            error = _parse_openai_error(exc.payload)
            rewritten_error = self._rewrite_previous_response_stream_error_compatible(
                previous_response_id=effective_payload.previous_response_id,
                preferred_account_id=preferred_account_id,
                error_code=_normalize_error_code(
                    error.code if error else None,
                    error.type if error else None,
                ),
                error_type=error.type if error else None,
                error_message=error.message if error else None,
                error_param=error.param if error else None,
            )
            if rewritten_error is not None:
                rewritten_code, rewritten_message, upstream_error_code = rewritten_error
                if upstream_error_code is not None:
                    self._classify_and_schedule_stream_error(
                        account,
                        _upstream_error_from_openai(error),
                        upstream_error_code,
                        http_status=exc.status_code,
                    )
                status = "error"
                error_code = rewritten_code
                error_message = rewritten_message
                settlement.record_success = False
                settlement.account_health_error = False
                yield _build_rewritten_stream_response_failed_event(
                    response_id=request_id,
                    error_code=rewritten_code,
                    error_message=rewritten_message,
                )[0]
                return
            status = "error"
            error_code = _normalize_error_code(
                error.code if error else None,
                error.type if error else None,
            )
            error_message = error.message if error else None
            settlement.record_success = False
            settlement.account_health_error = _should_penalize_stream_error(error_code)
            raise
        finally:
            response_create_lease.release()
            if stream is not None and not stream_iterator_detached:
                await self._close_direct_stream_iterator(
                    stream,
                    label=f"core direct Responses iterator request_id={request_id}",
                )
            input_tokens = usage.input_tokens if usage else None
            output_tokens = usage.output_tokens if usage else None
            cached_input_tokens = (
                usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else None
            )
            cache_write_tokens = (
                usage.input_tokens_details.cache_write_tokens if usage and usage.input_tokens_details else None
            )
            reasoning_tokens = (
                usage.output_tokens_details.reasoning_tokens if usage and usage.output_tokens_details else None
            )
            settlement.status = status
            settlement.model = model
            settlement.service_tier = service_tier
            settlement.input_tokens = input_tokens
            settlement.output_tokens = output_tokens
            settlement.cached_input_tokens = cached_input_tokens
            settlement.cache_write_tokens = cache_write_tokens
            usage_charge = _usage_charge_from_response_usage(
                usage,
                model=model,
                service_tier=service_tier,
            )
            settlement.usage_charges = (usage_charge,) if usage_charge is not None else ()
            settlement.error_code = error_code
            settlement.error_message = error_message

            async def write_request_log_before_deadline() -> None:
                log_remaining = request_deadline_at - time.monotonic()
                if log_remaining <= 0:
                    logger.warning(
                        "Skipped direct Responses request log after deadline request_id=%s",
                        response_id,
                    )
                    return
                try:
                    await _await_operation_before_hard_timeout(
                        self._write_request_log(
                            account_id=account_id_value,
                            api_key=api_key,
                            request_id=response_id,
                            model=model,
                            latency_ms=int((time.monotonic() - start) * 1000),
                            status=status,
                            error_code=error_code,
                            error_message=error_message,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_input_tokens=cached_input_tokens,
                            cache_write_tokens=cache_write_tokens,
                            reasoning_tokens=reasoning_tokens,
                            reasoning_effort=reasoning_effort,
                            transport=request_transport,
                            service_tier=service_tier,
                            requested_service_tier=requested_service_tier,
                            actual_service_tier=actual_service_tier,
                            latency_first_token_ms=latency_first_token_ms,
                            session_id=session_id,
                        ),
                        timeout_seconds=min(log_remaining, _STREAM_TERMINAL_LOG_TIMEOUT_SECONDS),
                        tasks=self._proxy_cleanup_tasks,
                        label=f"direct Responses request log request_id={response_id}",
                    )
                except Exception:
                    logger.warning(
                        "Failed to write direct Responses request log before deadline request_id=%s",
                        response_id,
                        exc_info=True,
                    )

            _schedule_tracked_background_task(
                self._proxy_cleanup_tasks,
                write_request_log_before_deadline(),
                name=f"direct-responses-log-{response_id}-{time.monotonic_ns()}",
                label=f"direct Responses request log request_id={response_id}",
            )
            _maybe_log_proxy_service_tier_trace(
                "stream",
                requested_service_tier=requested_service_tier,
                actual_service_tier=actual_service_tier,
                settings=self._proxy_runtime_settings(),
            )
