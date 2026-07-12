from __future__ import annotations

import logging
from typing import Protocol

from app.core.clients.proxy import ProxyResponseError
from app.core.errors import OpenAIErrorEnvelope, openai_error
from app.core.openai.model_registry import ModelRegistry
from app.db.models import AccountStatus
from app.modules.api_keys.service import ApiKeyData
from app.modules.proxy._service.affinity import _normalize_session_id
from app.modules.proxy._service.http_bridge.policy import (
    _account_supports_http_bridge_request_model,
)
from app.modules.proxy._service.support import _DurableAccountBinding
from app.modules.proxy.durable_bridge_coordinator import DurableBridgeLookup
from app.modules.proxy.load_balancer import _clone_account
from app.modules.proxy.repo_bundle import ProxyRepoFactory

logger = logging.getLogger("app.modules.proxy.service")

_WEBSOCKET_PREVIOUS_RESPONSE_ACCOUNT_CACHE_LIMIT = 4096


def _previous_response_owner_lookup_failed_error_envelope() -> OpenAIErrorEnvelope:
    return openai_error(
        "upstream_unavailable",
        "Previous response owner lookup failed; retry later.",
        error_type="server_error",
    )


class _ContinuityRuntimeService(Protocol):
    _repo_factory: ProxyRepoFactory
    _websocket_previous_response_account_index: dict[tuple[str, str | None, str | None], str]

    def _remember_websocket_previous_response_owner(
        self,
        *,
        previous_response_id: str | None,
        api_key_id: str | None,
        account_id: str | None,
        session_id: str | None = None,
    ) -> None: ...

    @staticmethod
    def _continuity_model_registry() -> ModelRegistry: ...

    @staticmethod
    def _record_continuity_owner_resolution_compatible(
        *,
        surface: str,
        source: str,
        outcome: str,
        previous_response_id: str | None,
        session_id: str | None,
    ) -> None: ...

    @staticmethod
    def _record_continuity_fail_closed_compatible(
        *,
        surface: str,
        reason: str,
        previous_response_id: str | None,
        session_id: str | None = None,
        upstream_error_code: str | None = None,
    ) -> None: ...


class _ContinuityRuntimeMixin:
    async def _durable_account_binding(
        self: _ContinuityRuntimeService,
        durable_lookup: DurableBridgeLookup | None,
        *,
        request_model: str | None,
    ) -> _DurableAccountBinding:
        if durable_lookup is None or durable_lookup.account_id is None:
            return _DurableAccountBinding(account_id=None, supports_request_model=True)
        try:
            async with self._repo_factory() as repos:
                account = await repos.accounts.get_by_id(durable_lookup.account_id)
                account = _clone_account(account) if account is not None else None
        except Exception:
            logger.warning(
                "Failed to validate durable HTTP bridge account model support; preserving durable binding",
                exc_info=True,
            )
            return _DurableAccountBinding(
                account_id=durable_lookup.account_id,
                supports_request_model=True,
            )
        if account is None or account.status != AccountStatus.ACTIVE:
            return _DurableAccountBinding(account_id=None, supports_request_model=False)
        return _DurableAccountBinding(
            account_id=account.id,
            supports_request_model=_account_supports_http_bridge_request_model(
                account,
                request_model,
                model_registry=self._continuity_model_registry(),
            ),
        )

    def _remember_websocket_previous_response_owner(
        self: _ContinuityRuntimeService,
        *,
        previous_response_id: str | None,
        api_key_id: str | None,
        account_id: str | None,
        session_id: str | None = None,
    ) -> None:
        if previous_response_id is None or account_id is None:
            return
        response_id = previous_response_id.strip()
        if not response_id:
            return
        account_id_value = account_id.strip()
        if not account_id_value:
            return
        cache_keys = [(response_id, api_key_id, None)]
        normalized_session_id = _normalize_session_id(session_id)
        if normalized_session_id is not None:
            cache_keys.append((response_id, api_key_id, normalized_session_id))
        for cache_key in cache_keys:
            self._websocket_previous_response_account_index.pop(cache_key, None)
            self._websocket_previous_response_account_index[cache_key] = account_id_value
        while len(self._websocket_previous_response_account_index) > _WEBSOCKET_PREVIOUS_RESPONSE_ACCOUNT_CACHE_LIMIT:
            self._websocket_previous_response_account_index.pop(
                next(iter(self._websocket_previous_response_account_index))
            )

    def _remember_websocket_previous_response_owner_miss(
        self: _ContinuityRuntimeService,
        *,
        previous_response_id: str | None,
        api_key_id: str | None,
        request_cache_scope: str | None,
    ) -> None:
        del previous_response_id, api_key_id, request_cache_scope
        # Negative caching caused stale misses under concurrent sessions.
        return None

    async def _resolve_websocket_previous_response_owner(
        self: _ContinuityRuntimeService,
        *,
        previous_response_id: str | None,
        api_key: ApiKeyData | None,
        session_id: str | None = None,
        surface: str,
    ) -> str | None:
        if previous_response_id is None:
            return None
        response_id = previous_response_id.strip()
        if not response_id:
            return None
        api_key_id = api_key.id if api_key is not None else None
        session_id_value = _normalize_session_id(session_id)
        cache_key = (response_id, api_key_id, session_id_value)
        cached_account_id = self._websocket_previous_response_account_index.get(cache_key)
        if cached_account_id is not None:
            self._record_continuity_owner_resolution_compatible(
                surface=surface,
                source="request_cache",
                outcome="hit",
                previous_response_id=response_id,
                session_id=session_id_value,
            )
            return cached_account_id
        fallback_account_id = (
            self._websocket_previous_response_account_index.get((response_id, api_key_id, None))
            if session_id_value is not None
            else None
        )
        try:
            async with self._repo_factory() as repos:
                account_id = await repos.request_logs.find_latest_account_id_for_response_id(
                    response_id=response_id,
                    api_key_id=api_key_id,
                    session_id=session_id_value,
                )
        except Exception as exc:
            if fallback_account_id is not None:
                self._record_continuity_owner_resolution_compatible(
                    surface=surface,
                    source="request_cache_fallback",
                    outcome="hit",
                    previous_response_id=response_id,
                    session_id=session_id_value,
                )
                logger.warning(
                    "Previous response owner lookup failed; using cached owner pin",
                    exc_info=True,
                )
                return fallback_account_id
            self._record_continuity_owner_resolution_compatible(
                surface=surface,
                source="request_logs",
                outcome="fail_closed",
                previous_response_id=response_id,
                session_id=session_id_value,
            )
            self._record_continuity_fail_closed_compatible(
                surface=surface,
                reason="owner_lookup_failed",
                previous_response_id=response_id,
                session_id=session_id_value,
            )
            logger.warning("Previous response owner lookup failed; failing closed", exc_info=True)
            raise ProxyResponseError(
                502,
                _previous_response_owner_lookup_failed_error_envelope(),
            ) from exc
        if account_id is None:
            if fallback_account_id is not None:
                self._record_continuity_owner_resolution_compatible(
                    surface=surface,
                    source="request_cache_fallback",
                    outcome="hit",
                    previous_response_id=response_id,
                    session_id=session_id_value,
                )
            else:
                self._record_continuity_owner_resolution_compatible(
                    surface=surface,
                    source="request_logs",
                    outcome="miss",
                    previous_response_id=response_id,
                    session_id=session_id_value,
                )
            return fallback_account_id
        self._remember_websocket_previous_response_owner(
            previous_response_id=response_id,
            api_key_id=api_key_id,
            account_id=account_id,
            session_id=session_id_value,
        )
        self._record_continuity_owner_resolution_compatible(
            surface=surface,
            source="request_logs",
            outcome="hit",
            previous_response_id=response_id,
            session_id=session_id_value,
        )
        return account_id
