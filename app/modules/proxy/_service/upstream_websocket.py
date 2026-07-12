from __future__ import annotations

from typing import Protocol

import anyio

from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
from app.core.crypto import TokenEncryptor
from app.core.errors import openai_error
from app.db.models import Account
from app.modules.proxy._service.budget import _raise_proxy_budget_exhausted
from app.modules.proxy._service.upstream_account import (
    _account_upstream_base_url,
    _account_upstream_wire_api,
    _upstream_account_header_value,
    _websocket_disabled_for_account,
)
from app.modules.proxy.work_admission import WorkAdmissionController


class _UpstreamWebSocketRuntimeService(Protocol):
    _encryptor: TokenEncryptor

    def _get_work_admission(self) -> WorkAdmissionController: ...

    async def _open_upstream_websocket(
        self,
        account: Account,
        headers: dict[str, str],
    ) -> UpstreamResponsesWebSocket: ...

    @staticmethod
    async def _connect_responses_websocket_compatible(
        headers: dict[str, str],
        access_token: str,
        account_id: str | None,
        *,
        base_url: str | None,
        wire_api: str,
    ) -> UpstreamResponsesWebSocket: ...


class _UpstreamWebSocketRuntimeMixin:
    async def _open_upstream_websocket_with_budget(
        self: _UpstreamWebSocketRuntimeService,
        account: Account,
        headers: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> UpstreamResponsesWebSocket:
        try:
            with anyio.fail_after(timeout_seconds):
                return await self._open_upstream_websocket(account, headers)
        except TimeoutError:
            _raise_proxy_budget_exhausted()

    async def _open_upstream_websocket(
        self: _UpstreamWebSocketRuntimeService,
        account: Account,
        headers: dict[str, str],
    ) -> UpstreamResponsesWebSocket:
        if _websocket_disabled_for_account(account):
            raise ProxyResponseError(
                400,
                openai_error(
                    "unsupported_transport",
                    "WebSocket transport is not supported by this upstream provider",
                ),
            )
        access_token = self._encryptor.decrypt(account.access_token_encrypted)
        account_id = _upstream_account_header_value(account)
        connect_lease = await self._get_work_admission().acquire_websocket_connect()
        try:
            return await self._connect_responses_websocket_compatible(
                headers,
                access_token,
                account_id,
                base_url=_account_upstream_base_url(account),
                wire_api=_account_upstream_wire_api(account),
            )
        finally:
            connect_lease.release()
