from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from types import TracebackType
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _normalize_session_id
from app.modules.proxy._service.request_logging import _RequestLoggingMixin
from app.modules.proxy._service.websocket.events import _normalize_session_id as _legacy_normalize_session_id
from app.modules.proxy.repo_bundle import ProxyRepoFactory, ProxyRepositories


class _FakeRequestLogs:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail
        self.rewrite_calls: list[tuple[str, str]] = []
        self.rewrite_results: list[int] = []
        self.rewrite_error: Exception | None = None

    async def add_log(self, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("request log unavailable")
        self.calls.append(kwargs)

    async def update_model_for_request(self, request_id: str, model: str) -> int:
        self.rewrite_calls.append((request_id, model))
        if self.rewrite_error is not None:
            raise self.rewrite_error
        if self.rewrite_results:
            return self.rewrite_results.pop(0)
        return 0


class _RepoContext(AbstractAsyncContextManager[ProxyRepositories]):
    def __init__(self, request_logs: _FakeRequestLogs) -> None:
        self._repos = cast(ProxyRepositories, type("Repos", (), {"request_logs": request_logs})())

    async def __aenter__(self) -> ProxyRepositories:
        return self._repos

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class _RequestLoggingHarness(_RequestLoggingMixin):
    def __init__(self, request_logs: _FakeRequestLogs) -> None:
        self._repo_factory = cast(ProxyRepoFactory, lambda: _RepoContext(request_logs))


def _api_key() -> ApiKeyData:
    return ApiKeyData(
        id="key-1",
        name="test",
        key_prefix="sk-test",
        allowed_models=None,
        enforced_model=None,
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_used_at=None,
    )


def test_session_id_normalizer_is_transport_neutral_and_compatible() -> None:
    assert _legacy_normalize_session_id is _normalize_session_id
    assert _normalize_session_id(None) is None
    assert _normalize_session_id("  ") is None
    assert _normalize_session_id("  session-1  ") == "session-1"


@pytest.mark.asyncio
async def test_request_logging_preserves_fields_and_normalizes_session_id() -> None:
    request_logs = _FakeRequestLogs()
    service = _RequestLoggingHarness(request_logs)

    await service._write_request_log(
        account_id="account-1",
        api_key=_api_key(),
        request_id="request-1",
        model="gpt-test",
        latency_ms=123,
        latency_first_token_ms=45,
        status="success",
        input_tokens=10,
        output_tokens=20,
        cached_input_tokens=4,
        reasoning_tokens=3,
        reasoning_effort="high",
        transport="websocket",
        service_tier="priority",
        requested_service_tier="priority",
        actual_service_tier="priority",
        session_id="  session-1  ",
    )

    assert request_logs.calls == [
        {
            "account_id": "account-1",
            "api_key_id": "key-1",
            "session_id": "session-1",
            "request_id": "request-1",
            "model": "gpt-test",
            "input_tokens": 10,
            "output_tokens": 20,
            "cached_input_tokens": 4,
            "cache_write_tokens": None,
            "reasoning_tokens": 3,
            "reasoning_effort": "high",
            "transport": "websocket",
            "service_tier": "priority",
            "requested_service_tier": "priority",
            "actual_service_tier": "priority",
            "latency_ms": 123,
            "latency_first_token_ms": 45,
            "status": "success",
            "error_code": None,
            "error_message": None,
        }
    ]


@pytest.mark.asyncio
async def test_stream_preflight_error_uses_default_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    request_logs = _FakeRequestLogs()
    service = _RequestLoggingHarness(request_logs)
    monkeypatch.setattr("app.modules.proxy._service.request_logging.time.monotonic", lambda: 10.5)

    await service._write_stream_preflight_error(
        account_id="account-1",
        api_key=None,
        request_id="request-2",
        model=None,
        start=10.0,
        error_code="proxy_unavailable",
        error_message="upstream unavailable",
        reasoning_effort="medium",
        service_tier="default",
    )

    call = request_logs.calls[0]
    assert call["latency_ms"] == 500
    assert call["status"] == "error"
    assert call["transport"] == "http"
    assert call["requested_service_tier"] == "default"
    assert call["service_tier"] == "default"
    assert call["model"] == ""


@pytest.mark.asyncio
async def test_request_log_failure_does_not_escape(caplog: pytest.LogCaptureFixture) -> None:
    service = _RequestLoggingHarness(_FakeRequestLogs(fail=True))

    with caplog.at_level(logging.WARNING, logger="app.modules.proxy.service"):
        await service._write_request_log(
            account_id="account-1",
            api_key=None,
            request_id="request-3",
            model="gpt-test",
            latency_ms=1,
            status="error",
        )

    assert "Failed to persist request log account_id=account-1 request_id=request-3" in caplog.text


@pytest.mark.asyncio
async def test_rewrite_request_log_model_retries_until_row_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_logs = _FakeRequestLogs()
    request_logs.rewrite_results = [0, 0, 1]
    service = _RequestLoggingHarness(request_logs)
    sleep = AsyncMock()
    monkeypatch.setattr("app.modules.proxy._service.request_logging.asyncio.sleep", sleep)

    await service.rewrite_request_log_model("request-4", "gpt-public")

    assert request_logs.rewrite_calls == [
        ("request-4", "gpt-public"),
        ("request-4", "gpt-public"),
        ("request-4", "gpt-public"),
    ]
    assert [call.args[0] for call in sleep.await_args_list] == [0.05, 0.1]


@pytest.mark.asyncio
async def test_rewrite_request_log_model_skips_blank_values() -> None:
    request_logs = _FakeRequestLogs()
    service = _RequestLoggingHarness(request_logs)

    await service.rewrite_request_log_model("", "gpt-public")
    await service.rewrite_request_log_model("request-5", "")

    assert request_logs.rewrite_calls == []


@pytest.mark.asyncio
async def test_rewrite_request_log_model_failure_does_not_escape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_logs = _FakeRequestLogs()
    request_logs.rewrite_error = RuntimeError("database unavailable")
    service = _RequestLoggingHarness(request_logs)

    with caplog.at_level(logging.WARNING, logger="app.modules.proxy.service"):
        await service.rewrite_request_log_model("request-6", "gpt-public")

    assert "failed to rewrite request_log model request_id=request-6 model=gpt-public" in caplog.text
