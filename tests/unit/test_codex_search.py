from __future__ import annotations

from typing import Self, cast

import aiohttp
import pytest
from pydantic import ValidationError

from app.core.clients.codex_search import search_codex
from app.core.clients.proxy import ProxyResponseError
from app.core.clients.upstream import build_codex_search_url
from app.core.config.settings import Settings
from app.core.openai.codex_search import CodexSearchRequest, CodexSearchResponse


class _SearchResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.reason = "OK" if status < 400 else "Bad Request"
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        return False

    async def json(self, *, content_type: str | None = None) -> object:
        del content_type
        return self._payload

    async def text(self, *, encoding: str | None = None, errors: str = "strict") -> str:
        del encoding, errors
        return ""


class _SearchSession:
    def __init__(self, response: _SearchResponse) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: object,
        timeout: aiohttp.ClientTimeout,
        proxy: str | None,
    ) -> _SearchResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
                "proxy": proxy,
            }
        )
        return self._response


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://chatgpt.com", "https://chatgpt.com/backend-api/codex/alpha/search"),
        ("https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/codex/alpha/search"),
        ("https://chatgpt.com/backend-api/codex", "https://chatgpt.com/backend-api/codex/alpha/search"),
    ],
)
def test_build_codex_search_url(base_url: str, expected: str) -> None:
    assert build_codex_search_url(base_url) == expected


def test_codex_search_request_preserves_forward_compatible_fields() -> None:
    payload = CodexSearchRequest.model_validate(
        {
            "id": " search-session ",
            "model": " gpt-5.6-sol ",
            "commands": {"search_query": [{"q": "OpenAI", "recency": 7}]},
            "settings": {"external_web_access": True},
            "max_output_tokens": 1200,
            "future_field": {"enabled": True},
        }
    )

    assert payload.id == "search-session"
    assert payload.model == "gpt-5.6-sol"
    assert payload.to_payload()["future_field"] == {"enabled": True}


def test_codex_search_response_rejects_empty_output() -> None:
    with pytest.raises(ValidationError):
        CodexSearchResponse(output="")


def test_codex_search_account_attempt_budget_must_leave_failover_time() -> None:
    with pytest.raises(ValidationError, match="must be less than"):
        Settings(
            codex_search_request_budget_seconds=10.0,
            codex_search_account_attempt_timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_search_codex_forwards_json_with_managed_account_credentials() -> None:
    payload = CodexSearchRequest.model_validate(
        {
            "id": "search-session",
            "model": "gpt-5.6-sol",
            "commands": {"search_query": [{"q": "OpenAI"}]},
        }
    )
    session = _SearchSession(
        _SearchResponse(
            200,
            {
                "encrypted_output": "ciphertext",
                "output": "search result",
                "future_response_field": 1,
            },
        )
    )

    result = await search_codex(
        payload,
        {"Authorization": "Bearer lb-key", "x-codex-client-version": "test"},
        "account-token",
        "account-header",
        base_url="https://chatgpt.com/backend-api/codex",
        timeout_seconds=30,
        session=cast(aiohttp.ClientSession, session),
    )

    assert result.output == "search result"
    assert result.to_payload()["future_response_field"] == 1
    call = session.calls[0]
    assert call["url"] == "https://chatgpt.com/backend-api/codex/alpha/search"
    headers = cast(dict[str, str], call["headers"])
    assert headers["Authorization"] == "Bearer account-token"
    assert headers["chatgpt-account-id"] == "account-header"
    assert headers["Accept"] == "application/json"
    assert call["proxy"] is None


@pytest.mark.asyncio
async def test_search_codex_sanitizes_structured_upstream_error() -> None:
    payload = CodexSearchRequest(id="search-session", model="gpt-5.6-sol")
    session = _SearchSession(
        _SearchResponse(
            400,
            {
                "error": {
                    "code": "invalid_request_error",
                    "message": "connect to 10.0.0.9:8443 via /srv/internal.sock",
                    "type": "/srv/internal.sock",
                }
            },
        )
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await search_codex(
            payload,
            {},
            "account-token",
            "account-header",
            base_url="https://chatgpt.com",
            timeout_seconds=30,
            session=cast(aiohttp.ClientSession, session),
        )

    exc = exc_info.value
    assert exc.status_code == 400
    assert exc.payload["error"]["code"] == "invalid_request_error"
    assert exc.payload["error"]["message"] == "Codex alpha search request was rejected"
    assert "10.0.0.9" not in str(exc.payload)
    assert "internal.sock" not in str(exc.payload)


@pytest.mark.asyncio
async def test_search_codex_sanitizes_native_transport_error() -> None:
    class _FailingSearchSession:
        def post(self, *args: object, **kwargs: object) -> _SearchResponse:
            del args, kwargs
            raise aiohttp.ClientConnectionError("connect to 10.0.0.9:8443 via /srv/internal.sock")

    with pytest.raises(ProxyResponseError) as exc_info:
        await search_codex(
            CodexSearchRequest(id="search-session", model="gpt-5.6-sol"),
            {},
            "account-token",
            "account-header",
            base_url="https://chatgpt.com",
            timeout_seconds=30,
            session=cast(aiohttp.ClientSession, _FailingSearchSession()),
        )

    exc = exc_info.value
    assert exc.status_code == 502
    assert exc.payload["error"]["message"] == "Codex alpha search upstream request failed"
    assert "10.0.0.9" not in str(exc.payload)
    assert "internal.sock" not in str(exc.payload)
