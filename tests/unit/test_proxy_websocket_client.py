from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake, InvalidProxy, InvalidStatus
from websockets.http11 import Response

import app.core.clients.proxy_websocket as proxy_websocket_module
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import (
    HTTPResponsesWebSocket,
    WebsocketsResponsesWebSocket,
    connect_responses_websocket,
    sanitize_upstream_websocket_event_text,
)


def _proxy_error_code(exc: ProxyResponseError) -> str | None:
    return exc.payload["error"].get("code")


def _proxy_error_message(exc: ProxyResponseError) -> str | None:
    return exc.payload["error"].get("message")


def _proxy_error_type(exc: ProxyResponseError) -> str | None:
    return exc.payload["error"].get("type")


class _UnexpectedAiohttpSession:
    async def ws_connect(self, *args, **kwargs):  # pragma: no cover - red-path guard
        raise AssertionError("aiohttp ws_connect should not be used for upstream websocket transport")


class _UnexpectedHttpClient:
    websocket_session = _UnexpectedAiohttpSession()


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return '{"type":"response.completed"}'

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_connect_responses_websocket_uses_http_transport_for_responses_wire_api(monkeypatch):
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=False,
        ),
    )

    websocket = await connect_responses_websocket(
        {"openai-beta": "responses_websockets=2026-02-06"},
        "access-token",
        None,
        base_url="https://provider.example/v1",
        wire_api="responses",
    )

    assert isinstance(websocket, HTTPResponsesWebSocket)


@pytest.mark.asyncio
async def test_http_responses_websocket_tracks_transport_cleanup_and_sanitizes_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_tasks: set[asyncio.Task[None]] = set()
    observed_cleanup_tasks: list[set[asyncio.Task[None]] | None] = []

    async def fake_stream_responses(
        *args: object,
        cleanup_tasks: set[asyncio.Task[None]] | None = None,
        on_response_started: Callable[[], None] | None = None,
        **kwargs: object,
    ):
        del args, kwargs
        observed_cleanup_tasks.append(cleanup_tasks)
        assert on_response_started is not None
        on_response_started()
        yield (
            'data: {"type":"response.failed","response":{"id":"resp-provider-error",'
            '"status":"failed","error":{"code":"connect-to-10.0.0.8:8443",'
            '"type":"/srv/private.sock","message":"connect to 10.0.0.8:8443 failed '
            'from /srv/private.sock","param":"/srv/private.sock"}}}\n\n'
        )

    monkeypatch.setattr(proxy_websocket_module, "upstream_stream_responses", fake_stream_responses)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=False,
        ),
    )
    websocket = await connect_responses_websocket(
        {},
        "access-token",
        None,
        base_url="https://provider.example/v1",
        wire_api="responses",
        cleanup_tasks=cleanup_tasks,
    )

    await websocket.send_text('{"type":"response.create","model":"gpt-5.4","input":"hello"}')
    message = await asyncio.wait_for(websocket.receive(), timeout=1.0)

    assert observed_cleanup_tasks == [cleanup_tasks]
    assert message.text is not None
    payload = json.loads(message.text)
    error = payload["response"]["error"]
    assert error == {
        "code": "upstream_error",
        "message": "Upstream request failed",
        "type": "server_error",
    }
    assert "10.0.0.8" not in message.text
    assert "private.sock" not in message.text
    await websocket.close()


@pytest.mark.asyncio
async def test_http_responses_websocket_send_waits_for_http_submission_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_started = asyncio.Event()
    acknowledge_post = asyncio.Event()

    async def delayed_stream_responses(
        *args: object,
        on_response_started: Callable[[], None] | None = None,
        **kwargs: object,
    ):
        del args, kwargs
        post_started.set()
        await acknowledge_post.wait()
        assert on_response_started is not None
        on_response_started()
        yield 'data: {"type":"response.completed","response":{"id":"resp-ack"}}\n\n'

    monkeypatch.setattr(proxy_websocket_module, "upstream_stream_responses", delayed_stream_responses)
    websocket = HTTPResponsesWebSocket(
        headers={},
        access_token="access-token",
        account_id=None,
        base_url="https://provider.example/v1",
        wire_api="responses",
        cleanup_tasks=set(),
    )

    send_task = asyncio.create_task(websocket.send_text('{"type":"response.create","model":"gpt-5.4","input":"hello"}'))
    await asyncio.wait_for(post_started.wait(), timeout=1.0)
    assert not send_task.done()
    acknowledge_post.set()
    await asyncio.wait_for(send_task, timeout=1.0)

    message = await asyncio.wait_for(websocket.receive(), timeout=1.0)
    assert message.text is not None
    assert "response.completed" in message.text
    await websocket.close()


@pytest.mark.asyncio
async def test_http_responses_websocket_send_fails_when_transport_breaks_before_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_stream_responses(*args: object, **kwargs: object):
        del args, kwargs
        if False:  # pragma: no cover - preserves async-generator shape
            yield ""
        raise ProxyResponseError(
            502,
            {"error": {"code": "upstream_unavailable", "message": "Upstream connection failed"}},
        )

    monkeypatch.setattr(proxy_websocket_module, "upstream_stream_responses", failed_stream_responses)
    websocket = HTTPResponsesWebSocket(
        headers={},
        access_token="access-token",
        account_id=None,
        base_url="https://provider.example/v1",
        wire_api="responses",
        cleanup_tasks=set(),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await websocket.send_text('{"type":"response.create","model":"gpt-5.4","input":"hello"}')

    assert exc_info.value.payload["error"]["code"] == "upstream_unavailable"
    await websocket.close()


@pytest.mark.asyncio
async def test_native_websocket_response_failure_is_sanitized() -> None:
    class SecretFailureConnection(_FakeConnection):
        async def recv(self) -> str:
            return (
                '{"type":"response.failed","response":{"id":"resp-native-secret",'
                '"status":"failed","error":{"code":"connect-to-10.0.0.8:8443",'
                '"type":"/srv/private.sock","message":"connect to 10.0.0.8:8443 failed '
                'from /srv/private.sock","param":"/srv/private.sock"}}}'
            )

    websocket = WebsocketsResponsesWebSocket(cast(ClientConnection, SecretFailureConnection()))

    message = await websocket.receive()

    assert message.text is not None
    assert '"code":"upstream_error"' in message.text
    assert '"message":"Upstream request failed"' in message.text
    assert '"type":"server_error"' in message.text
    assert "10.0.0.8" not in message.text
    assert "private.sock" not in message.text


@pytest.mark.asyncio
async def test_connect_responses_websocket_uses_websockets_transport(monkeypatch):
    fake_connection = _FakeConnection()
    seen: dict[str, object] = {}

    async def fake_websocket_connect(url: str, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return fake_connection

    monkeypatch.setattr(proxy_websocket_module, "get_http_client", lambda: _UnexpectedHttpClient(), raising=False)
    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=False,
        ),
    )

    websocket = await connect_responses_websocket(
        {
            "openai-beta": "responses_websockets=2026-02-06",
            "session_id": "session-1",
            "User-Agent": "Codex CLI Test",
            "Origin": "https://chatgpt.com",
            "Cookie": "dashboard_session=secret",
        },
        "access-token",
        "account-123",
    )

    await websocket.send_text("hello")

    assert fake_connection.sent == ["hello"]
    assert seen["url"] == "wss://chatgpt.com/backend-api/codex/responses"
    kwargs = cast(dict[str, object], seen["kwargs"])
    assert kwargs["origin"] == "https://chatgpt.com"
    assert kwargs["user_agent_header"] == "Codex CLI Test"
    assert kwargs["proxy"] is None
    assert kwargs["open_timeout"] == 7.0
    assert kwargs["max_size"] == 4321
    additional_headers = cast(dict[str, str], kwargs["additional_headers"])
    assert additional_headers["Authorization"] == "Bearer access-token"
    assert additional_headers["chatgpt-account-id"] == "account-123"
    assert additional_headers["openai-beta"] == "responses_websockets=2026-02-06"
    assert additional_headers["session_id"] == "session-1"
    assert "Cookie" not in additional_headers
    assert "User-Agent" not in additional_headers
    assert "Origin" not in additional_headers


@pytest.mark.asyncio
async def test_connect_responses_websocket_appends_required_beta_header(monkeypatch):
    fake_connection = _FakeConnection()
    seen: dict[str, object] = {}

    async def fake_websocket_connect(url: str, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return fake_connection

    monkeypatch.setattr(proxy_websocket_module, "get_http_client", lambda: _UnexpectedHttpClient(), raising=False)
    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=False,
        ),
    )

    await connect_responses_websocket(
        {"OpenAI-Beta": "assistants=v2"},
        "access-token",
        None,
    )

    kwargs = cast(dict[str, object], seen["kwargs"])
    additional_headers = cast(dict[str, str], kwargs["additional_headers"])
    assert additional_headers["OpenAI-Beta"] == "assistants=v2, responses_websockets=2026-02-06"


@pytest.mark.asyncio
async def test_connect_responses_websocket_maps_invalid_status(monkeypatch):
    async def fake_websocket_connect(url: str, **kwargs):
        raise InvalidStatus(
            Response(
                403,
                "Forbidden",
                Headers({"Content-Type": "application/json"}),
                b'{"error":{"message":"Forbidden","type":"permission_error","code":"forbidden"}}',
            )
        )

    monkeypatch.setattr(proxy_websocket_module, "get_http_client", lambda: _UnexpectedHttpClient(), raising=False)
    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=False,
        ),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await connect_responses_websocket(
            {"openai-beta": "responses_websockets=2026-02-06"},
            "access-token",
            "account-123",
        )

    assert exc_info.value.status_code == 403
    assert _proxy_error_code(exc_info.value) == "forbidden"
    assert _proxy_error_type(exc_info.value) == "permission_error"
    assert _proxy_error_message(exc_info.value) == "Upstream request was forbidden"


@pytest.mark.asyncio
async def test_connect_responses_websocket_uses_selected_proxy_url(monkeypatch):
    fake_connection = _FakeConnection()
    seen: dict[str, object] = {}

    async def fake_websocket_connect(url: str, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return fake_connection

    monkeypatch.setattr(proxy_websocket_module, "get_http_client", lambda: _UnexpectedHttpClient(), raising=False)
    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "select_upstream_egress",
        lambda: SimpleNamespace(route="proxy", proxy_url="http://127.0.0.1:7890"),
    )
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=False,
        ),
    )

    await connect_responses_websocket({"openai-beta": "responses_websockets=2026-02-06"}, "access-token", None)

    kwargs = cast(dict[str, object], seen["kwargs"])
    assert kwargs["proxy"] == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_connect_responses_websocket_maps_generic_invalid_handshake(monkeypatch):
    async def fake_websocket_connect(url: str, **kwargs):
        del url, kwargs
        raise InvalidHandshake("proxy CONNECT failed")

    monkeypatch.setattr(proxy_websocket_module, "get_http_client", lambda: _UnexpectedHttpClient(), raising=False)
    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=True,
        ),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await connect_responses_websocket(
            {"openai-beta": "responses_websockets=2026-02-06"},
            "access-token",
            "account-123",
        )

    assert exc_info.value.status_code == 502
    assert _proxy_error_code(exc_info.value) == "upstream_unavailable"
    assert _proxy_error_message(exc_info.value) == "Upstream websocket handshake failed"


@pytest.mark.asyncio
async def test_connect_responses_websocket_maps_invalid_proxy(monkeypatch):
    async def fake_websocket_connect(url: str, **kwargs):
        del url, kwargs
        raise InvalidProxy("http://proxy.invalid", "unsupported proxy scheme")

    monkeypatch.setattr(proxy_websocket_module, "get_http_client", lambda: _UnexpectedHttpClient(), raising=False)
    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=True,
        ),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await connect_responses_websocket(
            {"openai-beta": "responses_websockets=2026-02-06"},
            "access-token",
            "account-123",
        )

    assert exc_info.value.status_code == 502
    assert _proxy_error_code(exc_info.value) == "upstream_unavailable"
    assert _proxy_error_message(exc_info.value) == "Upstream websocket proxy connection failed"


@pytest.mark.asyncio
async def test_connect_responses_websocket_sanitizes_os_error_text(monkeypatch):
    async def fake_websocket_connect(url: str, **kwargs):
        del url, kwargs
        raise OSError("connect 10.0.0.8:8443 via /srv/private.sock")

    monkeypatch.setattr(proxy_websocket_module, "websocket_connect", fake_websocket_connect, raising=False)
    monkeypatch.setattr(
        proxy_websocket_module,
        "get_settings",
        lambda: SimpleNamespace(
            upstream_base_url="https://chatgpt.com/backend-api",
            upstream_connect_timeout_seconds=7.0,
            max_sse_event_bytes=4321,
            upstream_websocket_trust_env=True,
        ),
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await connect_responses_websocket({}, "access-token", "account-123")

    assert _proxy_error_code(exc_info.value) == "upstream_unavailable"
    assert _proxy_error_message(exc_info.value) == "Upstream websocket connection failed"


@pytest.mark.parametrize(
    "provider_text",
    [
        '{"type":"error","error":"connect 10.0.0.8:8443 /srv/private.sock"}',
        '{"type":"response.failed","response":{"id":"resp-malformed",'
        '"error":"connect 10.0.0.8:8443 /srv/private.sock"}}',
        "connect 10.0.0.8:8443 /srv/private.sock",
    ],
)
def test_provider_error_sanitizer_fails_closed_for_malformed_shapes(provider_text: str) -> None:
    public_text = sanitize_upstream_websocket_event_text(provider_text)

    assert '"code":"upstream_error"' in public_text
    assert '"message":"Upstream request failed"' in public_text
    assert '"type":"server_error"' in public_text
    assert "10.0.0.8" not in public_text
    assert "private.sock" not in public_text


def test_provider_error_sanitizer_preserves_capability_unavailable_code() -> None:
    provider_text = json.dumps(
        {
            "type": "response.failed",
            "response": {
                "id": "resp_capability_unavailable",
                "status": "failed",
                "error": {
                    "code": "upstream_capability_unavailable",
                    "message": "provider at 10.0.0.8 via /srv/private.sock is incompatible",
                    "type": "server_error",
                },
            },
        },
        separators=(",", ":"),
    )

    public_payload = json.loads(sanitize_upstream_websocket_event_text(provider_text))

    error = public_payload["response"]["error"]
    assert error["code"] == "upstream_capability_unavailable"
    assert error["message"] == "Upstream request failed"
    assert "10.0.0.8" not in json.dumps(public_payload)
    assert "private.sock" not in json.dumps(public_payload)
