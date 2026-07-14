from __future__ import annotations

import asyncio
from typing import Protocol

from app.core.clients.proxy import ProxyResponseError
from app.core.clients.proxy_websocket import UpstreamResponsesWebSocket
from app.core.crypto import TokenEncryptor
from app.core.errors import openai_error
from app.db.models import Account
from app.modules.proxy._service.budget import _raise_proxy_budget_exhausted
from app.modules.proxy._service.support import _await_operation_before_hard_timeout
from app.modules.proxy._service.upstream_account import (
    _account_upstream_base_url,
    _account_upstream_wire_api,
    _upstream_account_header_value,
    _websocket_disabled_for_account,
)
from app.modules.proxy.work_admission import WorkAdmissionController


class _UpstreamWebSocketRuntimeService(Protocol):
    _encryptor: TokenEncryptor
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

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
        cleanup_tasks: set[asyncio.Task[None]],
    ) -> UpstreamResponsesWebSocket: ...


class _UpstreamWebSocketRuntimeMixin:
    async def _open_upstream_websocket_with_budget(
        self: _UpstreamWebSocketRuntimeService,
        account: Account,
        headers: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> UpstreamResponsesWebSocket:
        class _NestedConnectTimeout(Exception):
            def __init__(self, cause: TimeoutError) -> None:
                super().__init__(str(cause))
                self.cause = cause

        async def open_upstream() -> UpstreamResponsesWebSocket:
            try:
                return await self._open_upstream_websocket(account, headers)
            except TimeoutError as exc:
                raise _NestedConnectTimeout(exc) from exc

        async def close_late_socket(socket: UpstreamResponsesWebSocket) -> None:
            await socket.close()

        try:
            return await _await_operation_before_hard_timeout(
                open_upstream(),
                timeout_seconds=timeout_seconds,
                tasks=self._proxy_cleanup_tasks,
                label=f"upstream WebSocket connect account_id={account.id}",
                late_result_cleanup=close_late_socket,
            )
        except _NestedConnectTimeout as exc:
            raise exc.cause
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
                cleanup_tasks=self._proxy_cleanup_tasks,
            )
        finally:
            connect_lease.release()
