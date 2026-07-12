from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import ClassVar

import anyio
import pytest

from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import (
    UpstreamResponsesWebSocket,
    UpstreamWebSocketMessage,
)
from app.core.crypto import TokenEncryptor
from app.db.models import ACCOUNT_PROVIDER_API_KEY, ACCOUNT_PROVIDER_OPENAI_OAUTH, Account
from app.modules.proxy import service as proxy_service
from app.modules.proxy._service.upstream_websocket import (
    _UpstreamWebSocketRuntimeMixin,
    _UpstreamWebSocketRuntimeService,
)
from app.modules.proxy.work_admission import (
    AdmissionLease,
    WorkAdmissionController,
)

pytestmark = pytest.mark.unit


class _FakeUpstreamWebSocket:
    async def send_text(self, text: str) -> None:
        del text

    async def send_bytes(self, data: bytes) -> None:
        del data

    async def receive(self) -> UpstreamWebSocketMessage:
        return UpstreamWebSocketMessage(kind="close")

    async def close(self) -> None:
        return None

    def response_header(self, name: str) -> str | None:
        del name
        return None


class _RecordingEncryptor(TokenEncryptor):
    def __init__(self, decrypted_token: str) -> None:
        self.decrypted_token = decrypted_token
        self.decrypt_calls: list[bytes] = []

    def decrypt(self, encrypted: bytes) -> str:
        self.decrypt_calls.append(encrypted)
        return self.decrypted_token


class _RecordingAdmissionLease(AdmissionLease):
    def __init__(self) -> None:
        super().__init__(None)
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        super().release()


class _RecordingWorkAdmission(WorkAdmissionController):
    def __init__(self, lease: _RecordingAdmissionLease) -> None:
        self.lease = lease
        self.acquire_calls = 0

    async def acquire_websocket_connect(self) -> AdmissionLease:
        self.acquire_calls += 1
        return self.lease


@dataclass(frozen=True, slots=True)
class _FactoryCall:
    headers: dict[str, str]
    access_token: str
    account_id: str | None
    base_url: str | None
    wire_api: str


class _RecordingFactory:
    def __init__(
        self,
        upstream: UpstreamResponsesWebSocket,
        *,
        error: BaseException | None = None,
        wait_forever: bool = False,
    ) -> None:
        self.upstream = upstream
        self.error = error
        self.wait_forever = wait_forever
        self.calls: list[_FactoryCall] = []

    async def connect(
        self,
        headers: dict[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
    ) -> UpstreamResponsesWebSocket:
        self.calls.append(
            _FactoryCall(
                headers=headers,
                access_token=access_token,
                account_id=account_id,
                base_url=base_url,
                wire_api=wire_api,
            )
        )
        if self.wait_forever:
            await anyio.sleep_forever()
        if self.error is not None:
            raise self.error
        return self.upstream


class _FakeService(_UpstreamWebSocketRuntimeMixin):
    _active_factory: ClassVar[_RecordingFactory]
    _encryptor: TokenEncryptor

    def __init__(
        self,
        encryptor: TokenEncryptor,
        admission: WorkAdmissionController,
        factory: _RecordingFactory,
    ) -> None:
        self._encryptor = encryptor
        self._admission = admission
        type(self)._active_factory = factory

    def _get_work_admission(self) -> WorkAdmissionController:
        return self._admission

    @staticmethod
    async def _connect_responses_websocket_compatible(
        headers: dict[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
    ) -> UpstreamResponsesWebSocket:
        return await _FakeService._active_factory.connect(
            headers,
            access_token,
            account_id,
            base_url=base_url,
            wire_api=wire_api,
        )


def _account(
    *,
    provider_kind: str,
    chatgpt_account_id: str | None,
    base_url: str | None,
    wire_api: str | None,
) -> Account:
    return Account(
        id="account-1",
        chatgpt_account_id=chatgpt_account_id,
        email="account-1@example.com",
        plan_type="plus",
        provider_kind=provider_kind,
        upstream_base_url=base_url,
        upstream_wire_api=wire_api,
        access_token_encrypted=b"encrypted-access-token",
    )


def _service(
    *,
    factory_error: BaseException | None = None,
    wait_forever: bool = False,
) -> tuple[
    _FakeService,
    _RecordingEncryptor,
    _RecordingWorkAdmission,
    _RecordingAdmissionLease,
    _RecordingFactory,
    _FakeUpstreamWebSocket,
]:
    upstream = _FakeUpstreamWebSocket()
    factory = _RecordingFactory(
        upstream,
        error=factory_error,
        wait_forever=wait_forever,
    )
    encryptor = _RecordingEncryptor("decrypted-access-token")
    lease = _RecordingAdmissionLease()
    admission = _RecordingWorkAdmission(lease)
    service = _FakeService(encryptor, admission, factory)
    service_contract: _UpstreamWebSocketRuntimeService = service
    assert service_contract is service
    return service, encryptor, admission, lease, factory, upstream


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "provider_kind",
        "chatgpt_account_id",
        "base_url",
        "wire_api",
        "expected_account_id",
        "expected_base_url",
        "expected_wire_api",
    ),
    [
        (
            ACCOUNT_PROVIDER_OPENAI_OAUTH,
            "workspace-account-1",
            "https://ignored.example.test",
            "responses",
            "workspace-account-1",
            None,
            "codex",
        ),
        (
            ACCOUNT_PROVIDER_API_KEY,
            None,
            "https://provider.example.test/v1",
            "responses",
            None,
            "https://provider.example.test/v1",
            "responses",
        ),
    ],
)
async def test_open_upstream_websocket_forwards_supported_account_contract(
    provider_kind: str,
    chatgpt_account_id: str | None,
    base_url: str | None,
    wire_api: str | None,
    expected_account_id: str | None,
    expected_base_url: str | None,
    expected_wire_api: str,
) -> None:
    service, encryptor, admission, lease, factory, upstream = _service()
    account = _account(
        provider_kind=provider_kind,
        chatgpt_account_id=chatgpt_account_id,
        base_url=base_url,
        wire_api=wire_api,
    )
    headers = {"x-request-id": "request-1"}

    result = await service._open_upstream_websocket(account, headers)

    assert result is upstream
    assert encryptor.decrypt_calls == [b"encrypted-access-token"]
    assert admission.acquire_calls == 1
    assert lease.release_calls == 1
    assert factory.calls == [
        _FactoryCall(
            headers=headers,
            access_token="decrypted-access-token",
            account_id=expected_account_id,
            base_url=expected_base_url,
            wire_api=expected_wire_api,
        )
    ]
    assert factory.calls[0].headers is headers


@pytest.mark.asyncio
async def test_proxy_service_factory_compatibility_omits_unsupported_wire_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = _FakeUpstreamWebSocket()
    calls: list[_FactoryCall] = []

    async def legacy_factory(
        headers: dict[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
    ) -> UpstreamResponsesWebSocket:
        calls.append(
            _FactoryCall(
                headers=headers,
                access_token=access_token,
                account_id=account_id,
                base_url=base_url,
                wire_api="not-accepted-by-legacy-factory",
            )
        )
        return upstream

    monkeypatch.setattr(proxy_service, "connect_responses_websocket", legacy_factory)

    result = await proxy_service.ProxyService._connect_responses_websocket_compatible(
        {"x-request-id": "request-legacy"},
        "access-token",
        "account-id",
        base_url="https://provider.example.test/v1",
        wire_api="responses",
    )

    assert result is upstream
    assert calls == [
        _FactoryCall(
            headers={"x-request-id": "request-legacy"},
            access_token="access-token",
            account_id="account-id",
            base_url="https://provider.example.test/v1",
            wire_api="not-accepted-by-legacy-factory",
        )
    ]


@pytest.mark.asyncio
async def test_open_upstream_websocket_rejects_unsupported_provider_before_side_effects() -> None:
    service, encryptor, admission, lease, factory, _ = _service()
    account = _account(
        provider_kind=ACCOUNT_PROVIDER_API_KEY,
        chatgpt_account_id=None,
        base_url="https://provider.example.test/v1",
        wire_api="codex",
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._open_upstream_websocket(account, {})

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["error"].get("code") == "unsupported_transport"
    assert exc_info.value.payload["error"].get("message") == (
        "WebSocket transport is not supported by this upstream provider"
    )
    assert encryptor.decrypt_calls == []
    assert admission.acquire_calls == 0
    assert lease.release_calls == 0
    assert factory.calls == []


@pytest.mark.asyncio
async def test_open_upstream_websocket_releases_lease_and_preserves_factory_error() -> None:
    factory_error = RuntimeError("factory failed")
    service, _, admission, lease, factory, _ = _service(factory_error=factory_error)
    account = _account(
        provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
        chatgpt_account_id="workspace-account-1",
        base_url=None,
        wire_api=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await service._open_upstream_websocket(account, {})

    assert exc_info.value is factory_error
    assert admission.acquire_calls == 1
    assert lease.release_calls == 1
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_open_upstream_websocket_releases_lease_and_preserves_factory_cancellation() -> None:
    cancellation = asyncio.CancelledError("factory cancelled")
    service, _, admission, lease, factory, _ = _service(factory_error=cancellation)
    account = _account(
        provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
        chatgpt_account_id="workspace-account-1",
        base_url=None,
        wire_api=None,
    )

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await service._open_upstream_websocket(account, {})

    assert exc_info.value is cancellation
    assert admission.acquire_calls == 1
    assert lease.release_calls == 1
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_open_upstream_websocket_budget_timeout_maps_error_and_releases_lease() -> None:
    service, _, admission, lease, factory, _ = _service(wait_forever=True)
    account = _account(
        provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
        chatgpt_account_id="workspace-account-1",
        base_url=None,
        wire_api=None,
    )

    with pytest.raises(ProxyResponseError) as exc_info:
        await service._open_upstream_websocket_with_budget(
            account,
            {},
            timeout_seconds=0.01,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.payload["error"].get("code") == "upstream_unavailable"
    assert exc_info.value.payload["error"].get("message") == "Proxy request budget exhausted"
    assert admission.acquire_calls == 1
    assert lease.release_calls == 1
    assert len(factory.calls) == 1
