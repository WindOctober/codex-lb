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
from app.modules.api_keys.service import ApiKeyData, ApiKeysService, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _owner_lookup_session_id_from_headers,
    _prompt_cache_key_from_request_model,
    _sticky_key_for_responses_request,
)
from app.modules.proxy._service.budget import (
    _proxy_request_timeout_event,
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
    _event_type_from_payload,
    _is_account_neutral_error_code,
    _RetryableStreamError,
    _routing_strategy,
    _should_penalize_stream_error,
    _stream_settlement_error_payload,
    _StreamSettlement,
    _TerminalStreamError,
    _TransientStreamError,
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
_TRANSIENT_RETRY_CODES = frozenset({"server_error"})
_TEXT_DELTA_EVENT_TYPES = frozenset({"response.output_text.delta", "response.refusal.delta"})
_TEXT_DONE_CONTENT_PART_TYPES = frozenset({"output_text", "refusal"})


def _resolve_upstream_stream_transport(upstream_stream_transport: str) -> str | None:
    if upstream_stream_transport == "default":
        return None
    return upstream_stream_transport


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

    @staticmethod
    def _core_stream_responses_compatible(
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

    async def _settle_stream_api_key_usage(
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
    ) -> AsyncIterator[str]: ...


class _StreamingMixin:
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
    ) -> AsyncIterator[str]:
        request_id = ensure_request_id()
        start = time.monotonic()
        base_settings = self._proxy_runtime_settings()
        settings = await self._proxy_dashboard_settings()
        deadline = start + base_settings.proxy_request_budget_seconds
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
        settled = False
        any_attempt_logged = False
        settlement = _StreamSettlement()
        last_transient_exc: ProxyResponseError | None = None
        excluded_account_ids: set[str] = set()
        preferred_account_id: str | None = None
        require_preferred_account = False
        try:
            if payload.previous_response_id is not None:
                preferred_account_id = await self._resolve_websocket_previous_response_owner(
                    previous_response_id=payload.previous_response_id,
                    api_key=api_key,
                    session_id=_owner_lookup_session_id_from_headers(headers),
                    surface="http_stream",
                )
                require_preferred_account = preferred_account_id is not None
            for attempt in range(max_attempts):
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    logger.warning(
                        "Proxy request budget exhausted before retry request_id=%s attempt=%s",
                        request_id,
                        attempt + 1,
                    )
                    await self._write_stream_preflight_error(
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
                    )
                except ProxyResponseError as exc:
                    error = _parse_openai_error(exc.payload)
                    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                    error_message = error.message if error else None
                    if error_code == "upstream_unavailable" and error_message == "Proxy request budget exhausted":
                        await self._write_stream_preflight_error(
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
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return
                    event = response_failed_event(
                        error_code,
                        error_message or "Upstream unavailable",
                        error_type=(error.type or "server_error") if error else "server_error",
                        response_id=request_id,
                    )
                    _apply_error_metadata(event["response"]["error"], error)
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
                        yield format_sse_event(event)
                        await self._write_request_log(
                            account_id=preferred_account_id,
                            api_key=api_key,
                            request_id=request_id,
                            model=payload.model,
                            latency_ms=int((time.monotonic() - start) * 1000),
                            status="error",
                            error_code="upstream_unavailable",
                            error_message=message,
                            reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                            transport=request_transport,
                            service_tier=payload.service_tier,
                            requested_service_tier=payload.service_tier,
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
                    yield format_sse_event(event)
                    await self._write_request_log(
                        account_id=None,
                        api_key=api_key,
                        request_id=request_id,
                        model=payload.model,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        status="error",
                        error_code=error_code,
                        error_message=no_accounts_msg,
                        reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                        transport=request_transport,
                        service_tier=payload.service_tier,
                        requested_service_tier=payload.service_tier,
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
                    yield format_sse_event(event)
                    await self._write_request_log(
                        account_id=preferred_account_id,
                        api_key=api_key,
                        request_id=request_id,
                        model=payload.model,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        status="error",
                        error_code="upstream_unavailable",
                        error_message=message,
                        reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                        transport=request_transport,
                        service_tier=payload.service_tier,
                        requested_service_tier=payload.service_tier,
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
                        await self._write_stream_preflight_error(
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
                        yield format_sse_event(_proxy_request_timeout_event(request_id))
                        return
                    try:
                        account = await self._ensure_fresh_with_budget(account, timeout_seconds=remaining_budget)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        logger.warning(
                            "Stream refresh/connect failed request_id=%s attempt=%s account_id=%s",
                            request_id,
                            attempt + 1,
                            account.id,
                            exc_info=True,
                        )
                        message = str(exc) or "Request to upstream timed out"
                        await self._write_stream_preflight_error(
                            account_id=account.id,
                            api_key=api_key,
                            request_id=request_id,
                            model=payload.model,
                            start=start,
                            error_code="upstream_unavailable",
                            error_message=message,
                            reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                            service_tier=payload.service_tier,
                            transport=request_transport,
                        )
                        event = response_failed_event(
                            "upstream_unavailable",
                            message,
                            response_id=request_id,
                        )
                        yield format_sse_event(event)
                        return
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
                        await self._write_stream_preflight_error(
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
                            async for line in self._stream_once(
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
                            ):
                                yield line
                        except (_TransientStreamError, ProxyResponseError) as tex:
                            if isinstance(tex, ProxyResponseError) and tex.status_code != 500:
                                error = _parse_openai_error(tex.payload)
                                code = _normalize_error_code(
                                    error.code if error else None,
                                    error.type if error else None,
                                )
                                if _is_account_neutral_error_code(code):
                                    raise
                                classified = await self._handle_stream_error(
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
                                delay = backoff_seconds(transient_retries)
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
                            await self._handle_stream_error(account, error_payload, error_code)
                            # Record remaining errors so total equals transient_retries,
                            # meeting the load balancer backoff threshold (error_count >= 3).
                            await self._load_balancer.record_errors(account, transient_retries - 1)
                            # Preserve last ProxyResponseError for propagate_http_errors path.
                            if isinstance(tex, ProxyResponseError):
                                last_transient_exc = tex
                            excluded_account_ids.add(account.id)
                            break  # outer loop: select different account
                        finally:
                            self._pop_stream_timeout_overrides_compatible(stream_timeout_tokens)
                        if settlement.account_health_error:
                            await self._handle_stream_error(
                                account,
                                _stream_settlement_error_payload(settlement),
                                settlement.error_code or "upstream_error",
                            )
                        elif settlement.record_success:
                            await self._load_balancer.record_success(account)
                        settled = await self._settle_stream_api_key_usage(
                            api_key,
                            api_key_reservation,
                            settlement,
                            request_id,
                        )
                        return
                    continue  # outer loop: account failover after transient exhaustion
                except _RetryableStreamError as exc:
                    await self._handle_stream_error(account, exc.error, exc.code)
                    excluded_account_ids.add(account.id)
                    continue
                except _TerminalStreamError as exc:
                    if _should_penalize_stream_error(exc.code):
                        await self._handle_stream_error(account, exc.error, exc.code)
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
                            await self._write_stream_preflight_error(
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
                                await self._load_balancer.mark_permanent_failure(account, refresh_exc.code)
                            continue
                        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                            logger.warning(
                                "Stream forced refresh/connect failed request_id=%s attempt=%s account_id=%s",
                                request_id,
                                attempt + 1,
                                account.id,
                                exc_info=True,
                            )
                            message = str(exc) or "Request to upstream timed out"
                            await self._write_stream_preflight_error(
                                account_id=account.id,
                                api_key=api_key,
                                request_id=request_id,
                                model=payload.model,
                                start=start,
                                error_code="upstream_unavailable",
                                error_message=message,
                                reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                                service_tier=payload.service_tier,
                                transport=request_transport,
                            )
                            event = response_failed_event(
                                "upstream_unavailable",
                                message,
                                response_id=request_id,
                            )
                            yield format_sse_event(event)
                            return
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
                            await self._write_stream_preflight_error(
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
                            yield format_sse_event(_proxy_request_timeout_event(request_id))
                            return
                        stream_timeout_tokens = self._push_stream_attempt_timeout_overrides_compatible(
                            effective_attempt_timeout
                        )
                        try:
                            async for line in self._stream_once(
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
                            ):
                                yield line
                        finally:
                            self._pop_stream_timeout_overrides_compatible(stream_timeout_tokens)
                        if settlement.account_health_error:
                            await self._handle_stream_error(
                                account,
                                _stream_settlement_error_payload(settlement),
                                settlement.error_code or "upstream_error",
                            )
                        elif settlement.record_success:
                            await self._load_balancer.record_success(account)
                        settled = await self._settle_stream_api_key_usage(
                            api_key,
                            api_key_reservation,
                            settlement,
                            request_id,
                        )
                        return
                    error = _parse_openai_error(exc.payload)
                    error_code = _normalize_error_code(error.code if error else None, error.type if error else None)
                    error_message = error.message if error else None
                    error_type = error.type if error else None
                    error_param = error.param if error else None
                    if _should_penalize_stream_error(error_code):
                        await self._handle_stream_error(
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
                    yield format_sse_event(event)
                    return
                except RefreshError as exc:
                    if exc.is_permanent:
                        await self._load_balancer.mark_permanent_failure(account, exc.code)
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
                    yield format_sse_event(event)
                    return
            # When HTTP error propagation is enabled and the last failure was
            # a transient 500, re-raise to preserve the upstream status/payload.
            if propagate_http_errors and last_transient_exc is not None:
                raise last_transient_exc
            retries_exhausted_msg = "No available accounts after retries"
            event = response_failed_event(
                "no_accounts",
                retries_exhausted_msg,
                response_id=request_id,
            )
            yield format_sse_event(event)
            if not any_attempt_logged:
                await self._write_request_log(
                    account_id=None,
                    api_key=api_key,
                    request_id=request_id,
                    model=payload.model,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    status="error",
                    error_code="no_accounts",
                    error_message=retries_exhausted_msg,
                    reasoning_effort=payload.reasoning.effort if payload.reasoning else None,
                    transport=request_transport,
                    service_tier=payload.service_tier,
                    requested_service_tier=payload.service_tier,
                )
        finally:
            if not settled and api_key is not None and api_key_reservation is not None:
                with anyio.CancelScope(shield=True):
                    try:
                        async with self._repo_factory() as repos:
                            api_keys_service = ApiKeysService(repos.api_keys)
                            await api_keys_service.release_usage_reservation(
                                api_key_reservation.reservation_id,
                            )
                    except Exception:
                        logger.warning(
                            "Failed to release stream API key reservation key_id=%s request_id=%s",
                            api_key.id,
                            request_id,
                            exc_info=True,
                        )

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

        try:
            direct_admission_wait_started_at = time.monotonic()
            response_create_lease = await self._get_work_admission().acquire_response_create()
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
                first = await iterator.__anext__()
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
                        await self._handle_stream_error(
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

            async for line in iterator:
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
                                await self._handle_stream_error(
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
                    await self._handle_stream_error(
                        account,
                        _upstream_error_from_openai(error),
                        upstream_error_code,
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
            input_tokens = usage.input_tokens if usage else None
            output_tokens = usage.output_tokens if usage else None
            cached_input_tokens = (
                usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else None
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
            settlement.error_code = error_code
            settlement.error_message = error_message
            await self._write_request_log(
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
                reasoning_tokens=reasoning_tokens,
                reasoning_effort=reasoning_effort,
                transport=request_transport,
                service_tier=service_tier,
                requested_service_tier=requested_service_tier,
                actual_service_tier=actual_service_tier,
                latency_first_token_ms=latency_first_token_ms,
                session_id=session_id,
            )
            _maybe_log_proxy_service_tier_trace(
                "stream",
                requested_service_tier=requested_service_tier,
                actual_service_tier=actual_service_tier,
                settings=self._proxy_runtime_settings(),
            )
