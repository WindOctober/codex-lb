from __future__ import annotations

import logging
from typing import Protocol

import anyio

from app.core.exceptions import ProxyAuthError, ProxyRateLimitError
from app.core.openai.models import CompactResponsePayload
from app.core.utils.request_id import get_request_id
from app.modules.api_keys.service import (
    ApiKeyData,
    ApiKeyInvalidError,
    ApiKeyRateLimitExceededError,
    ApiKeysService,
    ApiKeyUsageReservationData,
)
from app.modules.proxy._service.service_tier import _service_tier_from_response
from app.modules.proxy._service.support import _StreamSettlement
from app.modules.proxy.repo_bundle import ProxyRepoFactory

logger = logging.getLogger("app.modules.proxy.service")


class _ApiKeyUsageRuntimeService(Protocol):
    _repo_factory: ProxyRepoFactory


class _ApiKeyUsageRuntimeMixin:
    async def _reserve_websocket_api_key_usage(
        self: _ApiKeyUsageRuntimeService,
        api_key: ApiKeyData | None,
        *,
        request_model: str | None,
        request_service_tier: str | None,
    ) -> ApiKeyUsageReservationData | None:
        if api_key is None:
            return None

        with anyio.CancelScope(shield=True):
            async with self._repo_factory() as repos:
                service = ApiKeysService(repos.api_keys)
                try:
                    return await service.enforce_limits_for_request(
                        api_key.id,
                        request_model=request_model,
                        request_service_tier=request_service_tier,
                    )
                except ApiKeyRateLimitExceededError as exc:
                    message = f"{exc}. Usage resets at {exc.reset_at.isoformat()}Z."
                    raise ProxyRateLimitError(message) from exc
                except ApiKeyInvalidError as exc:
                    raise ProxyAuthError(str(exc)) from exc

    async def _release_websocket_reservation(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData | None,
    ) -> None:
        if reservation is None:
            return
        with anyio.CancelScope(shield=True):
            async with self._repo_factory() as repos:
                service = ApiKeysService(repos.api_keys)
                await service.release_usage_reservation(reservation.reservation_id)

    async def _settle_compact_api_key_usage(
        self: _ApiKeyUsageRuntimeService,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        response: CompactResponsePayload | None,
        request_service_tier: str | None,
    ) -> None:
        if api_key is None or api_key_reservation is None:
            return

        reservation_id = api_key_reservation.reservation_id
        usage = response.usage if response is not None else None
        input_tokens = usage.input_tokens if usage else None
        output_tokens = usage.output_tokens if usage else None
        cached_input_tokens = usage.input_tokens_details.cached_tokens if usage and usage.input_tokens_details else 0
        model_name = api_key_reservation.model or (getattr(response, "model", None) or "")
        response_service_tier = _service_tier_from_response(response)
        service_tier = (
            response_service_tier
            if isinstance(response_service_tier, str)
            else request_service_tier
            if isinstance(request_service_tier, str)
            else None
        )

        with anyio.CancelScope(shield=True):
            try:
                async with self._repo_factory() as repos:
                    api_keys_service = ApiKeysService(repos.api_keys)
                    if response is not None and input_tokens is not None and output_tokens is not None:
                        await api_keys_service.finalize_usage_reservation(
                            reservation_id,
                            model=model_name,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_input_tokens=cached_input_tokens or 0,
                            service_tier=service_tier,
                        )
                    else:
                        await api_keys_service.release_usage_reservation(reservation_id)
            except Exception:
                logger.warning(
                    "Failed to settle compact API key reservation key_id=%s request_id=%s",
                    api_key.id,
                    get_request_id(),
                    exc_info=True,
                )

    async def _settle_stream_api_key_usage(
        self: _ApiKeyUsageRuntimeService,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool:
        """Settle stream reservation. Returns True if settled."""
        if api_key is None or api_key_reservation is None:
            return True

        reservation_id = api_key_reservation.reservation_id
        model_name = api_key_reservation.model or settlement.model or ""

        settled: bool = False
        with anyio.CancelScope(shield=True):
            try:
                async with self._repo_factory() as repos:
                    api_keys_service = ApiKeysService(repos.api_keys)
                    if (
                        settlement.status == "success"
                        and settlement.input_tokens is not None
                        and settlement.output_tokens is not None
                    ):
                        await api_keys_service.finalize_usage_reservation(
                            reservation_id,
                            model=model_name,
                            input_tokens=settlement.input_tokens,
                            output_tokens=settlement.output_tokens,
                            cached_input_tokens=settlement.cached_input_tokens or 0,
                            service_tier=settlement.service_tier,
                        )
                    else:
                        await api_keys_service.release_usage_reservation(reservation_id)
                settled = True
            except Exception:
                logger.warning(
                    "Failed to settle stream API key reservation key_id=%s request_id=%s",
                    api_key.id,
                    request_id,
                    exc_info=True,
                )
                settled = False

        return settled
