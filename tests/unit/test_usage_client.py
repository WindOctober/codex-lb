from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from app.core.clients.usage import (
    UsageFetchError,
    consume_rate_limit_reset_credit,
    fetch_rate_limit_reset_credits,
    fetch_usage,
)

pytestmark = pytest.mark.unit


class StubResponse:
    def __init__(self, status: int, payload: dict | None, text: str) -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self, content_type: str | None = None) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def text(self) -> str:
        return self._text


@dataclass
class UsageClientState:
    calls: int = 0
    auth: str | None = None
    account: str | None = None
    method: str | None = None
    url: str | None = None
    json_body: dict[str, str] | None = None


class StubRequestContext:
    def __init__(
        self,
        responses: list[StubResponse],
        state: UsageClientState,
        headers: dict[str, str],
        retry_options: object | None,
        method: str,
        url: str,
        json_body: dict[str, str] | None,
    ) -> None:
        self._responses = responses
        self._state = state
        self._headers = headers
        self._retry_options = retry_options
        self._method = method
        self._url = url
        self._json_body = json_body

    async def __aenter__(self) -> StubResponse:
        attempts = getattr(self._retry_options, "attempts", 1)
        statuses = set(getattr(self._retry_options, "statuses", set()))
        response: StubResponse | None = None
        for attempt in range(attempts):
            index = min(self._state.calls, len(self._responses) - 1)
            response = self._responses[index]
            self._state.calls += 1
            self._state.auth = self._headers.get("Authorization")
            self._state.account = self._headers.get("chatgpt-account-id")
            self._state.method = self._method
            self._state.url = self._url
            self._state.json_body = self._json_body
            if response.status in statuses and attempt < attempts - 1:
                continue
            return response
        if response is None:
            response = StubResponse(500, None, "no response")
        return response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class StubRetryClient:
    def __init__(self, responses: list[StubResponse], state: UsageClientState) -> None:
        self._responses = responses
        self._state = state

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, str] | None = None,
        timeout: object | None = None,
        retry_options: object | None = None,
    ) -> StubRequestContext:
        return StubRequestContext(self._responses, self._state, headers or {}, retry_options, method, url, json)


@pytest.fixture
def usage_server() -> tuple[str, StubRetryClient, UsageClientState]:
    state = UsageClientState()
    responses = [
        StubResponse(503, None, "busy"),
        StubResponse(
            200,
            {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 12.5,
                        "reset_at": 1735689600,
                        "limit_window_seconds": 60,
                        "reset_after_seconds": 30,
                    }
                },
            },
            "",
        ),
    ]
    client = StubRetryClient(responses, state)
    return "http://usage.test/backend-api", client, state


@pytest.fixture
def failing_usage_server() -> tuple[str, StubRetryClient]:
    state = UsageClientState()
    responses = [StubResponse(503, None, "busy")]
    client = StubRetryClient(responses, state)
    return "http://usage.test/backend-api", client


@pytest.mark.asyncio
async def test_fetch_usage_retries_and_returns_payload(usage_server):
    base_url, client, state = usage_server
    data = await fetch_usage(
        access_token="access-token",
        account_id="acc_test",
        base_url=base_url,
        max_retries=1,
        timeout_seconds=2.0,
        client=cast(Any, client),
    )
    assert data.plan_type == "plus"
    assert state.calls == 2
    assert state.auth == "Bearer access-token"
    assert state.account == "acc_test"


@pytest.mark.asyncio
async def test_fetch_usage_raises_after_retries(failing_usage_server):
    base_url, client = failing_usage_server
    with pytest.raises(UsageFetchError) as excinfo:
        await fetch_usage(
            access_token="access-token",
            account_id=None,
            base_url=base_url,
            max_retries=0,
            timeout_seconds=1.0,
            client=cast(Any, client),
        )
    exc = excinfo.value
    assert isinstance(exc, UsageFetchError)
    assert exc.status_code == 503


@pytest.mark.asyncio
async def test_fetch_usage_preserves_error_code():
    state = UsageClientState()
    responses = [
        StubResponse(
            401,
            {
                "error": {
                    "code": "account_deactivated",
                    "message": "Your OpenAI account has been deactivated.",
                }
            },
            "",
        )
    ]
    client = StubRetryClient(responses, state)

    with pytest.raises(UsageFetchError) as excinfo:
        await fetch_usage(
            access_token="access-token",
            account_id=None,
            base_url="http://usage.test/backend-api",
            max_retries=0,
            timeout_seconds=1.0,
            client=cast(Any, client),
        )

    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.code == "account_deactivated"
    assert "deactivated" in exc.message.lower()


@pytest.mark.asyncio
async def test_fetch_rate_limit_reset_credits_reads_usage_payload():
    state = UsageClientState()
    client = StubRetryClient(
        [
            StubResponse(
                200,
                {
                    "plan_type": "plus",
                    "credits": [
                        {
                            "id": "RateLimitResetCredit_test",
                            "status": "available",
                            "reset_type": "codex_rate_limits",
                            "title": "Full reset (Weekly + 5 hr)",
                            "granted_at": "2026-06-18T03:15:00Z",
                            "expires_at": "2026-07-18T03:15:00Z",
                        }
                    ],
                    "available_count": 2,
                },
                "",
            )
        ],
        state,
    )

    credits = await fetch_rate_limit_reset_credits(
        access_token="access-token",
        account_id="acc_test",
        base_url="http://usage.test/backend-api",
        max_retries=0,
        timeout_seconds=1.0,
        client=cast(Any, client),
    )

    assert credits.available_count == 2
    assert credits.available_credits[0].id == "RateLimitResetCredit_test"
    assert credits.available_credits[0].title == "Full reset (Weekly + 5 hr)"
    assert credits.available_credits[0].granted_at is not None
    assert credits.available_credits[0].granted_at.isoformat() == "2026-06-18T03:15:00+00:00"
    assert credits.available_credits[0].expires_at is not None
    assert credits.available_credits[0].expires_at.isoformat() == "2026-07-18T03:15:00+00:00"
    assert state.method == "GET"
    assert state.url == "http://usage.test/backend-api/wham/rate-limit-reset-credits"


@pytest.mark.asyncio
async def test_consume_rate_limit_reset_credit_posts_redeem_request_id():
    state = UsageClientState()
    client = StubRetryClient(
        [
            StubResponse(
                200,
                {
                    "code": "reset",
                    "windows_reset": 1,
                },
                "",
            )
        ],
        state,
    )

    result = await consume_rate_limit_reset_credit(
        access_token="access-token",
        account_id="acc_test",
        credit_id="RateLimitResetCredit_test",
        idempotency_key="redeem-123",
        base_url="http://usage.test/backend-api",
        max_retries=0,
        timeout_seconds=1.0,
        client=cast(Any, client),
    )

    assert result.code == "reset"
    assert result.windows_reset == 1
    assert state.method == "POST"
    assert state.url == "http://usage.test/backend-api/wham/rate-limit-reset-credits/consume"
    assert state.json_body == {
        "credit_id": "RateLimitResetCredit_test",
        "redeem_request_id": "redeem-123",
    }
