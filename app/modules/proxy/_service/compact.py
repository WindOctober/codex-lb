from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Protocol

import aiohttp

from app.core.auth.refresh import RefreshError
from app.core.balancer import failover_decision
from app.core.balancer.types import ClassifiedFailure, UpstreamError
from app.core.clients.proxy import (
    ProxyResponseError,
    filter_inbound_headers,
    pop_compact_timeout_overrides,
    push_compact_timeout_overrides,
)
from app.core.config.settings import Settings
from app.core.crypto import TokenEncryptor
from app.core.errors import openai_error
from app.core.openai.models import CompactResponsePayload
from app.core.openai.requests import ResponsesCompactRequest
from app.core.utils.request_id import ensure_request_id, get_request_id
from app.core.utils.retry import backoff_seconds
from app.db.models import Account, DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.affinity import (
    _prompt_cache_key_from_request_model,
    _sticky_key_for_compact_request,
)
from app.modules.proxy._service.budget import (
    _raise_proxy_budget_exhausted,
    _raise_proxy_unavailable,
)
from app.modules.proxy._service.observability import (
    _maybe_log_proxy_request_payload,
    _maybe_log_proxy_request_shape,
    _maybe_log_proxy_service_tier_trace,
)
from app.modules.proxy._service.service_tier import (
    _effective_service_tier,
    _payload_with_account_service_tier,
    _service_tier_from_compact_payload,
    _service_tier_from_response,
)
from app.modules.proxy._service.support import (
    _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES,
    _REQUEST_TRANSPORT_HTTP,
    _is_account_neutral_error_code,
    _routing_strategy,
)
from app.modules.proxy._service.upstream_account import (
    _account_upstream_base_url,
    _account_upstream_wire_api,
    _upstream_account_header_value,
)
from app.modules.proxy.helpers import (
    _normalize_error_code,
    _parse_openai_error,
    _upstream_error_from_openai,
)
from app.modules.proxy.load_balancer import AccountSelection, LoadBalancer
from app.modules.proxy.work_admission import WorkAdmissionController

logger = logging.getLogger("app.modules.proxy.service")

_COMPACT_SAME_CONTRACT_RETRY_BUDGET = 1
_COMPACT_MAX_ACCOUNT_ATTEMPTS = 2


class _CompactRuntimeService(Protocol):
    _encryptor: TokenEncryptor
    _load_balancer: LoadBalancer

    def _proxy_runtime_settings(self) -> Settings: ...

    async def _proxy_dashboard_settings(self) -> DashboardSettings: ...

    def _get_work_admission(self) -> WorkAdmissionController: ...

    @staticmethod
    def _remaining_budget_seconds_compatible(deadline: float) -> float: ...

    async def _select_account_with_budget_compatible(
        self,
        deadline: float,
        **kwargs: object,
    ) -> AccountSelection: ...

    async def _ensure_fresh_with_budget(
        self,
        account: Account,
        *,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> Account: ...

    async def _settle_compact_api_key_usage(
        self,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        response: CompactResponsePayload | None,
        request_service_tier: str | None,
    ) -> None: ...

    async def _handle_proxy_error(self, account: Account, exc: ProxyResponseError) -> None: ...

    async def _handle_stream_error(
        self,
        account: Account,
        error: UpstreamError,
        code: str,
        http_status: int | None = None,
    ) -> ClassifiedFailure: ...

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

    async def _core_compact_responses_compatible(
        self,
        payload: ResponsesCompactRequest,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
    ) -> CompactResponsePayload: ...


class _CompactRuntimeMixin:
    async def compact_responses(
        self: _CompactRuntimeService,
        payload: ResponsesCompactRequest,
        headers: Mapping[str, str],
        *,
        codex_session_affinity: bool = False,
        openai_cache_affinity: bool = False,
        api_key: ApiKeyData | None = None,
        api_key_reservation: ApiKeyUsageReservationData | None = None,
    ) -> CompactResponsePayload:
        base_settings = self._proxy_runtime_settings()
        _maybe_log_proxy_request_payload("compact", payload, headers, settings=base_settings)
        filtered = filter_inbound_headers(headers)
        request_id = get_request_id() or ensure_request_id(None)
        start = time.monotonic()
        deadline = start + base_settings.compact_request_budget_seconds
        account_id_value: str | None = None
        log_status = "error"
        log_error_code: str | None = None
        log_error_message: str | None = None
        response: CompactResponsePayload | None = None
        request_service_tier: str | None = None
        actual_service_tier: str | None = None
        settings = await self._proxy_dashboard_settings()
        prefer_earlier_reset = settings.prefer_earlier_reset_accounts
        had_prompt_cache_key = _prompt_cache_key_from_request_model(payload) is not None
        affinity = _sticky_key_for_compact_request(
            payload,
            headers,
            codex_session_affinity=codex_session_affinity,
            openai_cache_affinity=openai_cache_affinity,
            openai_cache_affinity_max_age_seconds=settings.openai_cache_affinity_max_age_seconds,
            sticky_threads_enabled=settings.sticky_threads_enabled,
            api_key=api_key,
            settings=base_settings,
        )
        sticky_key_source = "none"
        if affinity.kind == StickySessionKind.CODEX_SESSION:
            sticky_key_source = "session_header"
        elif affinity.key:
            sticky_key_source = "payload" if had_prompt_cache_key else "derived"
        _maybe_log_proxy_request_shape(
            "compact",
            payload,
            headers,
            settings=base_settings,
            sticky_kind=affinity.kind.value if affinity.kind is not None else None,
            sticky_key_source=sticky_key_source,
            prompt_cache_key_set=_prompt_cache_key_from_request_model(payload) is not None,
        )
        routing_strategy = _routing_strategy(settings)
        try:

            async def _call_compact(
                target: Account,
                effective_payload: ResponsesCompactRequest,
            ) -> CompactResponsePayload:
                access_token = self._encryptor.decrypt(target.access_token_encrypted)
                account_id = _upstream_account_header_value(target)
                base_url = _account_upstream_base_url(target)
                wire_api = _account_upstream_wire_api(target)
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    logger.warning(
                        "Compact request budget exhausted before upstream call request_id=%s account_id=%s",
                        request_id,
                        target.id,
                    )
                    _raise_proxy_budget_exhausted()
                if wire_api != "codex":
                    raise ProxyResponseError(
                        501,
                        openai_error(
                            "not_implemented",
                            "responses/compact is not supported by this upstream provider",
                            error_type="server_error",
                        ),
                    )
                if base_settings.upstream_compact_timeout_seconds is None:
                    timeout_tokens = push_compact_timeout_overrides(
                        connect_timeout_seconds=remaining_budget,
                    )
                else:
                    timeout_tokens = push_compact_timeout_overrides(
                        connect_timeout_seconds=remaining_budget,
                        total_timeout_seconds=remaining_budget,
                    )
                try:
                    create_lease = await self._get_work_admission().acquire_response_create(compact=True)
                    try:
                        return await self._core_compact_responses_compatible(
                            effective_payload,
                            filtered,
                            access_token,
                            account_id,
                            base_url=base_url,
                            wire_api=wire_api,
                        )
                    finally:
                        create_lease.release()
                finally:
                    pop_compact_timeout_overrides(timeout_tokens)

            last_exc: ProxyResponseError | None = None
            excluded_account_ids: set[str] = set()
            for account_attempt in range(_COMPACT_MAX_ACCOUNT_ATTEMPTS):
                selection = await self._select_account_with_budget_compatible(
                    deadline,
                    request_id=request_id,
                    kind="compact",
                    api_key=api_key,
                    sticky_key=affinity.key,
                    sticky_kind=affinity.kind,
                    reallocate_sticky=affinity.reallocate_sticky,
                    sticky_max_age_seconds=affinity.max_age_seconds,
                    prefer_earlier_reset_accounts=prefer_earlier_reset,
                    routing_strategy=routing_strategy,
                    model=payload.model,
                    exclude_account_ids=excluded_account_ids,
                )
                account = selection.account
                if not account:
                    if last_exc is not None:
                        raise last_exc
                    log_error_code = selection.error_code or "no_accounts"
                    log_error_message = selection.error_message or "No active accounts available"
                    raise ProxyResponseError(
                        503,
                        openai_error(log_error_code, log_error_message),
                    )
                account_id_value = account.id
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    logger.warning("Compact request budget exhausted before freshness check request_id=%s", request_id)
                    _raise_proxy_budget_exhausted()
                try:
                    account = await self._ensure_fresh_with_budget(account, timeout_seconds=remaining_budget)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "Compact refresh/connect failed request_id=%s account_id=%s",
                        request_id,
                        account.id,
                        exc_info=True,
                    )
                    _raise_proxy_unavailable(str(exc) or "Request to upstream timed out")
                effective_payload = _payload_with_account_service_tier(payload, account, api_key=api_key)
                request_service_tier = _service_tier_from_compact_payload(effective_payload)

                safe_retry_budget = _COMPACT_SAME_CONTRACT_RETRY_BUDGET
                transient_retries = 0
                refresh_retry_used = False
                transient_exhausted = False
                while True:
                    try:
                        response = await _call_compact(account, effective_payload)
                        actual_service_tier = _service_tier_from_response(response)
                        await self._load_balancer.record_success(account)
                        await self._settle_compact_api_key_usage(
                            api_key=api_key,
                            api_key_reservation=api_key_reservation,
                            response=response,
                            request_service_tier=request_service_tier,
                        )
                        log_status = "success"
                        return response
                    except ProxyResponseError as exc:
                        if exc.status_code == 401:
                            if refresh_retry_used:
                                await self._settle_compact_api_key_usage(
                                    api_key=api_key,
                                    api_key_reservation=api_key_reservation,
                                    response=None,
                                    request_service_tier=request_service_tier,
                                )
                                await self._handle_proxy_error(account, exc)
                                raise
                            try:
                                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                                if remaining_budget <= 0:
                                    logger.warning(
                                        "Compact request budget exhausted before forced refresh retry request_id=%s "
                                        "account_id=%s",
                                        request_id,
                                        account.id,
                                    )
                                    _raise_proxy_budget_exhausted()
                                account = await self._ensure_fresh_with_budget(
                                    account,
                                    force=True,
                                    timeout_seconds=remaining_budget,
                                )
                            except RefreshError as refresh_exc:
                                if refresh_exc.is_permanent:
                                    await self._load_balancer.mark_permanent_failure(account, refresh_exc.code)
                                await self._settle_compact_api_key_usage(
                                    api_key=api_key,
                                    api_key_reservation=api_key_reservation,
                                    response=None,
                                    request_service_tier=request_service_tier,
                                )
                                raise exc
                            except (aiohttp.ClientError, asyncio.TimeoutError) as timeout_exc:
                                await self._settle_compact_api_key_usage(
                                    api_key=api_key,
                                    api_key_reservation=api_key_reservation,
                                    response=None,
                                    request_service_tier=request_service_tier,
                                )
                                logger.warning(
                                    "Compact forced refresh/connect failed request_id=%s account_id=%s",
                                    request_id,
                                    account.id,
                                    exc_info=True,
                                )
                                _raise_proxy_unavailable(str(timeout_exc) or "Request to upstream timed out")
                            refresh_retry_used = True
                            continue
                        if exc.status_code == 500:
                            transient_retries += 1
                            if (
                                transient_retries < _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES
                                and self._remaining_budget_seconds_compatible(deadline) > 0
                            ):
                                delay = backoff_seconds(transient_retries)
                                logger.info(
                                    "Transient compact error, retrying same account "
                                    "request_id=%s account_id=%s retry=%s/%s delay=%.2fs",
                                    request_id,
                                    account.id,
                                    transient_retries,
                                    _MAX_TRANSIENT_SAME_ACCOUNT_RETRIES,
                                    delay,
                                )
                                await asyncio.sleep(delay)
                                continue
                            logger.warning(
                                "Compact transient retries exhausted for account "
                                "request_id=%s account_id=%s retries=%s code=server_error",
                                request_id,
                                account.id,
                                transient_retries,
                            )
                            await self._handle_proxy_error(account, exc)
                            await self._load_balancer.record_errors(account, transient_retries - 1)
                            last_exc = exc
                            excluded_account_ids.add(account.id)
                            transient_exhausted = True
                            break
                        if exc.retryable_same_contract and safe_retry_budget > 0:
                            safe_retry_budget -= 1
                            continue
                        error = _parse_openai_error(exc.payload)
                        code = _normalize_error_code(
                            error.code if error else None,
                            error.type if error else None,
                        )
                        if _is_account_neutral_error_code(code):
                            await self._settle_compact_api_key_usage(
                                api_key=api_key,
                                api_key_reservation=api_key_reservation,
                                response=None,
                                request_service_tier=request_service_tier,
                            )
                            raise
                        classified = await self._handle_stream_error(
                            account,
                            _upstream_error_from_openai(error),
                            code,
                            http_status=exc.status_code,
                        )
                        if getattr(base_settings, "deterministic_failover_enabled", True):
                            action = failover_decision(
                                failure_class=classified["failure_class"],
                                downstream_visible=False,
                                candidates_remaining=_COMPACT_MAX_ACCOUNT_ATTEMPTS - account_attempt - 1,
                            )
                        else:
                            action = "surface"
                        logger.info(
                            "Failover decision request_id=%s transport=compact account_id=%s "
                            "attempt=%d failure_class=%s action=%s",
                            request_id,
                            account.id,
                            account_attempt + 1,
                            classified["failure_class"],
                            action,
                        )
                        if action == "failover_next":
                            last_exc = exc
                            excluded_account_ids.add(account.id)
                            transient_exhausted = True
                            break
                        await self._settle_compact_api_key_usage(
                            api_key=api_key,
                            api_key_reservation=api_key_reservation,
                            response=None,
                            request_service_tier=request_service_tier,
                        )
                        raise
                if transient_exhausted:
                    continue
            await self._settle_compact_api_key_usage(
                api_key=api_key,
                api_key_reservation=api_key_reservation,
                response=None,
                request_service_tier=request_service_tier,
            )
            if last_exc is not None:
                raise last_exc
            raise ProxyResponseError(
                502,
                openai_error("upstream_unavailable", "All account attempts exhausted"),
            )
        except ProxyResponseError as exc:
            error = _parse_openai_error(exc.payload)
            log_error_code = log_error_code or _normalize_error_code(
                error.code if error else None,
                error.type if error else None,
            )
            log_error_message = log_error_message or (error.message if error else None)
            raise
        finally:
            usage = response.usage if response else None
            reasoning_effort = payload.reasoning.effort if payload.reasoning else None
            await self._write_request_log(
                account_id=account_id_value,
                api_key=api_key,
                request_id=request_id,
                model=payload.model,
                latency_ms=int((time.monotonic() - start) * 1000),
                status=log_status,
                error_code=log_error_code,
                error_message=log_error_message,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cached_input_tokens=(
                    usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else None
                ),
                reasoning_tokens=(
                    usage.output_tokens_details.reasoning_tokens if usage and usage.output_tokens_details else None
                ),
                reasoning_effort=reasoning_effort,
                transport=_REQUEST_TRANSPORT_HTTP,
                service_tier=_effective_service_tier(request_service_tier, actual_service_tier),
                requested_service_tier=request_service_tier,
                actual_service_tier=actual_service_tier,
            )
            _maybe_log_proxy_service_tier_trace(
                "compact",
                requested_service_tier=request_service_tier,
                actual_service_tier=actual_service_tier,
                settings=base_settings,
            )
