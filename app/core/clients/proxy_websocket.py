from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlparse, urlunparse

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as websocket_connect
from websockets.datastructures import Headers
from websockets.exceptions import (
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidHandshake,
    InvalidProxy,
    InvalidStatus,
)
from websockets.typing import Origin

from app.core.clients.proxy import ProxyResponseError, filter_inbound_headers
from app.core.clients.proxy import stream_responses as upstream_stream_responses
from app.core.clients.upstream import build_responses_url
from app.core.config.settings import get_settings
from app.core.errors import OpenAIErrorEnvelope, openai_error
from app.core.openai.parsing import parse_error_payload
from app.core.openai.requests import ResponsesRequest
from app.core.utils.request_id import get_request_id
from app.core.utils.sse import extract_sse_data, parse_sse_data_json

_WEBSOCKET_HOP_BY_HOP_HEADERS = {
    "accept",
    "connection",
    "content-type",
    "cookie",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
    "upgrade",
}
_RESPONSES_WEBSOCKET_BETA_HEADER = "responses_websockets=2026-02-06"


@dataclass(slots=True)
class UpstreamWebSocketMessage:
    kind: str
    text: str | None = None
    data: bytes | None = None
    close_code: int | None = None
    error: str | None = None


class UpstreamResponsesWebSocket(Protocol):
    async def send_text(self, text: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def receive(self) -> UpstreamWebSocketMessage: ...

    async def close(self) -> None: ...

    def response_header(self, name: str) -> str | None: ...


class WebsocketsResponsesWebSocket:
    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection

    async def send_text(self, text: str) -> None:
        await self._connection.send(text)

    async def send_bytes(self, data: bytes) -> None:
        await self._connection.send(data)

    async def receive(self) -> UpstreamWebSocketMessage:
        try:
            message = await self._connection.recv()
        except ConnectionClosedOK as exc:
            return UpstreamWebSocketMessage(kind="close", close_code=_close_code_from_exception(exc))
        except ConnectionClosedError as exc:
            return UpstreamWebSocketMessage(
                kind="error",
                close_code=_close_code_from_exception(exc),
                error=str(exc),
            )

        if isinstance(message, str):
            return UpstreamWebSocketMessage(kind="text", text=message)
        if isinstance(message, bytes):
            return UpstreamWebSocketMessage(kind="binary", data=message)
        return UpstreamWebSocketMessage(kind="error", error=f"Unexpected websocket message type: {type(message)!r}")

    async def close(self) -> None:
        await self._connection.close()

    def response_header(self, name: str) -> str | None:
        response = getattr(self._connection, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        value = headers.get(name)
        if value is None:
            return None
        return str(value)


class HTTPResponsesWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str],
        access_token: str,
        account_id: str | None,
        base_url: str | None,
        wire_api: str,
    ) -> None:
        self._headers = dict(headers)
        self._access_token = access_token
        self._account_id = account_id
        self._base_url = base_url
        self._wire_api = wire_api
        self._queue: asyncio.Queue[UpstreamWebSocketMessage] = asyncio.Queue()
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_enqueued = False

    async def send_text(self, text: str) -> None:
        request = _responses_request_from_websocket_text(text)
        task = asyncio.create_task(self._stream_request(request))
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    async def send_bytes(self, data: bytes) -> None:
        await self._enqueue_error_event(
            openai_error(
                "not_implemented",
                "Binary upstream websocket messages are not supported over HTTP responses transport",
            )
        )

    async def receive(self) -> UpstreamWebSocketMessage:
        if self._closed and self._queue.empty():
            return UpstreamWebSocketMessage(kind="close")
        return await self._queue.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._stream_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._enqueue_close()

    def response_header(self, name: str) -> str | None:
        return None

    async def _stream_request(self, payload: ResponsesRequest) -> None:
        try:
            async for line in upstream_stream_responses(
                payload,
                self._headers,
                self._access_token,
                self._account_id,
                base_url=self._base_url,
                wire_api=self._wire_api,
                raise_for_status=True,
            ):
                if self._closed:
                    return
                text_payload = _websocket_text_from_sse_event(line)
                if text_payload is None:
                    continue
                await self._queue.put(UpstreamWebSocketMessage(kind="text", text=text_payload))
        except ProxyResponseError as exc:
            await self._enqueue_error_event(exc.payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._enqueue_error_event(
                openai_error("upstream_error", str(exc) or "HTTP responses transport failed")
            )

    async def _enqueue_error_event(self, payload: OpenAIErrorEnvelope) -> None:
        if self._closed:
            return
        await self._queue.put(
            UpstreamWebSocketMessage(kind="text", text=_serialize_error_event_payload(payload))
        )

    async def _enqueue_close(self) -> None:
        if self._close_enqueued:
            return
        self._close_enqueued = True
        await self._queue.put(UpstreamWebSocketMessage(kind="close"))


def filter_inbound_websocket_headers(headers: dict[str, str]) -> dict[str, str]:
    filtered = filter_inbound_headers(headers)
    return {key: value for key, value in filtered.items() if key.lower() not in _WEBSOCKET_HOP_BY_HOP_HEADERS}


def _build_upstream_websocket_headers(
    inbound: dict[str, str],
    access_token: str,
    account_id: str | None,
) -> dict[str, str]:
    headers = {key: value for key, value in inbound.items() if key.lower() != "cookie"}
    lower_keys = {key.lower() for key in headers}
    if "x-request-id" not in lower_keys and "request-id" not in lower_keys:
        request_id = get_request_id()
        if request_id:
            headers["x-request-id"] = request_id
    headers["Authorization"] = f"Bearer {access_token}"
    if account_id:
        headers["chatgpt-account-id"] = account_id
    _ensure_responses_websocket_beta_header(headers)
    return headers


def _ensure_responses_websocket_beta_header(headers: dict[str, str]) -> None:
    header_key = next((key for key in headers if key.lower() == "openai-beta"), "openai-beta")
    current_value = headers.get(header_key, "")
    beta_tokens = [token.strip() for token in current_value.split(",") if token.strip()]
    if _RESPONSES_WEBSOCKET_BETA_HEADER.lower() not in {token.lower() for token in beta_tokens}:
        beta_tokens.append(_RESPONSES_WEBSOCKET_BETA_HEADER)
    headers[header_key] = ", ".join(beta_tokens)


def _pop_header_case_insensitive(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key in tuple(headers):
        if key.lower() != lowered:
            continue
        return headers.pop(key)
    return None


def _responses_websocket_url(base_url: str, wire_api: str) -> str:
    parsed = urlparse(build_responses_url(base_url, wire_api))
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http":
        scheme = "ws"
    else:
        scheme = parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme))


async def connect_responses_websocket(
    headers: dict[str, str],
    access_token: str,
    account_id: str | None,
    *,
    base_url: str | None = None,
    wire_api: str = "codex",
    session: object | None = None,
) -> UpstreamResponsesWebSocket:
    settings = get_settings()
    upstream_base = (base_url or settings.upstream_base_url).rstrip("/")
    if wire_api != "codex":
        return HTTPResponsesWebSocket(
            headers=_build_upstream_websocket_headers(headers, access_token, account_id),
            access_token=access_token,
            account_id=account_id,
            base_url=base_url,
            wire_api=wire_api,
        )
    url = _responses_websocket_url(upstream_base, wire_api)
    upstream_headers = _build_upstream_websocket_headers(headers, access_token, account_id)
    origin = cast(Origin | None, _pop_header_case_insensitive(upstream_headers, "origin"))
    user_agent = _pop_header_case_insensitive(upstream_headers, "user-agent")
    _ = session

    try:
        response = await websocket_connect(
            url,
            origin=origin,
            additional_headers=upstream_headers or None,
            user_agent_header=user_agent,
            proxy=True if settings.upstream_websocket_trust_env else None,
            open_timeout=settings.upstream_connect_timeout_seconds,
            max_size=settings.max_sse_event_bytes,
        )
    except asyncio.TimeoutError as exc:
        raise ProxyResponseError(
            502,
            openai_error("upstream_unavailable", "Request to upstream timed out"),
        ) from exc
    except InvalidStatus as exc:
        response = exc.response
        message = response.reason_phrase or f"Upstream websocket error: HTTP {response.status_code}"
        raise ProxyResponseError(
            response.status_code,
            _handshake_error_payload(response.status_code, message, response.headers, response.body),
        ) from exc
    except InvalidHandshake as exc:
        message = str(exc) or "Invalid upstream websocket handshake"
        raise ProxyResponseError(
            502,
            openai_error("upstream_unavailable", message, error_type="server_error"),
        ) from exc
    except InvalidProxy as exc:
        message = str(exc) or "Invalid upstream websocket proxy configuration"
        raise ProxyResponseError(
            502,
            openai_error("upstream_unavailable", message, error_type="server_error"),
        ) from exc
    except OSError as exc:
        raise ProxyResponseError(
            502,
            openai_error("upstream_unavailable", str(exc)),
        ) from exc

    return WebsocketsResponsesWebSocket(response)


def _close_code_from_exception(exc: ConnectionClosedOK | ConnectionClosedError) -> int | None:
    if exc.rcvd is not None:
        return int(exc.rcvd.code)
    if exc.sent is not None:
        return int(exc.sent.code)
    return None


def _responses_request_from_websocket_text(text: str) -> ResponsesRequest:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProxyResponseError(
            400,
            openai_error(
                "invalid_request_error",
                "Invalid websocket response.create payload",
                error_type="invalid_request_error",
            ),
        ) from exc
    if not isinstance(payload, dict):
        raise ProxyResponseError(
            400,
            openai_error(
                "invalid_request_error",
                "Invalid websocket response.create payload",
                error_type="invalid_request_error",
            ),
        )
    request_payload = dict(payload)
    request_payload.pop("type", None)
    request_payload.pop("client_metadata", None)
    request_payload["stream"] = True
    try:
        return ResponsesRequest.model_validate(request_payload)
    except ValidationError as exc:
        raise ProxyResponseError(
            400,
            openai_error(
                "invalid_request_error",
                "Invalid websocket response.create payload",
                error_type="invalid_request_error",
            ),
        ) from exc


def _websocket_text_from_sse_event(event_block: str) -> str | None:
    payload = parse_sse_data_json(event_block)
    if payload is not None:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    raw_data = extract_sse_data(event_block)
    if raw_data is None:
        return None
    return raw_data


def _serialize_error_event_payload(payload: OpenAIErrorEnvelope) -> str:
    error_payload = payload.get("error") or openai_error("upstream_error", "Upstream error")["error"]
    event = {"type": "error", "error": error_payload}
    return json.dumps(event, ensure_ascii=True, separators=(",", ":"))


def _handshake_error_payload(
    status_code: int,
    message: str,
    headers: Headers | None = None,
    body: bytes | bytearray | None = None,
) -> OpenAIErrorEnvelope:
    parsed = _try_parse_handshake_error_payload(headers, body)
    if parsed is not None:
        return parsed
    if status_code == 401:
        return openai_error("invalid_api_key", message, error_type="authentication_error")
    if status_code == 429:
        return openai_error("rate_limit_exceeded", message, error_type="rate_limit_error")
    if status_code == 403:
        return openai_error("forbidden", message, error_type="permission_error")
    if status_code >= 500:
        return openai_error("upstream_error", message, error_type="server_error")
    return openai_error("invalid_request_error", message, error_type="invalid_request_error")


def _try_parse_handshake_error_payload(
    headers: Headers | None,
    body: bytes | bytearray | None,
) -> OpenAIErrorEnvelope | None:
    if not body:
        return None

    content_type = ""
    if headers is not None:
        content_type = headers.get("Content-Type", "")

    if "json" not in content_type.lower() and not body.strip().startswith((b"{", b"[")):
        return None

    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None

    error = parse_error_payload(payload)
    if error is None:
        return None
    return {"error": error.model_dump(exclude_none=True)}
