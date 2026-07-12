from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Protocol

import aiohttp

from app.core.auth.refresh import RefreshError
from app.core.balancer import RoutingStrategy
from app.core.clients.proxy import (
    ProxyResponseError,
    filter_inbound_headers,
    pop_transcribe_timeout_overrides,
    push_transcribe_timeout_overrides,
)
from app.core.config.settings import Settings
from app.core.crypto import TokenEncryptor
from app.core.errors import openai_error
from app.core.types import JsonValue
from app.core.utils.request_id import ensure_request_id, get_request_id
from app.db.models import Account, DashboardSettings
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.budget import (
    _raise_proxy_budget_exhausted,
    _raise_proxy_unavailable,
)
from app.modules.proxy._service.support import _REQUEST_TRANSPORT_HTTP, _routing_strategy
from app.modules.proxy._service.upstream_account import (
    _account_upstream_base_url,
    _upstream_account_header_value,
)
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error
from app.modules.proxy.load_balancer import AccountSelection, LoadBalancer

logger = logging.getLogger("app.modules.proxy.service")


class _TranscriptionRuntimeService(Protocol):
    _encryptor: TokenEncryptor
    _load_balancer: LoadBalancer

    def _proxy_runtime_settings(self) -> Settings: ...

    async def _proxy_dashboard_settings(self) -> DashboardSettings: ...

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

    async def _handle_proxy_error(self, account: Account, exc: ProxyResponseError) -> None: ...

    async def _write_request_log(
        self,
        *,
        account_id: str | None,
        api_key: ApiKeyData | None,
        request_id: str,
        model: str | None,
        latency_ms: int,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        transport: str | None = None,
    ) -> None: ...

    async def _core_transcribe_audio_compatible(
        self,
        audio_bytes: bytes,
        *,
        filename: str,
        content_type: str | None,
        prompt: str | None,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        base_url: str | None,
    ) -> dict[str, JsonValue]: ...


class _TranscriptionRuntimeMixin:
    async def transcribe(
        self: _TranscriptionRuntimeService,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
        prompt: str | None,
        headers: Mapping[str, str],
        api_key: ApiKeyData | None = None,
    ) -> dict[str, JsonValue]:
        filtered = filter_inbound_headers(headers)
        request_id = get_request_id() or ensure_request_id(None)
        start = time.monotonic()
        base_settings = self._proxy_runtime_settings()
        deadline = start + base_settings.transcription_request_budget_seconds
        account_id_value: str | None = None
        log_status = "error"
        log_error_code: str | None = None
        log_error_message: str | None = None
        transcribe_model = "gpt-4o-transcribe"

        settings = await self._proxy_dashboard_settings()
        prefer_earlier_reset = settings.prefer_earlier_reset_accounts
        routing_strategy: RoutingStrategy = _routing_strategy(settings)
        try:
            selection = await self._select_account_with_budget_compatible(
                deadline,
                request_id=request_id,
                kind="transcribe",
                api_key=api_key,
                prefer_earlier_reset_accounts=prefer_earlier_reset,
                routing_strategy=routing_strategy,
                model=None,
            )
            account = selection.account
            if not account:
                log_error_code = selection.error_code or "no_accounts"
                log_error_message = selection.error_message or "No active accounts available"
                raise ProxyResponseError(
                    503,
                    openai_error(log_error_code, log_error_message),
                )
            account_id_value = account.id

            async def _call_transcribe(target: Account) -> dict[str, JsonValue]:
                access_token = self._encryptor.decrypt(target.access_token_encrypted)
                account_id = _upstream_account_header_value(target)
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    logger.warning(
                        "Transcription request budget exhausted before upstream call request_id=%s account_id=%s",
                        request_id,
                        target.id,
                    )
                    _raise_proxy_budget_exhausted()
                timeout_tokens = push_transcribe_timeout_overrides(
                    connect_timeout_seconds=remaining_budget,
                    total_timeout_seconds=remaining_budget,
                )
                try:
                    return await self._core_transcribe_audio_compatible(
                        audio_bytes,
                        filename=filename,
                        content_type=content_type,
                        prompt=prompt,
                        headers=filtered,
                        access_token=access_token,
                        account_id=account_id,
                        base_url=_account_upstream_base_url(target),
                    )
                finally:
                    pop_transcribe_timeout_overrides(timeout_tokens)

            try:
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    logger.warning(
                        "Transcription request budget exhausted before freshness check request_id=%s",
                        request_id,
                    )
                    _raise_proxy_budget_exhausted()
                try:
                    account = await self._ensure_fresh_with_budget(account, timeout_seconds=remaining_budget)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "Transcription refresh/connect failed request_id=%s account_id=%s",
                        request_id,
                        account.id,
                        exc_info=True,
                    )
                    _raise_proxy_unavailable(str(exc) or "Request to upstream timed out")
                result = await _call_transcribe(account)
                await self._load_balancer.record_success(account)
                log_status = "success"
                return result
            except RefreshError as refresh_exc:
                if refresh_exc.is_permanent:
                    await self._load_balancer.mark_permanent_failure(account, refresh_exc.code)
                raise ProxyResponseError(
                    401,
                    openai_error(
                        "invalid_api_key",
                        refresh_exc.message,
                        error_type="invalid_request_error",
                    ),
                ) from refresh_exc
            except ProxyResponseError as exc:
                if exc.status_code != 401:
                    await self._handle_proxy_error(account, exc)
                    raise
                try:
                    remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                    if remaining_budget <= 0:
                        logger.warning(
                            "Transcription request budget exhausted before forced refresh retry "
                            "request_id=%s account_id=%s",
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
                    raise exc
                except (aiohttp.ClientError, asyncio.TimeoutError) as timeout_exc:
                    logger.warning(
                        "Transcription forced refresh/connect failed request_id=%s account_id=%s",
                        request_id,
                        account.id,
                        exc_info=True,
                    )
                    _raise_proxy_unavailable(str(timeout_exc) or "Request to upstream timed out")
                try:
                    result = await _call_transcribe(account)
                    await self._load_balancer.record_success(account)
                    log_status = "success"
                    return result
                except ProxyResponseError as retry_exc:
                    await self._handle_proxy_error(account, retry_exc)
                    raise
        except ProxyResponseError as exc:
            error = _parse_openai_error(exc.payload)
            log_error_code = log_error_code or _normalize_error_code(
                error.code if error else None,
                error.type if error else None,
            )
            log_error_message = log_error_message or (error.message if error else None)
            raise
        finally:
            await self._write_request_log(
                account_id=account_id_value,
                api_key=api_key,
                request_id=request_id,
                model=transcribe_model,
                latency_ms=int((time.monotonic() - start) * 1000),
                status=log_status,
                error_code=log_error_code,
                error_message=log_error_message,
                transport=_REQUEST_TRANSPORT_HTTP,
            )
