from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp_retry import ExponentialRetry, RetryClient
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.clients.http import get_http_client
from app.core.config.settings import get_settings
from app.core.egress import select_upstream_egress
from app.core.types import JsonObject
from app.core.usage.models import RateLimitResetConsumePayload, RateLimitResetCreditBankPayload, UsagePayload
from app.core.utils.request_id import get_request_id

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
RETRY_START_TIMEOUT = 0.5
RETRY_MAX_TIMEOUT = 2.0

logger = logging.getLogger(__name__)


def _egress_proxy_kwargs(proxy_url: str | None) -> dict[str, str]:
    if proxy_url is None:
        return {}
    return {"proxy": proxy_url}


class UsageErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str | None = None
    message: str | None = None
    error_description: str | None = None


class UsageErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: UsageErrorDetail | str | None = None
    error_description: str | None = None
    message: str | None = None


class UsageFetchError(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


async def fetch_usage(
    *,
    access_token: str,
    account_id: str | None,
    account_label: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    client: RetryClient | None = None,
) -> UsagePayload:
    settings = get_settings()
    usage_base = base_url or settings.upstream_base_url
    url = _usage_url(usage_base)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds or settings.usage_fetch_timeout_seconds)
    retries = max_retries if max_retries is not None else settings.usage_fetch_max_retries
    headers = _usage_headers(access_token, account_id)
    retry_client = client or get_http_client().retry_client
    egress = select_upstream_egress() if client is None else None
    retry_options = _retry_options(retries + 1)

    try:
        async with retry_client.request(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            retry_options=retry_options,
            **_egress_proxy_kwargs(egress.proxy_url if egress is not None else None),
        ) as resp:
            data = await _safe_json(resp)
            if resp.status >= 400:
                code = _extract_error_code(data)
                message = _extract_error_message(data) or f"Usage fetch failed ({resp.status})"
                logger.warning(
                    "Usage fetch failed request_id=%s account=%s status=%s code=%s message=%s",
                    get_request_id(),
                    account_label,
                    resp.status,
                    code,
                    message,
                )
                raise UsageFetchError(resp.status, message, code=code)
            try:
                return UsagePayload.model_validate(data)
            except ValidationError as exc:
                logger.warning(
                    "Usage fetch invalid payload request_id=%s account=%s",
                    get_request_id(),
                    account_label,
                )
                raise UsageFetchError(502, "Invalid usage payload") from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning(
            "Usage fetch error request_id=%s account=%s error=%s",
            get_request_id(),
            account_label,
            exc,
        )
        raise UsageFetchError(0, f"Usage fetch failed: {exc}") from exc


async def fetch_rate_limit_reset_credits(
    *,
    access_token: str,
    account_id: str | None,
    account_label: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    client: RetryClient | None = None,
) -> RateLimitResetCreditBankPayload:
    settings = get_settings()
    usage_base = base_url or settings.upstream_base_url
    url = _rate_limit_reset_credits_url(usage_base)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds or settings.usage_fetch_timeout_seconds)
    retries = max_retries if max_retries is not None else settings.usage_fetch_max_retries
    headers = _usage_headers(access_token, account_id)
    retry_client = client or get_http_client().retry_client
    egress = select_upstream_egress() if client is None else None
    retry_options = _retry_options(retries + 1)

    try:
        async with retry_client.request(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            retry_options=retry_options,
            **_egress_proxy_kwargs(egress.proxy_url if egress is not None else None),
        ) as resp:
            data = await _safe_json(resp)
            if resp.status >= 400:
                code = _extract_error_code(data)
                message = _extract_error_message(data) or f"Reset credit fetch failed ({resp.status})"
                logger.warning(
                    "Reset credit fetch failed request_id=%s account=%s status=%s code=%s message=%s",
                    get_request_id(),
                    account_label,
                    resp.status,
                    code,
                    message,
                )
                raise UsageFetchError(resp.status, message, code=code)
            try:
                return RateLimitResetCreditBankPayload.model_validate(data)
            except ValidationError as exc:
                logger.warning(
                    "Reset credit fetch invalid payload request_id=%s account=%s",
                    get_request_id(),
                    account_label,
                )
                raise UsageFetchError(502, "Invalid reset credit payload") from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning(
            "Reset credit fetch error request_id=%s account=%s error=%s",
            get_request_id(),
            account_label,
            exc,
        )
        raise UsageFetchError(0, f"Reset credit fetch failed: {exc}") from exc


async def consume_rate_limit_reset_credit(
    *,
    access_token: str,
    account_id: str | None,
    credit_id: str,
    idempotency_key: str,
    account_label: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    client: RetryClient | None = None,
) -> RateLimitResetConsumePayload:
    settings = get_settings()
    usage_base = base_url or settings.upstream_base_url
    url = _rate_limit_reset_consume_url(usage_base)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds or settings.usage_fetch_timeout_seconds)
    retries = max_retries if max_retries is not None else settings.usage_fetch_max_retries
    headers = _usage_headers(access_token, account_id)
    headers["Content-Type"] = "application/json"
    retry_client = client or get_http_client().retry_client
    egress = select_upstream_egress() if client is None else None
    retry_options = _retry_options(retries + 1)

    try:
        async with retry_client.request(
            "POST",
            url,
            headers=headers,
            json={"credit_id": credit_id, "redeem_request_id": idempotency_key},
            timeout=timeout,
            retry_options=retry_options,
            **_egress_proxy_kwargs(egress.proxy_url if egress is not None else None),
        ) as resp:
            data = await _safe_json(resp)
            if resp.status >= 400:
                code = _extract_error_code(data)
                message = _extract_error_message(data) or f"Reset credit consume failed ({resp.status})"
                logger.warning(
                    "Reset credit consume failed request_id=%s account=%s status=%s code=%s message=%s",
                    get_request_id(),
                    account_label,
                    resp.status,
                    code,
                    message,
                )
                raise UsageFetchError(resp.status, message, code=code)
            try:
                return RateLimitResetConsumePayload.model_validate(data)
            except ValidationError as exc:
                logger.warning(
                    "Reset credit consume invalid payload request_id=%s account=%s",
                    get_request_id(),
                    account_label,
                )
                raise UsageFetchError(502, "Invalid reset credit consume payload") from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning(
            "Reset credit consume error request_id=%s account=%s error=%s",
            get_request_id(),
            account_label,
            exc,
        )
        raise UsageFetchError(0, f"Reset credit consume failed: {exc}") from exc


def _usage_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if "/backend-api" not in normalized:
        normalized = f"{normalized}/backend-api"
    return f"{normalized}/wham/usage"


def _rate_limit_reset_credits_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if "/backend-api" not in normalized:
        normalized = f"{normalized}/backend-api"
    return f"{normalized}/wham/rate-limit-reset-credits"


def _rate_limit_reset_consume_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if "/backend-api" not in normalized:
        normalized = f"{normalized}/backend-api"
    return f"{normalized}/wham/rate-limit-reset-credits/consume"


def _usage_headers(access_token: str, account_id: str | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    request_id = get_request_id()
    if request_id:
        headers["x-request-id"] = request_id
    if account_id and not account_id.startswith(("email_", "local_")):
        headers["chatgpt-account-id"] = account_id
    return headers


async def _safe_json(resp: aiohttp.ClientResponse) -> JsonObject:
    try:
        data = await resp.json(content_type=None)
    except Exception:
        text = await resp.text()
        return {"error": {"message": text.strip()}}
    return data if isinstance(data, dict) else {"error": {"message": str(data)}}


def _extract_error_message(payload: JsonObject) -> str | None:
    envelope = UsageErrorEnvelope.model_validate(payload)
    error = envelope.error
    if isinstance(error, UsageErrorDetail):
        return error.message or error.error_description
    if isinstance(error, str):
        return envelope.error_description or error
    return envelope.message


def _extract_error_code(payload: JsonObject) -> str | None:
    envelope = UsageErrorEnvelope.model_validate(payload)
    error = envelope.error
    if isinstance(error, UsageErrorDetail) and isinstance(error.code, str):
        normalized = error.code.strip().lower()
        return normalized or None
    return None


def _retry_options(attempts: int) -> ExponentialRetry:
    return ExponentialRetry(
        attempts=attempts,
        start_timeout=RETRY_START_TIMEOUT,
        max_timeout=RETRY_MAX_TIMEOUT,
        factor=2.0,
        statuses=RETRYABLE_STATUS,
        exceptions={aiohttp.ClientError, asyncio.TimeoutError},
        retry_all_server_errors=False,
    )
