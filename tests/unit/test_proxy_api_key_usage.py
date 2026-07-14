from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from types import TracebackType
from typing import cast

import pytest

from app.core.exceptions import ProxyAuthError, ProxyRateLimitError
from app.core.openai.models import CompactResponsePayload
from app.modules.api_keys.service import (
    ApiKeyData,
    ApiKeyInvalidError,
    ApiKeyRateLimitExceededError,
    ApiKeyUsageCharge,
    ApiKeyUsageReservationData,
)
from app.modules.proxy._service import api_key_usage
from app.modules.proxy._service.api_key_usage import _ApiKeyUsageRuntimeMixin
from app.modules.proxy._service.support import _StreamSettlement
from app.modules.proxy.repo_bundle import ProxyRepoFactory, ProxyRepositories


class _FakeApiKeyRepository:
    def __init__(self) -> None:
        self.reservation = ApiKeyUsageReservationData(reservation_id="reservation-1", key_id="key-1", model="gpt-key")
        self.enforce_error: Exception | None = None
        self.enforce_calls: list[dict[str, str | None]] = []
        self.finalize_calls: list[tuple[str, dict[str, object]]] = []
        self.fail_calls: list[tuple[str, dict[str, object]]] = []
        self.release_calls: list[str] = []
        self.settlement_error: Exception | None = None
        self.settlement_attempts = 0


class _FakeApiKeysService:
    def __init__(self, repository: _FakeApiKeyRepository) -> None:
        self._repository = repository

    async def enforce_limits_for_request(
        self,
        key_id: str,
        *,
        request_model: str | None,
        request_service_tier: str | None,
    ) -> ApiKeyUsageReservationData:
        self._repository.enforce_calls.append(
            {
                "key_id": key_id,
                "request_model": request_model,
                "request_service_tier": request_service_tier,
            }
        )
        if self._repository.enforce_error is not None:
            raise self._repository.enforce_error
        return self._repository.reservation

    async def finalize_usage_reservation(self, reservation_id: str, **kwargs: object) -> None:
        self._repository.settlement_attempts += 1
        if self._repository.settlement_error is not None:
            raise self._repository.settlement_error
        self._repository.finalize_calls.append((reservation_id, kwargs))

    async def fail_usage_reservation(self, reservation_id: str, **kwargs: object) -> None:
        self._repository.settlement_attempts += 1
        if self._repository.settlement_error is not None:
            raise self._repository.settlement_error
        self._repository.fail_calls.append((reservation_id, kwargs))

    async def release_usage_reservation(self, reservation_id: str) -> None:
        self._repository.settlement_attempts += 1
        if self._repository.settlement_error is not None:
            raise self._repository.settlement_error
        self._repository.release_calls.append(reservation_id)


class _RepoContext(AbstractAsyncContextManager[ProxyRepositories]):
    def __init__(self, api_keys: _FakeApiKeyRepository) -> None:
        self._repos = cast(ProxyRepositories, type("Repos", (), {"api_keys": api_keys})())

    async def __aenter__(self) -> ProxyRepositories:
        return self._repos

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class _ApiKeyUsageHarness(_ApiKeyUsageRuntimeMixin):
    def __init__(self, api_keys: _FakeApiKeyRepository) -> None:
        self._repo_factory = cast(ProxyRepoFactory, lambda: _RepoContext(api_keys))


@pytest.fixture(autouse=True)
def _fake_api_keys_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_usage, "ApiKeysService", _FakeApiKeysService)


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


@pytest.mark.asyncio
async def test_reserve_api_key_usage_preserves_enforcement_inputs() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)

    reservation = await service._reserve_websocket_api_key_usage(
        _api_key(),
        request_model="gpt-requested",
        request_service_tier="priority",
    )

    assert reservation is repository.reservation
    assert repository.enforce_calls == [
        {
            "key_id": "key-1",
            "request_model": "gpt-requested",
            "request_service_tier": "priority",
        }
    ]


@pytest.mark.asyncio
async def test_reserve_api_key_usage_maps_limit_and_auth_errors() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)
    repository.enforce_error = ApiKeyRateLimitExceededError(
        message="limit reached",
        reset_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    with pytest.raises(ProxyRateLimitError, match=r"Usage resets at 2026-01-02T00:00:00\+00:00Z"):
        await service._reserve_websocket_api_key_usage(
            _api_key(),
            request_model="gpt-requested",
            request_service_tier=None,
        )

    repository.enforce_error = ApiKeyInvalidError("invalid key")
    with pytest.raises(ProxyAuthError, match="invalid key"):
        await service._reserve_websocket_api_key_usage(
            _api_key(),
            request_model=None,
            request_service_tier=None,
        )


@pytest.mark.asyncio
async def test_release_api_key_usage_is_noop_without_reservation() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)

    await service._release_websocket_reservation(None)
    await service._release_websocket_reservation(repository.reservation)

    assert repository.release_calls == ["reservation-1"]


@pytest.mark.asyncio
async def test_compact_settlement_finalizes_complete_usage() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)
    response = CompactResponsePayload.model_validate(
        {
            "object": "response.compact",
            "model": "gpt-response",
            "service_tier": "priority",
            "usage": {
                "input_tokens": 30,
                "output_tokens": 12,
                "input_tokens_details": {"cached_tokens": 7},
            },
        }
    )

    await service._settle_compact_api_key_usage(
        api_key=_api_key(),
        api_key_reservation=repository.reservation,
        response=response,
        request_service_tier="default",
    )

    assert repository.finalize_calls == [
        (
            "reservation-1",
            {
                "model": "gpt-key",
                "input_tokens": 30,
                "output_tokens": 12,
                "cached_input_tokens": 7,
                    "cache_write_tokens": 0,
                    "service_tier": "priority",
                },
        )
    ]
    assert repository.release_calls == []


@pytest.mark.asyncio
async def test_compact_settlement_releases_missing_response() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)

    await service._settle_compact_api_key_usage(
        api_key=_api_key(),
        api_key_reservation=repository.reservation,
        response=None,
        request_service_tier="default",
    )

    assert repository.finalize_calls == []
    assert repository.release_calls == ["reservation-1"]


@pytest.mark.asyncio
async def test_compact_authoritative_settlement_retries_without_releasing_on_persistence_failure() -> None:
    repository = _FakeApiKeyRepository()
    repository.settlement_error = RuntimeError("database unavailable")
    service = _ApiKeyUsageHarness(repository)
    response = CompactResponsePayload.model_validate(
        {
            "object": "response.compact",
            "model": "gpt-5.6-sol",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 80},
            },
        }
    )

    await service._settle_compact_api_key_usage(
        api_key=None,
        api_key_reservation=repository.reservation,
        response=response,
        request_service_tier="priority",
    )

    assert repository.settlement_attempts == 2
    assert repository.finalize_calls == []
    assert repository.release_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage_payload", "expected_usage"),
    [
        (
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 80},
            },
                {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cached_input_tokens": 20,
                    "cache_write_tokens": 80,
                },
        ),
        (
            {"output_tokens": 7},
            {
                "input_tokens": 0,
                "output_tokens": 7,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
            },
        ),
        (
            {"input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 80}},
            {
                "input_tokens": 100,
                "output_tokens": 0,
                "cached_input_tokens": 20,
                "cache_write_tokens": 80,
            },
        ),
        (
            {"output_tokens_details": {"reasoning_tokens": 9}},
            {
                "input_tokens": 0,
                "output_tokens": 9,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
            },
        ),
    ],
)
async def test_compact_partial_authoritative_usage_is_failed_settled_instead_of_released(
    usage_payload: dict[str, object],
    expected_usage: dict[str, int | None],
) -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)
    response = CompactResponsePayload.model_validate(
        {
            "object": "response.compact",
            "model": "gpt-5.6-sol",
            "usage": usage_payload,
        }
    )

    await service._settle_compact_api_key_usage(
        api_key=_api_key(),
        api_key_reservation=repository.reservation,
        response=response,
        request_service_tier="priority",
    )

    assert repository.finalize_calls == []
    assert repository.fail_calls == [
        (
            "reservation-1",
            {
                "model": "gpt-key",
                **expected_usage,
                "service_tier": "priority",
            },
        )
    ]
    assert repository.release_calls == []


@pytest.mark.asyncio
async def test_stream_settlement_preserves_success_and_release_paths() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)
    success = _StreamSettlement(
        status="success",
        model="gpt-stream",
        service_tier="priority",
        input_tokens=40,
        output_tokens=20,
        cached_input_tokens=9,
    )

    assert await service._settle_stream_api_key_usage(_api_key(), repository.reservation, success, "request-1")
    assert repository.finalize_calls == [
        (
            "reservation-1",
            {
                "model": "gpt-key",
                "input_tokens": 40,
                "output_tokens": 20,
                "cached_input_tokens": 9,
                "cache_write_tokens": 0,
                "service_tier": "priority",
                "usage_charges": None,
            },
        )
    ]

    repository.finalize_calls.clear()
    incomplete = _StreamSettlement(status="error", model="gpt-stream")
    assert await service._settle_stream_api_key_usage(_api_key(), repository.reservation, incomplete, "request-2")
    assert repository.release_calls == ["reservation-1"]


@pytest.mark.asyncio
async def test_stream_settlement_failure_isolated_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _FakeApiKeyRepository()
    repository.settlement_error = RuntimeError("database unavailable")
    service = _ApiKeyUsageHarness(repository)
    settlement = _StreamSettlement(status="error")

    with caplog.at_level(logging.WARNING, logger="app.modules.proxy.service"):
        settled = await service._settle_stream_api_key_usage(
            _api_key(), repository.reservation, settlement, "request-3"
        )

    assert settled is False
    assert "Failed to settle stream API key reservation key_id=key-1 request_id=request-3" in caplog.text


@pytest.mark.asyncio
async def test_failed_stream_settlement_uses_reservation_key_when_api_key_context_is_absent() -> None:
    repository = _FakeApiKeyRepository()
    service = _ApiKeyUsageHarness(repository)
    charge = ApiKeyUsageCharge(
        model="gpt-5.6-sol",
        input_tokens=100,
        output_tokens=2,
        cached_input_tokens=20,
        cache_write_tokens=80,
        service_tier="priority",
    )
    settlement = _StreamSettlement(
        status="error",
        model="gpt-5.6-sol",
        service_tier="priority",
        input_tokens=100,
        output_tokens=2,
        cached_input_tokens=20,
        cache_write_tokens=80,
        usage_charges=(charge,),
        error_code="stream_incomplete",
    )

    assert await service._settle_stream_api_key_usage(
        None,
        repository.reservation,
        settlement,
        "request-without-api-key-context",
    )
    assert repository.fail_calls == [
        (
            "reservation-1",
            {
                "model": "gpt-key",
                "input_tokens": 100,
                "output_tokens": 2,
                "cached_input_tokens": 20,
                "cache_write_tokens": 80,
                "service_tier": "priority",
                "usage_charges": (charge,),
            },
        )
    ]
    assert repository.release_calls == []
