from __future__ import annotations

import json
import time
from datetime import UTC
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.modules.traffic.store import TrafficRequestRecord, TrafficStore, get_traffic_store, utc_now

REQUEST_MODEL_PARSE_LIMIT = 256 * 1024
PROXY_PATH_PREFIXES = ("/backend-api/", "/v1/", "/internal/bridge/", "/api/codex/usage")


def infer_remote_port(headers: dict[str, str]) -> str:
    for key in ("host", "x-forwarded-host"):
        port = _extract_port_from_host(headers.get(key))
        if port is not None:
            return str(port)
    explicit = (headers.get("x-codex-lb-port") or "").strip()
    if explicit.isdigit():
        return explicit
    return "unknown"


def _extract_port_from_host(value: str | None) -> int | None:
    if not value:
        return None
    host = value.split(",", maxsplit=1)[0].strip()
    if not host:
        return None
    try:
        parsed = urlsplit(host if "://" in host else f"//{host}")
        return parsed.port
    except ValueError:
        return None


def _headers_to_dict(scope: Scope) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers", []):
        key = raw_key.decode("latin1").lower()
        value = raw_value.decode("latin1")
        if key not in result:
            result[key] = value
    return result


def _parse_model_from_body(body: bytes) -> str | None:
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    if isinstance(parsed, dict):
        model = parsed.get("model")
        if isinstance(model, str) and model:
            return model
    return None


class TrafficMetricsMiddleware:
    def __init__(self, app: ASGIApp, *, enabled: bool = True, store: TrafficStore | None = None) -> None:
        self.app = app
        self.enabled = enabled
        self.store = store or get_traffic_store()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        if path.startswith("/__traffic") or not path.startswith(PROXY_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = _headers_to_dict(scope)
        internal_id = uuid4().hex
        request_id = headers.get("x-request-id") or headers.get("request-id") or internal_id
        client = scope.get("client")
        client_ip = client[0] if isinstance(client, tuple) and client else None
        start_monotonic = time.monotonic()
        start_time = utc_now()
        record = TrafficRequestRecord(
            internal_id=internal_id,
            request_id=request_id,
            start_time=start_time,
            end_time=None,
            duration_ms=None,
            method=str(scope.get("method") or "GET"),
            path=path,
            host=headers.get("host"),
            inferred_remote_port=infer_remote_port(headers),
            client_ip=client_ip,
        )
        self.store.start_request(record)

        request_bytes = 0
        request_buffer = bytearray()
        request_buffer_truncated = False
        response_bytes = 0
        status_code: int | None = None
        first_byte_ms: float | None = None
        response_completed = False
        saw_disconnect = False
        is_streaming = False
        error_type: str | None = None
        error_message: str | None = None

        async def receive_wrapper() -> Message:
            nonlocal request_bytes, request_buffer_truncated, saw_disconnect
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"") or b""
                request_bytes += len(body)
                if not request_buffer_truncated and body:
                    if len(request_buffer) + len(body) <= REQUEST_MODEL_PARSE_LIMIT:
                        request_buffer.extend(body)
                    else:
                        request_buffer_truncated = True
                        request_buffer.clear()
            elif message["type"] == "http.disconnect":
                saw_disconnect = True
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal first_byte_ms, is_streaming, response_bytes, response_completed, status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = {
                    key.decode("latin1").lower(): value.decode("latin1")
                    for key, value in message.get("headers", [])
                }
                content_type = response_headers.get("content-type", "")
                if "text/event-stream" in content_type.lower():
                    is_streaming = True
            elif message["type"] == "http.response.body":
                body = message.get("body", b"") or b""
                response_bytes += len(body)
                if first_byte_ms is None and body:
                    first_byte_ms = (time.monotonic() - start_monotonic) * 1000.0
                if bool(message.get("more_body")):
                    is_streaming = True
                else:
                    response_completed = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BaseException as exc:
            error_type = type(exc).__name__
            error_message = str(exc)
            raise
        finally:
            end_time = utc_now()
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=UTC)
            duration_ms = (time.monotonic() - start_monotonic) * 1000.0
            if error_type is not None:
                state = "errored"
            elif saw_disconnect and not response_completed:
                state = "aborted"
            elif not response_completed and status_code is not None:
                state = "aborted"
            else:
                state = "completed"
            model = None if request_buffer_truncated else _parse_model_from_body(bytes(request_buffer))
            if status_code is not None and status_code >= 400 and error_type is None:
                error_type = "http_status"
                error_message = f"HTTP {status_code}"
            self.store.finish_request(
                internal_id,
                end_time=end_time,
                duration_ms=duration_ms,
                status_code=status_code,
                error_type=error_type,
                error_message=error_message,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                is_streaming=is_streaming,
                state=state,
                first_byte_ms=first_byte_ms,
                model=model,
            )


__all__ = ["TrafficMetricsMiddleware", "infer_remote_port"]
