from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import aiohttp

from app.core.auth.refresh import RefreshError
from app.core.clients.proxy import ProxyResponseError, filter_inbound_headers
from app.core.config.settings import Settings
from app.core.crypto import TokenEncryptor
from app.core.errors import openai_error
from app.core.openai.codex_search import CodexSearchRequest, CodexSearchResponse
from app.core.utils.request_id import ensure_request_id, get_request_id
from app.db.models import Account, DashboardSettings, StickySessionKind
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _prompt_cache_affinity_key_for_values
from app.modules.proxy._service.budget import _raise_proxy_budget_exhausted
from app.modules.proxy._service.support import (
    _await_operation_before_hard_timeout,
    _close_tracked_background_tasks,
    _is_account_neutral_error_code,
    _routing_strategy,
)
from app.modules.proxy._service.upstream_account import (
    _account_upstream_base_url,
    _account_upstream_wire_api,
    _upstream_account_header_value,
)
from app.modules.proxy.helpers import _normalize_error_code, _parse_openai_error
from app.modules.proxy.load_balancer import AccountSelection, LoadBalancer

logger = logging.getLogger("app.modules.proxy.service")

_SEARCH_MAX_ACCOUNT_ATTEMPTS = 2
_SEARCH_BOOKKEEPING_TIMEOUT_SECONDS = 1.0
_SEARCH_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _search_error_is_account_neutral(exc: ProxyResponseError, code: str) -> bool:
    if _is_account_neutral_error_code(code):
        return True
    return 400 <= exc.status_code < 500 and exc.status_code not in {401, 403, 408, 409, 429}


class _SearchRuntimeService(Protocol):
    _encryptor: TokenEncryptor
    _load_balancer: LoadBalancer
    _search_background_tasks: set[asyncio.Task[None]]

    def _schedule_search_bookkeeping(
        self,
        operation: Callable[[], Awaitable[None]],
        *,
        label: str,
        request_id: str,
        deadline: float,
    ) -> None: ...

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

    async def _core_search_codex_compatible(
        self,
        payload: CodexSearchRequest,
        headers: Mapping[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
        timeout_seconds: float,
    ) -> CodexSearchResponse: ...


class _SearchRuntimeMixin:
    def _schedule_search_bookkeeping(
        self: _SearchRuntimeService,
        operation: Callable[[], Awaitable[None]],
        *,
        label: str,
        request_id: str,
        deadline: float,
    ) -> None:
        async def run() -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.debug(
                    "Skipping Codex search bookkeeping after deadline label=%s request_id=%s",
                    label,
                    request_id,
                )
                return
            try:
                await _await_operation_before_hard_timeout(
                    operation(),
                    timeout_seconds=min(_SEARCH_BOOKKEEPING_TIMEOUT_SECONDS, remaining),
                    tasks=self._search_background_tasks,
                    label=f"Codex search bookkeeping {label} request_id={request_id}",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Codex search background bookkeeping failed label=%s request_id=%s",
                    label,
                    request_id,
                    exc_info=True,
                )

        task = asyncio.create_task(run(), name=f"codex-search-{label}-{request_id}")
        self._search_background_tasks.add(task)
        task.add_done_callback(self._search_background_tasks.discard)

    async def close_search_background_tasks(self: _SearchRuntimeService) -> None:
        await _close_tracked_background_tasks(
            self._search_background_tasks,
            label="Codex search background tasks",
            timeout_seconds=_SEARCH_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS,
        )

    async def search_codex(
        self: _SearchRuntimeService,
        payload: CodexSearchRequest,
        headers: Mapping[str, str],
        *,
        api_key: ApiKeyData | None = None,
        request_started_at: float | None = None,
        request_deadline_at: float | None = None,
    ) -> CodexSearchResponse:
        base_settings = self._proxy_runtime_settings()
        if (request_started_at is None) != (request_deadline_at is None):
            raise RuntimeError("Codex search request timing must be provided as a complete pair")
        started_at = time.monotonic() if request_started_at is None else request_started_at
        deadline = (
            started_at + base_settings.codex_search_request_budget_seconds
            if request_deadline_at is None
            else request_deadline_at
        )
        dashboard_timeout_seconds = deadline - time.monotonic()
        if dashboard_timeout_seconds <= 0:
            _raise_proxy_budget_exhausted()
        try:
            dashboard_settings = await _await_operation_before_hard_timeout(
                self._proxy_dashboard_settings(),
                timeout_seconds=dashboard_timeout_seconds,
                tasks=self._search_background_tasks,
                label="Codex search dashboard lookup",
            )
        except TimeoutError:
            _raise_proxy_budget_exhausted()
        filtered_headers = filter_inbound_headers(headers)
        request_id = get_request_id() or ensure_request_id(None)
        routing_strategy = _routing_strategy(dashboard_settings)
        excluded_account_ids: set[str] = set()
        last_error: ProxyResponseError | None = None
        account_id_value: str | None = None
        log_status = "error"
        log_error_code: str | None = None
        log_error_message: str | None = None
        sticky_key = _prompt_cache_affinity_key_for_values(
            cache_key=payload.id,
            model=payload.model,
            api_key=api_key,
        )

        try:
            for account_attempt in range(_SEARCH_MAX_ACCOUNT_ATTEMPTS):
                selection = await self._select_account_with_budget_compatible(
                    deadline,
                    request_id=request_id,
                    kind="search",
                    api_key=api_key,
                    sticky_key=sticky_key,
                    sticky_kind=StickySessionKind.PROMPT_CACHE,
                    sticky_max_age_seconds=dashboard_settings.openai_cache_affinity_max_age_seconds,
                    prefer_earlier_reset_accounts=dashboard_settings.prefer_earlier_reset_accounts,
                    routing_strategy=routing_strategy,
                    model=payload.model,
                    exclude_account_ids=excluded_account_ids,
                    required_upstream_wire_api="codex",
                )
                account = selection.account
                if account is None:
                    if last_error is not None:
                        raise last_error
                    log_error_code = selection.error_code or "no_accounts"
                    log_error_message = selection.error_message or "No active accounts available"
                    raise ProxyResponseError(
                        503,
                        openai_error(log_error_code, log_error_message),
                    )

                account_id_value = account.id
                remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                if remaining_budget <= 0:
                    _raise_proxy_budget_exhausted()
                account_attempt_deadline = min(
                    deadline,
                    time.monotonic() + base_settings.codex_search_account_attempt_timeout_seconds,
                )
                account_attempt_remaining = max(0.0, account_attempt_deadline - time.monotonic())
                if account_attempt_remaining <= 0:
                    _raise_proxy_budget_exhausted()
                try:
                    account = await self._ensure_fresh_with_budget(
                        account,
                        timeout_seconds=min(remaining_budget, account_attempt_remaining),
                    )
                except RefreshError as exc:
                    if exc.is_permanent:
                        self._schedule_search_bookkeeping(
                            lambda target=account, error_code=exc.code: self._load_balancer.mark_permanent_failure(
                                target,
                                error_code,
                            ),
                            label="mark-permanent-failure",
                            request_id=request_id,
                            deadline=deadline,
                        )
                    else:
                        self._schedule_search_bookkeeping(
                            lambda target=account: self._load_balancer.record_error(target),
                            label="record-refresh-error",
                            request_id=request_id,
                            deadline=deadline,
                        )
                    last_error = ProxyResponseError(
                        401 if exc.is_permanent else 502,
                        openai_error(
                            "invalid_api_key" if exc.is_permanent else "upstream_unavailable",
                            "Codex alpha search credential refresh failed",
                        ),
                    )
                    excluded_account_ids.add(account.id)
                    if account_attempt + 1 < _SEARCH_MAX_ACCOUNT_ATTEMPTS:
                        continue
                    raise last_error from exc
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "Codex alpha search credential refresh failed account_id=%s",
                        account.id,
                        exc_info=True,
                    )
                    self._schedule_search_bookkeeping(
                        lambda target=account: self._load_balancer.record_error(target),
                        label="record-refresh-transport-error",
                        request_id=request_id,
                        deadline=deadline,
                    )
                    last_error = ProxyResponseError(
                        502,
                        openai_error("upstream_unavailable", "Codex alpha search credential refresh failed"),
                    )
                    excluded_account_ids.add(account.id)
                    if account_attempt + 1 < _SEARCH_MAX_ACCOUNT_ATTEMPTS:
                        continue
                    raise last_error from exc

                wire_api = _account_upstream_wire_api(account)
                if wire_api != "codex":
                    last_error = ProxyResponseError(
                        501,
                        openai_error(
                            "not_implemented",
                            "Codex alpha search is not supported by this upstream provider",
                            error_type="server_error",
                        ),
                    )
                    excluded_account_ids.add(account.id)
                    if account_attempt + 1 < _SEARCH_MAX_ACCOUNT_ATTEMPTS:
                        continue
                    raise last_error

                forced_refresh_used = False
                while True:
                    remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                    if remaining_budget <= 0:
                        _raise_proxy_budget_exhausted()
                    account_attempt_remaining = max(
                        0.0,
                        account_attempt_deadline - time.monotonic(),
                    )
                    if account_attempt_remaining <= 0:
                        self._schedule_search_bookkeeping(
                            lambda target=account: self._load_balancer.record_error(target),
                            label="record-attempt-timeout",
                            request_id=request_id,
                            deadline=deadline,
                        )
                        last_error = ProxyResponseError(
                            502,
                            openai_error(
                                "upstream_unavailable",
                                "Codex alpha search account attempt budget exhausted",
                            ),
                        )
                        excluded_account_ids.add(account.id)
                        break
                    attempt_timeout_seconds = min(remaining_budget, account_attempt_remaining)
                    try:
                        result = await _await_operation_before_hard_timeout(
                            self._core_search_codex_compatible(
                                payload,
                                filtered_headers,
                                self._encryptor.decrypt(account.access_token_encrypted),
                                _upstream_account_header_value(account),
                                base_url=_account_upstream_base_url(account),
                                wire_api=wire_api,
                                timeout_seconds=attempt_timeout_seconds,
                            ),
                            timeout_seconds=attempt_timeout_seconds,
                            tasks=self._search_background_tasks,
                            label=f"Codex search upstream attempt account_id={account.id}",
                        )
                        self._schedule_search_bookkeeping(
                            lambda: self._load_balancer.record_success(account),
                            label="record-success",
                            request_id=request_id,
                            deadline=deadline,
                        )
                        log_status = "success"
                        log_error_code = None
                        log_error_message = None
                        return result
                    except ProxyResponseError as exc:
                        error = _parse_openai_error(exc.payload)
                        code = _normalize_error_code(
                            error.code if error else None,
                            error.type if error else None,
                        )
                        log_error_code = code
                        log_error_message = error.message if error else None
                        if exc.status_code == 401 and not forced_refresh_used:
                            forced_refresh_used = True
                            remaining_budget = self._remaining_budget_seconds_compatible(deadline)
                            if remaining_budget <= 0:
                                _raise_proxy_budget_exhausted()
                            account_attempt_remaining = max(
                                0.0,
                                account_attempt_deadline - time.monotonic(),
                            )
                            if account_attempt_remaining <= 0:
                                self._schedule_search_bookkeeping(
                                    lambda target=account, error=exc: self._handle_proxy_error(target, error),
                                    label="record-forced-refresh-timeout",
                                    request_id=request_id,
                                    deadline=deadline,
                                )
                                last_error = ProxyResponseError(
                                    502,
                                    openai_error(
                                        "upstream_unavailable",
                                        "Codex alpha search account attempt budget exhausted",
                                    ),
                                )
                                excluded_account_ids.add(account.id)
                                break
                            try:
                                account = await self._ensure_fresh_with_budget(
                                    account,
                                    force=True,
                                    timeout_seconds=min(remaining_budget, account_attempt_remaining),
                                )
                            except (RefreshError, aiohttp.ClientError, asyncio.TimeoutError):
                                self._schedule_search_bookkeeping(
                                    lambda target=account, error=exc: self._handle_proxy_error(target, error),
                                    label="record-forced-refresh-error",
                                    request_id=request_id,
                                    deadline=deadline,
                                )
                                last_error = exc
                                excluded_account_ids.add(account.id)
                                break
                            continue
                        if _search_error_is_account_neutral(exc, code):
                            raise
                        self._schedule_search_bookkeeping(
                            lambda target=account, error=exc: self._handle_proxy_error(target, error),
                            label="record-upstream-error",
                            request_id=request_id,
                            deadline=deadline,
                        )
                        last_error = exc
                        excluded_account_ids.add(account.id)
                        break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        logger.warning(
                            "Codex alpha search upstream request failed account_id=%s",
                            account.id,
                            exc_info=True,
                        )
                        self._schedule_search_bookkeeping(
                            lambda target=account: self._load_balancer.record_error(target),
                            label="record-upstream-transport-error",
                            request_id=request_id,
                            deadline=deadline,
                        )
                        log_error_code = "upstream_unavailable"
                        log_error_message = "Codex alpha search upstream request failed"
                        last_error = ProxyResponseError(
                            502,
                            openai_error(log_error_code, log_error_message),
                        )
                        excluded_account_ids.add(account.id)
                        break

                if account_attempt + 1 >= _SEARCH_MAX_ACCOUNT_ATTEMPTS and last_error is not None:
                    raise last_error

            if last_error is not None:
                raise last_error
            raise ProxyResponseError(503, openai_error("no_accounts", "No active accounts available"))
        finally:
            latency_ms = int((time.monotonic() - started_at) * 1000)
            self._schedule_search_bookkeeping(
                lambda: self._write_request_log(
                    account_id=account_id_value,
                    api_key=api_key,
                    request_id=request_id,
                    model=payload.model,
                    latency_ms=latency_ms,
                    status=log_status,
                    error_code=log_error_code,
                    error_message=log_error_message,
                    transport="search",
                    session_id=payload.id,
                ),
                label="request-log",
                request_id=request_id,
                deadline=deadline,
            )
