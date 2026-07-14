from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocket
from fastapi.responses import JSONResponse
from starlette.requests import Request

import app.core.auth.dependencies as auth_dependencies
import app.modules.proxy.api as proxy_api_module
from app.core.errors import openai_error
from app.core.exceptions import ProxyAuthError
from app.core.openai.codex_search import CodexSearchRequest, CodexSearchResponse
from app.core.openai.requests import ResponsesRequest
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_responses_authentication_preflight_is_bounded_by_request_deadline(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    cancellation_seen = asyncio.Event()

    async def blocked_auth(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            raise

    monkeypatch.setattr(proxy_api_module, "_validate_proxy_api_key_authorization_compatible", blocked_auth)
    cleanup_tasks: set[asyncio.Task[None]] = set()
    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(service=SimpleNamespace(_proxy_cleanup_tasks=cleanup_tasks)),
    )
    started_at = time.monotonic()

    _api_key, response = await proxy_api_module._authenticate_responses_request_before_deadline(
        request,
        "Bearer test",
        context,
        request_deadline_at=started_at + 0.02,
        label="test authentication",
    )

    assert response is not None
    assert response.status_code == 504
    assert time.monotonic() - started_at < 0.2
    await asyncio.sleep(0)
    assert cancellation_seen.is_set()


@pytest.mark.asyncio
async def test_public_responses_deadline_starts_before_authentication(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/backend-api/codex/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    captured: dict[str, float] = {}

    async def delayed_auth(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.02)
        return None

    async def capture_stream(*_args: object, **kwargs: object) -> JSONResponse:
        captured["started"] = kwargs["request_started_at"]
        captured["deadline"] = kwargs["request_deadline_at"]
        captured["entered"] = time.monotonic()
        return JSONResponse({"ok": True})

    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_session_bridge_enabled=False,
            http_responses_stream_request_budget_seconds=0.1,
            http_responses_session_bridge_request_budget_seconds=0.8,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "_validate_proxy_api_key_authorization_compatible", delayed_auth)
    monkeypatch.setattr(proxy_api_module, "_stream_responses", capture_stream)
    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(service=SimpleNamespace(_proxy_cleanup_tasks=set())),
    )

    response = await proxy_api_module.responses(request, payload, context, authorization=None)

    assert response.status_code == 200
    assert captured["deadline"] - captured["started"] == pytest.approx(0.1)
    assert captured["deadline"] - captured["entered"] < 0.09


@pytest.mark.asyncio
async def test_alpha_search_authentication_is_hard_bounded_by_route_deadline(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/backend-api/codex/alpha/search",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = CodexSearchRequest.model_validate({"id": "search-auth-deadline", "model": "gpt-5.4"})
    cancellation_seen = asyncio.Event()
    allow_late_result = asyncio.Event()

    async def cancellation_suppressing_auth(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_result.wait()

    cleanup_tasks: set[asyncio.Task[None]] = set()
    search_codex = AsyncMock()
    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                _proxy_cleanup_tasks=cleanup_tasks,
                search_codex=search_codex,
            )
        ),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(codex_search_request_budget_seconds=0.02),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "_validate_proxy_api_key_authorization_compatible",
        cancellation_suppressing_auth,
    )
    started_at = time.monotonic()

    response = await proxy_api_module.codex_alpha_search(
        request,
        payload,
        context,
        authorization="Bearer test",
    )

    assert response.status_code == 504
    assert time.monotonic() - started_at < 0.2
    search_codex.assert_not_awaited()
    await asyncio.sleep(0)
    assert cancellation_seen.is_set()
    late_tasks = tuple(cleanup_tasks)
    assert late_tasks
    allow_late_result.set()
    await asyncio.gather(*late_tasks)


@pytest.mark.asyncio
async def test_alpha_search_model_access_is_hard_bounded_by_route_deadline(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/backend-api/codex/alpha/search",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = CodexSearchRequest.model_validate({"id": "search-model-deadline", "model": "gpt-5.4"})
    cancellation_seen = asyncio.Event()
    allow_late_result = asyncio.Event()

    async def pass_auth(*_args: object, **_kwargs: object) -> None:
        return None

    async def cancellation_suppressing_model_access(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_late_result.wait()

    cleanup_tasks: set[asyncio.Task[None]] = set()
    search_codex = AsyncMock()
    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                _proxy_cleanup_tasks=cleanup_tasks,
                search_codex=search_codex,
            )
        ),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(codex_search_request_budget_seconds=0.02),
    )
    monkeypatch.setattr(proxy_api_module, "_validate_proxy_api_key_authorization_compatible", pass_auth)
    monkeypatch.setattr(
        proxy_api_module,
        "_validate_model_access_for_request",
        cancellation_suppressing_model_access,
    )
    started_at = time.monotonic()

    response = await proxy_api_module.codex_alpha_search(
        request,
        payload,
        context,
        authorization="Bearer test",
    )

    assert response.status_code == 504
    assert time.monotonic() - started_at < 0.2
    search_codex.assert_not_awaited()
    await asyncio.sleep(0)
    assert cancellation_seen.is_set()
    late_tasks = tuple(cleanup_tasks)
    assert late_tasks
    allow_late_result.set()
    await asyncio.gather(*late_tasks)


@pytest.mark.asyncio
async def test_alpha_search_passes_route_entry_deadline_after_preflight(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/backend-api/codex/alpha/search",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = CodexSearchRequest.model_validate({"id": "search-inherited-deadline", "model": "gpt-5.4"})
    captured: dict[str, float] = {}

    async def delayed_auth(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.02)

    async def delayed_model_access(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.02)

    async def capture_search(*_args: object, **kwargs: object) -> CodexSearchResponse:
        captured["started"] = cast(float, kwargs["request_started_at"])
        captured["deadline"] = cast(float, kwargs["request_deadline_at"])
        captured["entered"] = time.monotonic()
        return CodexSearchResponse(output="search completed")

    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                _proxy_cleanup_tasks=set(),
                search_codex=capture_search,
            )
        ),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(codex_search_request_budget_seconds=0.2),
    )
    monkeypatch.setattr(proxy_api_module, "_validate_proxy_api_key_authorization_compatible", delayed_auth)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", delayed_model_access)

    response = await proxy_api_module.codex_alpha_search(
        request,
        payload,
        context,
        authorization="Bearer test",
    )

    assert response.status_code == 200
    assert captured["deadline"] - captured["started"] == pytest.approx(0.2)
    assert captured["entered"] - captured["started"] >= 0.03
    assert captured["deadline"] - captured["entered"] < 0.18


@pytest.mark.asyncio
async def test_internal_bridge_rejects_expired_signed_deadline_before_authentication(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    forwarded = SimpleNamespace(context=SimpleNamespace(request_deadline_unix_ms=int((time.time() - 1.0) * 1000)))
    auth = AsyncMock()
    stream = AsyncMock()
    monkeypatch.setattr(proxy_api_module, "parse_forwarded_request", lambda *_args, **_kwargs: (forwarded, None))
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(http_responses_session_bridge_instance_id="instance-a"),
    )
    monkeypatch.setattr(proxy_api_module, "_validate_internal_bridge_api_key", auth)
    monkeypatch.setattr(proxy_api_module, "_stream_responses", stream)
    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(service=SimpleNamespace(_proxy_cleanup_tasks=set())),
    )

    response = await proxy_api_module.internal_bridge_responses(request, payload, context)

    assert response.status_code == 504
    auth.assert_not_awaited()
    stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_bridge_streaming_fallback_uses_direct_absolute_deadline(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    reservation = ApiKeyUsageReservationData(
        reservation_id="res-preflight-deadline",
        key_id="key-preflight-deadline",
        model="gpt-5.4",
    )
    captured: dict[str, float] = {}

    async def validate_model(*args, **kwargs) -> None:
        del args, kwargs
        await asyncio.sleep(0.02)

    async def reserve(*args, **kwargs) -> ApiKeyUsageReservationData:
        del args, kwargs
        await asyncio.sleep(0.02)
        return reservation

    async def rate_limit_headers() -> dict[str, str]:
        await asyncio.sleep(0.02)
        return {}

    async def stream_responses(*args, **kwargs):
        del args
        captured["started"] = kwargs["request_started_at"]
        captured["deadline"] = kwargs["request_deadline_at"]
        captured["stream_started"] = time.monotonic()
        yield 'data: {"type":"response.completed","response":{"id":"resp-deadline","output":[]}}\n\n'

    schedule_release = Mock()
    service = SimpleNamespace(
        rate_limit_headers=rate_limit_headers,
        stream_http_responses=stream_responses,
        _schedule_websocket_reservation_release=schedule_release,
        _release_websocket_reservation=AsyncMock(),
        _proxy_cleanup_tasks=set(),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_session_bridge_enabled=False,
            http_responses_stream_request_budget_seconds=0.2,
            http_responses_session_bridge_request_budget_seconds=0.8,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda *_args: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", validate_model)
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", reserve)

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service)),
        None,
        prefer_http_bridge=True,
    )
    await response.body_iterator.aclose()

    assert captured["deadline"] - captured["started"] == pytest.approx(0.2)
    assert captured["deadline"] - captured["stream_started"] < 0.17
    schedule_release.assert_not_called()


@pytest.mark.asyncio
async def test_direct_responses_blocking_rate_header_preflight_cannot_extend_deadline(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    reservation = ApiKeyUsageReservationData(
        reservation_id="res-blocked-rate-headers",
        key_id="key-blocked-rate-headers",
        model="gpt-5.4",
    )
    schedule_release = Mock()

    async def blocked_headers() -> dict[str, str]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_stream_request_budget_seconds=0.02,
            http_responses_session_bridge_request_budget_seconds=0.02,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda *_args: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", AsyncMock())
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", AsyncMock(return_value=reservation))
    service = SimpleNamespace(
        rate_limit_headers=blocked_headers,
        stream_responses=Mock(),
        _schedule_websocket_reservation_release=schedule_release,
        _release_websocket_reservation=AsyncMock(),
        _proxy_cleanup_tasks=set(),
    )

    response = await asyncio.wait_for(
        proxy_api_module._stream_responses(
            request,
            payload,
            cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service)),
            None,
        ),
        timeout=0.5,
    )

    assert response.status_code == 504
    payload_body = json.loads(cast(bytes, response.body))
    assert payload_body["error"]["message"] == "Proxy request budget exhausted"
    schedule_release.assert_called_once_with(
        reservation,
        reason="responses-rate-limit-headers-timeout",
    )
    service.stream_responses.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("non_streaming", [False, True])
async def test_direct_responses_prefirst_failure_does_not_double_release_transferred_reservation(
    monkeypatch,
    non_streaming: bool,
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    reservation = ApiKeyUsageReservationData(
        reservation_id=f"res-prefirst-{non_streaming}",
        key_id="key-prefirst",
        model="gpt-5.4",
    )
    schedule_release = Mock()
    direct_release = AsyncMock()

    async def rate_limit_headers() -> dict[str, str]:
        return {}

    async def stream_responses(*args, **kwargs):
        del args, kwargs
        schedule_release(reservation, reason="inner-direct-prefirst-terminal")
        raise proxy_api_module.ProxyResponseError(
            504,
            openai_error("upstream_request_timeout", "Proxy request budget exhausted"),
        )
        yield ""

    service = SimpleNamespace(
        rate_limit_headers=rate_limit_headers,
        stream_responses=stream_responses,
        _schedule_websocket_reservation_release=schedule_release,
        _release_websocket_reservation=AsyncMock(),
        _proxy_cleanup_tasks=set(),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_session_bridge_enabled=False,
            http_responses_stream_request_budget_seconds=1.0,
            http_responses_session_bridge_request_budget_seconds=1.0,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda *_args: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", AsyncMock())
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", AsyncMock(return_value=reservation))
    monkeypatch.setattr(proxy_api_module, "_release_reservation", direct_release)

    if non_streaming:
        response = await proxy_api_module._collect_responses(
            request,
            payload,
            cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service)),
            None,
        )
    else:
        response = await proxy_api_module._stream_responses(
            request,
            payload,
            cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service)),
            None,
        )

    assert response.status_code == 504
    schedule_release.assert_called_once_with(
        reservation,
        reason="inner-direct-prefirst-terminal",
    )
    direct_release.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_bridge_nonstreaming_fallback_uses_direct_absolute_deadline(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    reservation = ApiKeyUsageReservationData(
        reservation_id="res-nonstream-deadline",
        key_id="key-nonstream-deadline",
        model="gpt-5.4",
    )
    captured: dict[str, float] = {}

    async def delayed_model_access(*args, **kwargs) -> None:
        del args, kwargs
        await asyncio.sleep(0.02)

    async def delayed_reservation(*args, **kwargs) -> ApiKeyUsageReservationData:
        del args, kwargs
        await asyncio.sleep(0.02)
        return reservation

    async def delayed_headers() -> dict[str, str]:
        await asyncio.sleep(0.02)
        return {}

    async def stream_responses(*args, **kwargs):
        del args
        captured["started"] = kwargs["request_started_at"]
        captured["deadline"] = kwargs["request_deadline_at"]
        captured["stream_started"] = time.monotonic()
        yield (
            'data: {"type":"response.completed","response":{"id":"resp-nonstream-deadline",'
            '"object":"response","status":"completed","output":[]}}\n\n'
        )

    service = SimpleNamespace(
        rate_limit_headers=delayed_headers,
        stream_http_responses=stream_responses,
        _release_websocket_reservation=AsyncMock(),
        _proxy_cleanup_tasks=set(),
    )
    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_session_bridge_enabled=False,
            http_responses_stream_request_budget_seconds=0.2,
            http_responses_session_bridge_request_budget_seconds=0.8,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda *_args: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", delayed_model_access)
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", delayed_reservation)

    response = await proxy_api_module._collect_responses(
        request,
        payload,
        cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service)),
        None,
        prefer_http_bridge=True,
    )

    assert response.status_code == 200
    assert captured["deadline"] - captured["started"] == pytest.approx(0.2)
    assert captured["deadline"] - captured["stream_started"] < 0.17


@pytest.mark.asyncio
async def test_non_streaming_responses_releases_reservation_when_rate_headers_fail(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    reservation = ApiKeyUsageReservationData(
        reservation_id="res-nonstream-header-failure",
        key_id="key-nonstream-header-failure",
        model="gpt-5.4",
    )
    release = AsyncMock()
    schedule_release = Mock()

    async def failed_headers() -> dict[str, str]:
        raise RuntimeError("rate header persistence failed")

    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_stream_request_budget_seconds=1.0,
            http_responses_session_bridge_request_budget_seconds=1.0,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda *_args: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", AsyncMock())
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", AsyncMock(return_value=reservation))
    monkeypatch.setattr(proxy_api_module, "_release_reservation", release)

    with pytest.raises(RuntimeError, match="rate header persistence failed"):
        await proxy_api_module._collect_responses(
            request,
            payload,
            cast(
                proxy_api_module.ProxyContext,
                SimpleNamespace(
                    service=SimpleNamespace(
                        rate_limit_headers=failed_headers,
                        _schedule_websocket_reservation_release=schedule_release,
                        _release_websocket_reservation=AsyncMock(),
                        _proxy_cleanup_tasks=set(),
                    )
                ),
            ),
            None,
        )

    release.assert_not_awaited()
    schedule_release.assert_called_once_with(
        reservation,
        reason="non-streaming-responses-preflight-failure",
    )


@pytest.mark.asyncio
async def test_non_streaming_responses_releases_reservation_when_rate_headers_are_cancelled(monkeypatch) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "input": "hello"})
    reservation = ApiKeyUsageReservationData(
        reservation_id="res-nonstream-header-cancel",
        key_id="key-nonstream-header-cancel",
        model="gpt-5.4",
    )
    headers_started = asyncio.Event()
    release = AsyncMock()
    schedule_release = Mock()

    async def blocked_headers() -> dict[str, str]:
        headers_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        proxy_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            http_responses_stream_request_budget_seconds=1.0,
            http_responses_session_bridge_request_budget_seconds=1.0,
        ),
    )
    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda *_args: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", AsyncMock())
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", AsyncMock(return_value=reservation))
    monkeypatch.setattr(proxy_api_module, "_release_reservation", release)
    task = asyncio.create_task(
        proxy_api_module._collect_responses(
            request,
            payload,
            cast(
                proxy_api_module.ProxyContext,
                SimpleNamespace(
                    service=SimpleNamespace(
                        rate_limit_headers=blocked_headers,
                        _schedule_websocket_reservation_release=schedule_release,
                        _release_websocket_reservation=AsyncMock(),
                        _proxy_cleanup_tasks=set(),
                    )
                ),
            ),
            None,
        )
    )
    await headers_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.assert_not_awaited()
    schedule_release.assert_called_once_with(
        reservation,
        reason="non-streaming-responses-preflight-failure",
    )


@pytest.mark.asyncio
async def test_validate_proxy_websocket_request_returns_firewall_denial(monkeypatch):
    denial = JSONResponse(
        status_code=403,
        content=openai_error("ip_forbidden", "Access denied for client IP", error_type="access_error"),
    )

    async def fake_denial(_websocket):
        return denial

    async def fail_auth(_authorization):
        raise AssertionError("authorization validation must not run when firewall already denied the websocket")

    monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", fake_denial)
    monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", fail_auth)

    api_key, response = await proxy_api_module._validate_proxy_websocket_request(
        cast(WebSocket, SimpleNamespace(headers={})),
    )

    assert api_key is None
    assert response is denial


@pytest.mark.asyncio
async def test_validate_proxy_websocket_request_maps_auth_error(monkeypatch):
    async def fake_denial(_websocket):
        return None

    async def fail_auth(_authorization):
        raise ProxyAuthError("Missing API key in Authorization header")

    monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", fake_denial)
    monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", fail_auth)

    api_key, response = await proxy_api_module._validate_proxy_websocket_request(
        cast(WebSocket, SimpleNamespace(headers={"authorization": "Bearer invalid"})),
    )

    assert api_key is None
    assert response is not None
    assert response.status_code == 401
    payload = json.loads(cast(bytes, response.body).decode("utf-8"))
    assert payload["error"]["code"] == "invalid_api_key"
    assert payload["error"]["message"] == "Missing API key in Authorization header"


@pytest.mark.asyncio
async def test_validate_proxy_websocket_request_returns_validated_api_key(monkeypatch):
    async def fake_denial(_websocket):
        return None

    api_key = ApiKeyData(
        id="key_1",
        name="Test Key",
        key_prefix="sk-test",
        allowed_models=None,
        enforced_model=None,
        enforced_reasoning_effort=None,
        enforced_service_tier=None,
        expires_at=None,
        is_active=True,
        created_at=datetime(2026, 3, 10),
        last_used_at=None,
    )

    async def pass_auth(authorization: str | None):
        assert authorization == "Bearer valid-key"
        return api_key

    monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", fake_denial)
    monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", pass_auth)

    resolved_api_key, response = await proxy_api_module._validate_proxy_websocket_request(
        cast(WebSocket, SimpleNamespace(headers={"authorization": "Bearer valid-key"})),
    )

    assert response is None
    assert resolved_api_key == api_key


@pytest.mark.asyncio
async def test_validate_proxy_websocket_request_allows_explicit_socket_peer_when_auth_disabled(monkeypatch):
    async def fake_denial(_websocket):
        return None

    async def fake_dashboard_settings() -> SimpleNamespace:
        return SimpleNamespace(api_key_auth_enabled=False)

    monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", fake_denial)
    monkeypatch.setattr(auth_dependencies, "get_settings_cache", lambda: SimpleNamespace(get=fake_dashboard_settings))
    monkeypatch.setattr(
        auth_dependencies,
        "get_settings",
        lambda: SimpleNamespace(proxy_unauthenticated_client_cidrs=["192.168.65.1/32"]),
    )
    monkeypatch.setattr(auth_dependencies, "is_local_request", lambda _request: False)

    resolved_api_key, response = await proxy_api_module._validate_proxy_websocket_request(
        cast(
            WebSocket,
            SimpleNamespace(headers={}, client=SimpleNamespace(host="192.168.65.1")),
        )
    )

    assert response is None
    assert resolved_api_key is None


@pytest.mark.asyncio
async def test_validate_proxy_websocket_request_rejects_remote_socket_peer_outside_allowlist(monkeypatch):
    async def fake_denial(_websocket):
        return None

    async def fake_dashboard_settings() -> SimpleNamespace:
        return SimpleNamespace(api_key_auth_enabled=False)

    monkeypatch.setattr(proxy_api_module, "_websocket_firewall_denial_response", fake_denial)
    monkeypatch.setattr(auth_dependencies, "get_settings_cache", lambda: SimpleNamespace(get=fake_dashboard_settings))
    monkeypatch.setattr(
        auth_dependencies,
        "get_settings",
        lambda: SimpleNamespace(proxy_unauthenticated_client_cidrs=["192.168.65.1/32"]),
    )
    monkeypatch.setattr(auth_dependencies, "is_local_request", lambda _request: False)

    resolved_api_key, response = await proxy_api_module._validate_proxy_websocket_request(
        cast(
            WebSocket,
            SimpleNamespace(headers={}, client=SimpleNamespace(host="192.168.65.2")),
        )
    )

    assert resolved_api_key is None
    assert response is not None
    assert response.status_code == 401
    payload = json.loads(cast(bytes, response.body).decode("utf-8"))
    assert payload["error"]["code"] == "invalid_api_key"
    assert payload["error"]["message"] == "Proxy authentication must be configured before remote access is allowed"


@pytest.mark.asyncio
async def test_validate_internal_bridge_api_key_allows_auth_disabled_remote_request(monkeypatch):
    async def fake_settings():
        return SimpleNamespace(api_key_auth_enabled=False)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("10.0.0.12", 12345),
        }
    )

    async def pass_auth(authorization: str | None, *, request: Request | None = None):
        assert authorization is None
        assert request is not None
        return None

    monkeypatch.setattr(proxy_api_module, "get_settings_cache", lambda: SimpleNamespace(get=fake_settings))
    monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", pass_auth)

    api_key, response = await proxy_api_module._validate_internal_bridge_api_key(request)

    assert api_key is None
    assert response is None


@pytest.mark.asyncio
async def test_validate_internal_bridge_api_key_preserves_local_request_exemption(monkeypatch):
    async def fake_settings():
        return SimpleNamespace(api_key_auth_enabled=True)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )

    async def pass_auth(authorization: str | None, *, request: Request | None = None):
        assert authorization is None
        assert request is not None
        return None

    monkeypatch.setattr(proxy_api_module, "get_settings_cache", lambda: SimpleNamespace(get=fake_settings))
    monkeypatch.setattr(proxy_api_module, "validate_proxy_api_key_authorization", pass_auth)

    api_key, response = await proxy_api_module._validate_internal_bridge_api_key(request)

    assert api_key is None
    assert response is None


@pytest.mark.asyncio
async def test_stream_responses_prefers_forwarded_downstream_turn_state(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [(b"x-codex-turn-state", b"http_turn_header_value")],
            "client": ("10.0.0.12", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    captured: dict[str, object] = {}

    def fake_apply_api_key_enforcement(_payload, _api_key):
        return None

    async def fake_validate_model_access(_api_key, _model):
        return None

    async def fake_enforce_request_limits(_api_key, *, request_model=None, request_service_tier=None):
        return None

    async def fake_release_reservation(_reservation):
        return None

    async def fake_rate_limit_headers():
        return {}

    async def fake_stream_http_responses(
        _payload,
        _headers,
        *,
        downstream_turn_state=None,
        **kwargs,
    ):
        captured["downstream_turn_state"] = downstream_turn_state
        event_block = (
            'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
            '"status":"completed","output":[]}}\n\n'
        )
        yield event_block

    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", fake_apply_api_key_enforcement)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", fake_validate_model_access)
    monkeypatch.setattr(proxy_api_module, "_enforce_request_limits", fake_enforce_request_limits)
    monkeypatch.setattr(proxy_api_module, "_release_reservation", fake_release_reservation)
    monkeypatch.setattr(
        proxy_api_module.proxy_service_module,
        "get_settings",
        lambda: SimpleNamespace(http_responses_session_bridge_enabled=True),
    )

    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                rate_limit_headers=fake_rate_limit_headers,
                stream_http_responses=fake_stream_http_responses,
                _release_websocket_reservation=AsyncMock(),
                _proxy_cleanup_tasks=set(),
            )
        ),
    )

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        context,
        None,
        prefer_http_bridge=True,
        forwarded_request=True,
        forwarded_headers={"x-codex-turn-state": "http_turn_header_value"},
        forwarded_downstream_turn_state="http_turn_forwarded_value",
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    assert captured["downstream_turn_state"] == "http_turn_forwarded_value"
    assert response.headers["x-codex-turn-state"] == "http_turn_forwarded_value"


@pytest.mark.asyncio
async def test_stream_responses_rejects_unclaimed_forwarded_reservation(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("10.0.0.12", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    release_reservation = AsyncMock()
    claim_reservation = AsyncMock(return_value=False)
    stream_http_responses = AsyncMock()
    forwarded_reservation = ApiKeyUsageReservationData(
        reservation_id="res_1",
        key_id="key_1",
        model="gpt-5.4",
    )

    def fake_apply_api_key_enforcement(_payload, _api_key):
        return None

    async def fake_validate_model_access(_api_key, _model):
        return None

    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", fake_apply_api_key_enforcement)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", fake_validate_model_access)
    monkeypatch.setattr(proxy_api_module, "_release_reservation", release_reservation)

    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                _claim_forwarded_websocket_reservation=claim_reservation,
                stream_http_responses=stream_http_responses,
                _proxy_cleanup_tasks=set(),
            )
        ),
    )

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        context,
        None,
        prefer_http_bridge=True,
        skip_limit_enforcement=True,
        api_key_reservation_override=forwarded_reservation,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    assert response.status_code == 409
    claim_reservation.assert_awaited_once_with(forwarded_reservation)
    stream_http_responses.assert_not_called()
    release_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_responses_reconciles_forwarded_claim_that_outlives_deadline(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("10.0.0.12", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    claim_release = asyncio.Event()
    scheduled: list[tuple[asyncio.Task[bool], ApiKeyUsageReservationData]] = []
    forwarded_reservation = ApiKeyUsageReservationData(
        reservation_id="res_timeout",
        key_id="key_1",
        model="gpt-5.4",
    )

    async def fake_claim_reservation(_reservation: ApiKeyUsageReservationData) -> bool:
        await claim_release.wait()
        return True

    def schedule_reconciliation(
        task: asyncio.Task[bool],
        reservation: ApiKeyUsageReservationData,
    ) -> None:
        scheduled.append((task, reservation))

    async def fake_validate_model_access(_api_key, _model):
        return None

    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda _payload, _api_key: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", fake_validate_model_access)
    service = SimpleNamespace(
        _claim_forwarded_websocket_reservation=fake_claim_reservation,
        _schedule_forwarded_reservation_claim_reconciliation=schedule_reconciliation,
        stream_http_responses=AsyncMock(),
        _proxy_cleanup_tasks=set(),
    )
    context = cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service))

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        context,
        None,
        prefer_http_bridge=True,
        skip_limit_enforcement=True,
        api_key_reservation_override=forwarded_reservation,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 0.02) * 1000),
    )

    assert response.status_code == 504
    assert len(scheduled) == 1
    claim_task, scheduled_reservation = scheduled[0]
    assert scheduled_reservation is forwarded_reservation
    assert not claim_task.done()
    claim_release.set()
    assert await asyncio.wait_for(claim_task, timeout=1.0) is True


@pytest.mark.asyncio
async def test_stream_responses_reconciles_forwarded_claim_commit_ack_failure(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("10.0.0.12", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    forwarded_reservation = ApiKeyUsageReservationData(
        reservation_id="res_commit_ack_failure",
        key_id="key_1",
        model="gpt-5.4",
    )
    scheduled: list[tuple[asyncio.Task[bool], ApiKeyUsageReservationData]] = []

    async def ambiguous_claim(_reservation: ApiKeyUsageReservationData) -> bool:
        raise RuntimeError("commit acknowledgement lost")

    def schedule_reconciliation(
        task: asyncio.Task[bool],
        reservation: ApiKeyUsageReservationData,
    ) -> None:
        scheduled.append((task, reservation))

    async def fake_validate_model_access(_api_key, _model):
        return None

    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda _payload, _api_key: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", fake_validate_model_access)
    service = SimpleNamespace(
        _claim_forwarded_websocket_reservation=ambiguous_claim,
        _schedule_forwarded_reservation_claim_reconciliation=schedule_reconciliation,
        stream_http_responses=AsyncMock(),
        _proxy_cleanup_tasks=set(),
    )
    context = cast(proxy_api_module.ProxyContext, SimpleNamespace(service=service))

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        context,
        None,
        prefer_http_bridge=True,
        skip_limit_enforcement=True,
        api_key_reservation_override=forwarded_reservation,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
    )

    assert response.status_code == 503
    assert len(scheduled) == 1
    assert scheduled[0][1] is forwarded_reservation
    assert scheduled[0][0].done()


@pytest.mark.asyncio
async def test_stream_responses_claims_forwarded_reservation_before_starting_owner_stream(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("10.0.0.12", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    release_reservation = AsyncMock()
    call_order: list[str] = []
    forwarded_reservation = ApiKeyUsageReservationData(
        reservation_id="res_1",
        key_id="key_1",
        model="gpt-5.4",
    )

    def fake_apply_api_key_enforcement(_payload, _api_key):
        return None

    async def fake_validate_model_access(_api_key, _model):
        return None

    async def fake_claim_reservation(_reservation):
        call_order.append("claim")
        return True

    async def fake_stream_http_responses(*args, **kwargs):
        del args, kwargs
        call_order.append("stream")
        yield (
            'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
            '"status":"completed","output":[]}}\n\n'
        )

    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", fake_apply_api_key_enforcement)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", fake_validate_model_access)
    monkeypatch.setattr(proxy_api_module, "_release_reservation", release_reservation)
    monkeypatch.setattr(
        proxy_api_module.proxy_service_module,
        "get_settings",
        lambda: SimpleNamespace(http_responses_session_bridge_enabled=True),
    )

    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                _claim_forwarded_websocket_reservation=fake_claim_reservation,
                stream_http_responses=fake_stream_http_responses,
                _proxy_cleanup_tasks=set(),
            )
        ),
    )

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        context,
        None,
        prefer_http_bridge=True,
        skip_limit_enforcement=True,
        api_key_reservation_override=forwarded_reservation,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
        include_rate_limit_headers=False,
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 200
    assert "response.completed" in body
    assert call_order == ["claim", "stream"]
    release_reservation.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_responses_releases_claimed_forwarded_reservation_once_on_startup_error(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bridge/responses",
            "headers": [],
            "client": ("10.0.0.12", 12345),
        }
    )
    payload = ResponsesRequest.model_validate({"model": "gpt-5.4", "instructions": "hi", "input": "hi"})
    release_reservation = AsyncMock()
    forwarded_reservation = ApiKeyUsageReservationData(
        reservation_id="res_1",
        key_id="key_1",
        model="gpt-5.4",
    )

    async def fake_validate_model_access(_api_key, _model):
        return None

    async def fake_claim_reservation(_reservation):
        return True

    async def fake_stream_http_responses(*args, **kwargs):
        del args, kwargs
        raise proxy_api_module.ProxyResponseError(
            503,
            openai_error("bridge_owner_unreachable", "owner unavailable", error_type="server_error"),
        )
        yield ""

    monkeypatch.setattr(proxy_api_module, "apply_api_key_enforcement", lambda _payload, _api_key: None)
    monkeypatch.setattr(proxy_api_module, "_validate_model_access_for_request", fake_validate_model_access)
    monkeypatch.setattr(proxy_api_module, "_release_reservation", release_reservation)
    monkeypatch.setattr(
        proxy_api_module.proxy_service_module,
        "get_settings",
        lambda: SimpleNamespace(http_responses_session_bridge_enabled=True),
    )
    context = cast(
        proxy_api_module.ProxyContext,
        SimpleNamespace(
            service=SimpleNamespace(
                _claim_forwarded_websocket_reservation=fake_claim_reservation,
                stream_http_responses=fake_stream_http_responses,
                _proxy_cleanup_tasks=set(),
            )
        ),
    )

    response = await proxy_api_module._stream_responses(
        request,
        payload,
        context,
        None,
        prefer_http_bridge=True,
        skip_limit_enforcement=True,
        api_key_reservation_override=forwarded_reservation,
        forwarded_request=True,
        forwarded_request_deadline_unix_ms=int((time.time() + 60.0) * 1000),
        include_rate_limit_headers=False,
    )

    assert response.status_code == 503
    release_reservation.assert_not_awaited()
