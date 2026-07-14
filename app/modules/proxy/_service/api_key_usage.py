from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import anyio

from app.core.exceptions import ProxyAuthError, ProxyRateLimitError
from app.core.openai.models import CompactResponsePayload, normalize_response_usage
from app.core.utils.request_id import get_request_id
from app.modules.api_keys.service import (
    ApiKeyData,
    ApiKeyInvalidError,
    ApiKeyRateLimitExceededError,
    ApiKeysService,
    ApiKeyUsageReservationData,
)
from app.modules.proxy._service.service_tier import _service_tier_from_response
from app.modules.proxy._service.support import (
    _await_operation_before_hard_timeout,
    _await_shielded_cleanup,
    _close_tracked_background_tasks,
    _schedule_tracked_background_task,
    _StreamSettlement,
    _track_existing_background_task,
)
from app.modules.proxy.repo_bundle import ProxyRepoFactory

logger = logging.getLogger("app.modules.proxy.service")
_RESERVATION_RELEASE_ATTEMPT_TIMEOUT_SECONDS = 1.0


class _ApiKeyUsageRuntimeService(Protocol):
    _repo_factory: ProxyRepoFactory
    _proxy_cleanup_tasks: set[asyncio.Task[None]]

    async def _release_websocket_reservation(
        self,
        reservation: ApiKeyUsageReservationData | None,
    ) -> None: ...

    async def _release_unclaimed_websocket_reservation(
        self,
        reservation: ApiKeyUsageReservationData | None,
    ) -> None: ...

    async def _forwarded_websocket_reservation_status(
        self,
        reservation: ApiKeyUsageReservationData,
    ) -> str | None: ...

    async def _release_websocket_reservation_with_retry(
        self,
        reservation: ApiKeyUsageReservationData,
        *,
        reason: str,
    ) -> None: ...

    async def _release_unclaimed_websocket_reservation_with_retry(
        self,
        reservation: ApiKeyUsageReservationData,
        *,
        reason: str,
    ) -> None: ...

    async def _settle_stream_api_key_usage(
        self,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool: ...


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

        async def release() -> None:
            async with self._repo_factory() as repos:
                service = ApiKeysService(repos.api_keys)
                await service.release_usage_reservation(reservation.reservation_id)

        await _await_shielded_cleanup(release(), label="websocket API-key reservation release")

    async def _claim_forwarded_websocket_reservation(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData | None,
    ) -> bool:
        if reservation is None:
            return True

        async with self._repo_factory() as repos:
            service = ApiKeysService(repos.api_keys)
            return await service.claim_usage_reservation_for_forwarding(reservation.reservation_id)

    async def _forwarded_websocket_reservation_status(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData,
    ) -> str | None:
        async with self._repo_factory() as repos:
            service = ApiKeysService(repos.api_keys)
            return await service.usage_reservation_status(reservation.reservation_id)

    async def _release_websocket_reservation_with_retry(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData,
        *,
        reason: str,
    ) -> None:
        for attempt in range(2):
            try:
                await _await_operation_before_hard_timeout(
                    self._release_websocket_reservation(reservation),
                    timeout_seconds=_RESERVATION_RELEASE_ATTEMPT_TIMEOUT_SECONDS,
                    tasks=self._proxy_cleanup_tasks,
                    label=f"API-key reservation release reservation_id={reservation.reservation_id}",
                )
                return
            except Exception:
                if attempt > 0:
                    raise
                logger.warning(
                    "API-key reservation release failed; retrying reservation_id=%s reason=%s",
                    reservation.reservation_id,
                    reason,
                    exc_info=True,
                )

    def _schedule_websocket_reservation_release(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData | None,
        *,
        reason: str,
    ) -> None:
        if reservation is None:
            return

        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            self._release_websocket_reservation_with_retry(reservation, reason=reason),
            name=f"reservation-release-{reservation.reservation_id}",
            label=f"API-key reservation release reason={reason} reservation_id={reservation.reservation_id}",
        )

    def _schedule_forwarded_reservation_claim_reconciliation(
        self: _ApiKeyUsageRuntimeService,
        claim_task: asyncio.Task[bool],
        reservation: ApiKeyUsageReservationData,
    ) -> None:
        async def read_durable_status_with_retry() -> str | None:
            for attempt in range(2):
                try:
                    return await _await_operation_before_hard_timeout(
                        self._forwarded_websocket_reservation_status(reservation),
                        timeout_seconds=_RESERVATION_RELEASE_ATTEMPT_TIMEOUT_SECONDS,
                        tasks=self._proxy_cleanup_tasks,
                        label=(
                            "forwarded API-key reservation status reconciliation "
                            f"reservation_id={reservation.reservation_id}"
                        ),
                    )
                except Exception:
                    if attempt > 0:
                        raise
                    logger.warning(
                        "Forwarded API-key reservation status reconciliation failed; retrying reservation_id=%s",
                        reservation.reservation_id,
                        exc_info=True,
                    )
            raise AssertionError("unreachable")

        async def reconcile_claim() -> None:
            async def reconcile_durable_status(reason_prefix: str) -> None:
                status = await read_durable_status_with_retry()
                if status == "owner_accepted":
                    await self._release_websocket_reservation_with_retry(
                        reservation,
                        reason=f"{reason_prefix}-forwarded-claim-owner-accepted",
                    )
                elif status == "reserved":
                    await self._release_unclaimed_websocket_reservation_with_retry(
                        reservation,
                        reason=f"{reason_prefix}-forwarded-claim-reserved",
                    )

            try:
                claimed = await asyncio.shield(claim_task)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    while not claim_task.done():
                        try:
                            await asyncio.shield(claim_task)
                        except asyncio.CancelledError:
                            if claim_task.done():
                                break
                    if claim_task.cancelled():
                        await reconcile_durable_status("cancelled")
                        return
                    try:
                        claimed = claim_task.result()
                    except Exception:
                        await reconcile_durable_status("ambiguous")
                        return
            except Exception:
                await reconcile_durable_status("ambiguous")
                return
            if claimed:
                await self._release_websocket_reservation_with_retry(
                    reservation,
                    reason="late-forwarded-claim",
                )

        _track_existing_background_task(
            self._proxy_cleanup_tasks,
            claim_task,
            label=f"forwarded reservation claim reservation_id={reservation.reservation_id}",
        )
        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            reconcile_claim(),
            name=f"forwarded-reservation-reconcile-{reservation.reservation_id}",
            label=f"forwarded reservation claim reconciliation reservation_id={reservation.reservation_id}",
        )

    async def close_proxy_cleanup_tasks(self: _ApiKeyUsageRuntimeService) -> None:
        await _close_tracked_background_tasks(
            self._proxy_cleanup_tasks,
            label="proxy cleanup tasks",
        )

    async def _release_unclaimed_websocket_reservation(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData | None,
    ) -> None:
        if reservation is None:
            return

        async def release() -> None:
            async with self._repo_factory() as repos:
                service = ApiKeysService(repos.api_keys)
                await service.release_unclaimed_usage_reservation(reservation.reservation_id)

        await _await_shielded_cleanup(
            release(),
            label="unclaimed forwarded API-key reservation release",
        )

    async def _release_unclaimed_websocket_reservation_with_retry(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData,
        *,
        reason: str,
    ) -> None:
        for attempt in range(2):
            try:
                await _await_operation_before_hard_timeout(
                    self._release_unclaimed_websocket_reservation(reservation),
                    timeout_seconds=_RESERVATION_RELEASE_ATTEMPT_TIMEOUT_SECONDS,
                    tasks=self._proxy_cleanup_tasks,
                    label=f"unclaimed API-key reservation release reservation_id={reservation.reservation_id}",
                )
                return
            except Exception:
                if attempt > 0:
                    raise
                logger.warning(
                    "Unclaimed API-key reservation release failed; retrying reservation_id=%s reason=%s",
                    reservation.reservation_id,
                    reason,
                    exc_info=True,
                )

    def _schedule_unclaimed_websocket_reservation_release(
        self: _ApiKeyUsageRuntimeService,
        reservation: ApiKeyUsageReservationData | None,
        *,
        reason: str,
    ) -> None:
        if reservation is None:
            return
        _schedule_tracked_background_task(
            self._proxy_cleanup_tasks,
            self._release_unclaimed_websocket_reservation_with_retry(reservation, reason=reason),
            name=f"unclaimed-reservation-release-{reservation.reservation_id}",
            label=(
                "unclaimed forwarded API-key reservation release "
                f"reason={reason} reservation_id={reservation.reservation_id}"
            ),
        )

    async def _settle_compact_api_key_usage(
        self: _ApiKeyUsageRuntimeService,
        *,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        response: CompactResponsePayload | None,
        request_service_tier: str | None,
    ) -> None:
        if api_key_reservation is None:
            return

        reservation_id = api_key_reservation.reservation_id
        usage = response.usage if response is not None else None
        normalized_usage = normalize_response_usage(usage)
        input_tokens = normalized_usage.input_tokens if normalized_usage is not None else None
        output_tokens = normalized_usage.output_tokens if normalized_usage is not None else None
        cached_input_tokens = (
            normalized_usage.cached_input_tokens if normalized_usage is not None else None
        )
        cache_write_tokens = normalized_usage.cache_write_tokens if normalized_usage is not None else None
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
            for attempt in range(2):
                try:
                    async with self._repo_factory() as repos:
                        api_keys_service = ApiKeysService(repos.api_keys)
                        if (
                            response is not None
                            and usage is not None
                            and usage.input_tokens is not None
                            and usage.output_tokens is not None
                            and normalized_usage is not None
                        ):
                            await api_keys_service.finalize_usage_reservation(
                                reservation_id,
                                model=model_name,
                                input_tokens=normalized_usage.input_tokens,
                                output_tokens=normalized_usage.output_tokens,
                                cached_input_tokens=normalized_usage.cached_input_tokens,
                                cache_write_tokens=normalized_usage.cache_write_tokens,
                                service_tier=service_tier,
                            )
                        elif normalized_usage is not None:
                            await api_keys_service.fail_usage_reservation(
                                reservation_id,
                                model=model_name,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_input_tokens=cached_input_tokens,
                                cache_write_tokens=cache_write_tokens,
                                service_tier=service_tier,
                            )
                        else:
                            await api_keys_service.release_usage_reservation(reservation_id)
                    return
                except Exception:
                    if attempt == 0:
                        logger.warning(
                            "Compact API key reservation settlement failed; retrying "
                            "reservation_id=%s request_id=%s",
                            reservation_id,
                            get_request_id(),
                            exc_info=True,
                        )
                        continue
                    logger.warning(
                        "Failed to settle compact API key reservation key_id=%s request_id=%s; "
                        "reservation retained because authoritative usage may exist",
                        api_key.id if api_key is not None else api_key_reservation.key_id,
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
        if api_key_reservation is None:
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
                            cache_write_tokens=settlement.cache_write_tokens or 0,
                            service_tier=settlement.service_tier,
                            usage_charges=settlement.usage_charges or None,
                        )
                    elif any(
                        value is not None
                        for value in (
                            settlement.input_tokens,
                            settlement.output_tokens,
                            settlement.cached_input_tokens,
                            settlement.cache_write_tokens,
                        )
                    ):
                        await api_keys_service.fail_usage_reservation(
                            reservation_id,
                            model=model_name,
                            input_tokens=settlement.input_tokens,
                            output_tokens=settlement.output_tokens,
                            cached_input_tokens=settlement.cached_input_tokens,
                            cache_write_tokens=settlement.cache_write_tokens,
                            service_tier=settlement.service_tier,
                            usage_charges=settlement.usage_charges or None,
                        )
                    else:
                        await api_keys_service.release_usage_reservation(reservation_id)
                settled = True
            except Exception:
                logger.warning(
                    "Failed to settle stream API key reservation key_id=%s request_id=%s",
                    api_key.id if api_key is not None else api_key_reservation.key_id,
                    request_id,
                    exc_info=True,
                )
                settled = False

        return settled

    async def _settle_stream_api_key_usage_with_fallback(
        self: _ApiKeyUsageRuntimeService,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: _StreamSettlement,
        request_id: str,
    ) -> bool:
        """Finish reservation ownership even when final usage persistence fails."""

        async def settle_or_release() -> bool:
            settled = await self._settle_stream_api_key_usage(
                api_key,
                api_key_reservation,
                settlement,
                request_id,
            )
            if settled or api_key_reservation is None:
                return settled
            has_authoritative_usage = bool(settlement.usage_charges) or any(
                value is not None
                for value in (
                    settlement.input_tokens,
                    settlement.output_tokens,
                    settlement.cached_input_tokens,
                    settlement.cache_write_tokens,
                )
            )
            if has_authoritative_usage:
                logger.warning(
                    "Retrying authoritative API-key usage settlement without fallback release "
                    "reservation_id=%s request_id=%s",
                    api_key_reservation.reservation_id,
                    request_id,
                )
                settled = await self._settle_stream_api_key_usage(
                    api_key,
                    api_key_reservation,
                    settlement,
                    request_id,
                )
                if not settled:
                    logger.error(
                        "Authoritative API-key usage settlement failed twice; reservation retained "
                        "reservation_id=%s request_id=%s",
                        api_key_reservation.reservation_id,
                        request_id,
                    )
                return settled
            logger.warning(
                "Falling back to API-key reservation release reservation_id=%s request_id=%s",
                api_key_reservation.reservation_id,
                request_id,
            )
            await self._release_websocket_reservation(api_key_reservation)
            return True

        return await _await_shielded_cleanup(
            settle_or_release(),
            label=f"stream API-key reservation settlement request_id={request_id}",
        )
