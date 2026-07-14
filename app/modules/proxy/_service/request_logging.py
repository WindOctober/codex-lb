from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

import anyio

from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _normalize_session_id
from app.modules.proxy._service.support import _REQUEST_TRANSPORT_HTTP
from app.modules.proxy.repo_bundle import ProxyRepoFactory

logger = logging.getLogger("app.modules.proxy.service")


class _RequestLoggingService(Protocol):
    _repo_factory: ProxyRepoFactory

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


class _RequestLoggingMixin:
    async def rewrite_request_log_model(
        self: _RequestLoggingService,
        request_id: str,
        model: str,
    ) -> None:
        """Rewrite the model recorded for a completed proxied request."""
        if not request_id or not model:
            return
        with anyio.CancelScope(shield=True):
            try:
                rowcount = 0
                for delay in (0.0, 0.05, 0.1, 0.2, 0.4, 0.8):
                    if delay > 0:
                        await asyncio.sleep(delay)
                    async with self._repo_factory() as repos:
                        rowcount = await repos.request_logs.update_model_for_request(request_id, model)
                    if rowcount:
                        break
                if not rowcount:
                    logger.warning(
                        "rewrite_request_log_model: request_log row for %s never appeared; model %s not recorded",
                        request_id,
                        model,
                    )
            except Exception:
                logger.warning(
                    "failed to rewrite request_log model request_id=%s model=%s",
                    request_id,
                    model,
                    exc_info=True,
                )

    async def _write_request_log(
        self: _RequestLoggingService,
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
    ) -> None:
        with anyio.CancelScope(shield=True):
            try:
                async with self._repo_factory() as repos:
                    await repos.request_logs.add_log(
                        account_id=account_id,
                        api_key_id=api_key.id if api_key else None,
                        session_id=_normalize_session_id(session_id),
                        request_id=request_id,
                        model=model or "",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_input_tokens=cached_input_tokens,
                        cache_write_tokens=cache_write_tokens,
                        reasoning_tokens=reasoning_tokens,
                        reasoning_effort=reasoning_effort,
                        transport=transport,
                        service_tier=service_tier,
                        requested_service_tier=requested_service_tier,
                        actual_service_tier=actual_service_tier,
                        latency_ms=latency_ms,
                        latency_first_token_ms=latency_first_token_ms,
                        status=status,
                        error_code=error_code,
                        error_message=error_message,
                    )
            except Exception:
                logger.warning(
                    "Failed to persist request log account_id=%s request_id=%s",
                    account_id,
                    request_id,
                    exc_info=True,
                )

    async def _write_stream_preflight_error(
        self: _RequestLoggingService,
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
        transport: str = _REQUEST_TRANSPORT_HTTP,
    ) -> None:
        await self._write_request_log(
            account_id=account_id,
            api_key=api_key,
            request_id=request_id,
            model=model,
            latency_ms=int((time.monotonic() - start) * 1000),
            status="error",
            error_code=error_code,
            error_message=error_message,
            reasoning_effort=reasoning_effort,
            transport=transport,
            service_tier=service_tier,
            requested_service_tier=service_tier,
        )
