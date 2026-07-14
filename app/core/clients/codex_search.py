from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping

import aiohttp
from pydantic import ValidationError

from app.core.clients.http import get_http_client
from app.core.clients.proxy import (
    ProxyResponseError,
    _build_upstream_headers,
    _error_details_from_envelope,
    _error_payload_from_response,
    _maybe_log_upstream_request_complete,
    _maybe_log_upstream_request_start,
    _service_circuit_breaker_context,
    _summarize_json_payload,
)
from app.core.clients.upstream import build_codex_search_url
from app.core.config.settings import get_settings
from app.core.egress import select_upstream_egress
from app.core.errors import openai_error
from app.core.openai.codex_search import CodexSearchRequest, CodexSearchResponse
from app.core.resilience.circuit_breaker import CircuitBreakerOpenError


def _is_retryable_search_status(status_code: int) -> bool:
    return status_code in {401, 429, 500, 502, 503, 504}


def _public_search_http_error(status_code: int) -> tuple[str, str]:
    if status_code == 400:
        return "invalid_request_error", "Codex alpha search request was rejected"
    if status_code == 401:
        return "invalid_api_key", "Codex alpha search authorization failed"
    if status_code == 403:
        return "permission_denied", "Codex alpha search request was denied"
    if status_code == 429:
        return "rate_limit_exceeded", "Codex alpha search was rate limited"
    return "upstream_unavailable", "Codex alpha search upstream request failed"


async def search_codex(
    payload: CodexSearchRequest,
    headers: Mapping[str, str],
    access_token: str,
    account_id: str | None,
    *,
    base_url: str | None = None,
    wire_api: str = "codex",
    timeout_seconds: float,
    session: aiohttp.ClientSession | None = None,
) -> CodexSearchResponse:
    if wire_api != "codex":
        raise ProxyResponseError(
            501,
            openai_error(
                "not_implemented",
                "Codex alpha search is not supported by this upstream provider",
                error_type="server_error",
            ),
        )

    settings = get_settings()
    upstream_base = (base_url or settings.upstream_base_url).rstrip("/")
    url = build_codex_search_url(upstream_base)
    upstream_headers = _build_upstream_headers(
        headers,
        access_token,
        account_id,
        accept="application/json",
    )
    payload_dict = payload.to_payload()
    effective_timeout = max(0.001, timeout_seconds)
    timeout = aiohttp.ClientTimeout(
        total=effective_timeout,
        sock_connect=min(settings.upstream_connect_timeout_seconds, effective_timeout),
        sock_read=effective_timeout,
    )
    client = session or get_http_client().session
    proxy_url = None if session is not None else select_upstream_egress().proxy_url
    started_at = time.monotonic()
    status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    failure_phase: str | None = None
    failure_detail: str | None = None
    failure_exception_type: str | None = None
    retryable_same_contract: bool | None = None

    _maybe_log_upstream_request_start(
        kind="codex_search",
        url=url,
        headers=upstream_headers,
        method="POST",
        payload_summary=_summarize_json_payload(payload_dict),
        payload_json=(
            json.dumps(payload_dict, ensure_ascii=True, separators=(",", ":"))
            if settings.log_upstream_request_payload
            else None
        ),
    )
    try:
        async with _service_circuit_breaker_context(
            client.post(
                url,
                headers=upstream_headers,
                json=payload_dict,
                timeout=timeout,
                proxy=proxy_url,
            ),
            account_id=account_id,
        ) as response:
            status_code = response.status
            if response.status >= 400:
                error_payload = await _error_payload_from_response(response)
                error_code, error_message = _error_details_from_envelope(error_payload)
                public_error_code, public_error_message = _public_search_http_error(response.status)
                failure_phase = "http_response"
                retryable_same_contract = _is_retryable_search_status(response.status)
                raise ProxyResponseError(
                    response.status,
                    openai_error(public_error_code, public_error_message),
                    failure_phase=failure_phase,
                    failure_detail=str(error_payload),
                    retryable_same_contract=retryable_same_contract,
                    upstream_status_code=response.status,
                )
            try:
                response_payload = await response.json(content_type=None)
                result = CodexSearchResponse.model_validate(response_payload)
            except (ValueError, ValidationError) as exc:
                error_code = "invalid_upstream_response"
                error_message = "Codex alpha search upstream returned invalid JSON"
                failure_phase = "response_decode"
                failure_detail = str(exc)
                failure_exception_type = type(exc).__name__
                raise ProxyResponseError(
                    502,
                    openai_error(error_code, error_message),
                    failure_phase=failure_phase,
                    failure_detail=failure_detail,
                    failure_exception_type=failure_exception_type,
                    upstream_status_code=response.status,
                ) from exc
            return result
    except ProxyResponseError:
        raise
    except CircuitBreakerOpenError as exc:
        error_code = "upstream_unavailable"
        error_message = "Upstream circuit breaker is open"
        failure_phase = "circuit_breaker"
        failure_detail = str(exc)
        failure_exception_type = type(exc).__name__
        retryable_same_contract = True
        raise ProxyResponseError(
            502,
            openai_error(error_code, error_message),
            failure_phase=failure_phase,
            retryable_same_contract=True,
            failure_detail=failure_detail,
            failure_exception_type=failure_exception_type,
        ) from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        error_code = "upstream_unavailable"
        error_message = "Codex alpha search upstream request failed"
        failure_phase = "transport"
        failure_detail = str(exc)
        failure_exception_type = type(exc).__name__
        retryable_same_contract = True
        raise ProxyResponseError(
            502,
            openai_error(error_code, error_message),
            failure_phase=failure_phase,
            retryable_same_contract=True,
            failure_detail=failure_detail,
            failure_exception_type=failure_exception_type,
        ) from exc
    finally:
        _maybe_log_upstream_request_complete(
            kind="codex_search",
            url=url,
            headers=upstream_headers,
            method="POST",
            started_at=started_at,
            status_code=status_code,
            error_code=error_code,
            error_message=error_message,
            failure_phase=failure_phase,
            failure_detail=failure_detail,
            failure_exception_type=failure_exception_type,
            retryable_same_contract=retryable_same_contract,
        )
