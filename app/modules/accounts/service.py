from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, cast
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import ValidationError

from app.core import usage as usage_core
from app.core.account_groups import is_reserved_account_group
from app.core.auth import (
    DEFAULT_EMAIL,
    DEFAULT_PLAN,
    claims_from_auth,
    generate_unique_account_id,
    parse_auth_json,
)
from app.core.auth.api_key_cache import get_api_key_cache
from app.core.auth.refresh import RefreshError
from app.core.cache.invalidation import NAMESPACE_API_KEY, get_cache_invalidation_poller
from app.core.clients.upstream import UpstreamProbeError, probe_upstream_provider
from app.core.clients.usage import (
    UsageFetchError,
    fetch_rate_limit_reset_credits,
    fetch_usage,
)
from app.core.clients.usage import (
    consume_rate_limit_reset_credit as consume_upstream_rate_limit_reset_credit,
)
from app.core.crypto import TokenEncryptor
from app.core.plan_types import coerce_account_plan_type
from app.core.usage.models import RateLimitResetConsumePayload, RateLimitResetCreditBankPayload
from app.core.utils.time import naive_utc_to_epoch, to_utc_naive, utcnow
from app.db.models import (
    ACCOUNT_PROVIDER_API_KEY,
    ACCOUNT_PROVIDER_OPENAI_OAUTH,
    Account,
    AccountStatus,
    AdditionalUsageHistory,
    UsageHistory,
)
from app.modules.accounts.auth_manager import AuthManager
from app.modules.accounts.mappers import (
    build_account_quota_timeline,
    build_account_summaries,
    build_account_usage_trends,
)
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.schemas import (
    AccountAdditionalQuota,
    AccountAdditionalWindow,
    AccountAvailabilityResponse,
    AccountFastServiceTierBulkUpdateResponse,
    AccountImportResponse,
    AccountMergeResponse,
    AccountQuotaStatus,
    AccountQuotaWindow,
    AccountRateLimitResetConsumeResponse,
    AccountRateLimitResetCreditsResponse,
    AccountRequestUsage,
    AccountRuntimeState,
    AccountSummary,
    AccountTrendsResponse,
    AccountUpdateRequest,
    ApiProviderCreateRequest,
    ApiProviderCreateResponse,
)
from app.modules.proxy.account_cache import get_account_selection_cache
from app.modules.usage.additional_quota_keys import get_additional_display_label_for_quota_key
from app.modules.usage.latest_model import get_latest_model_quota_key
from app.modules.usage.repository import AdditionalUsageRepository, UsageRepository
from app.modules.usage.updater import AdditionalUsageRepositoryPort, UsageUpdater

_SPARKLINE_DAYS = 7
_DETAIL_BUCKET_SECONDS = 3600  # 1h → 168 points
_QUOTA_TIMELINE_BUCKET_SECONDS = 5 * 3600

logger = logging.getLogger(__name__)


class InvalidAuthJsonError(Exception):
    pass


class InvalidApiProviderError(ValueError):
    pass


class AccountMergeValidationError(ValueError):
    pass


class AccountResetCreditError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _AvailabilityOutcome:
    ok: bool
    status: AccountStatus
    reason: str | None = None
    reset_at: int | None = None
    probed: bool = True


@dataclass(frozen=True, slots=True)
class AccountRuntimeOccupancy:
    sessions: int = 0
    pending_requests: int = 0
    queued_requests: int = 0
    busy_sessions: int = 0
    codex_sessions: int = 0
    reconnect_requested_sessions: int = 0

    @property
    def occupied(self) -> bool:
        return self.sessions > 0 or self.pending_requests > 0 or self.queued_requests > 0 or self.busy_sessions > 0


def _overlay_latest_model_usage(
    base: dict[str, UsageHistory],
    latest: dict[str, AdditionalUsageHistory],
) -> dict[str, UsageHistory | AdditionalUsageHistory]:
    if not latest:
        return base
    merged = dict(base)
    merged.update(latest)
    return merged


class AccountsService:
    def __init__(
        self,
        repo: AccountsRepository,
        usage_repo: UsageRepository | None = None,
        additional_usage_repo: AdditionalUsageRepository | AdditionalUsageRepositoryPort | None = None,
    ) -> None:
        self._repo = repo
        self._usage_repo = usage_repo
        self._additional_usage_repo = additional_usage_repo
        self._usage_updater = UsageUpdater(usage_repo, repo, additional_usage_repo) if usage_repo else None
        self._encryptor = TokenEncryptor()
        self._auth_manager = AuthManager(repo)

    async def list_accounts(self) -> list[AccountSummary]:
        accounts = await self._repo.list_accounts()
        if not accounts:
            return []
        account_ids = [account.id for account in accounts]
        account_id_set = set(account_ids)
        primary_usage = await self._usage_repo.latest_by_account(window="primary") if self._usage_repo else {}
        secondary_usage = await self._usage_repo.latest_by_account(window="secondary") if self._usage_repo else {}
        request_usage_rows = await self._repo.list_request_usage_summary_by_account(account_ids)
        request_usage_by_account = {
            account_id: AccountRequestUsage(
                request_count=row.request_count,
                total_tokens=row.total_tokens,
                tokens_7d=row.tokens_7d,
                cached_input_tokens=row.cached_input_tokens,
                total_cost_usd=row.total_cost_usd,
                estimated_total_cost=row.estimated_total_cost,
                estimated_total_cost_currency=row.estimated_total_cost_currency,
            )
            for account_id, row in request_usage_rows.items()
        }
        additional_quotas_by_account: dict[str, list[AccountAdditionalQuota]] = {}
        additional_usage_repo = cast(AdditionalUsageRepository | None, self._additional_usage_repo)
        if additional_usage_repo:
            latest_quota_key = get_latest_model_quota_key()
            if latest_quota_key:
                latest_primary = await additional_usage_repo.latest_by_account(
                    latest_quota_key, "primary", account_ids=account_ids
                )
                latest_secondary = await additional_usage_repo.latest_by_account(
                    latest_quota_key, "secondary", account_ids=account_ids
                )
                primary_usage = _overlay_latest_model_usage(primary_usage, latest_primary)
                secondary_usage = _overlay_latest_model_usage(secondary_usage, latest_secondary)
            quota_keys = await additional_usage_repo.list_quota_keys(account_ids=account_ids)
            for quota_key in quota_keys:
                primary_entries = await additional_usage_repo.latest_by_account(quota_key, "primary")
                secondary_entries = await additional_usage_repo.latest_by_account(quota_key, "secondary")
                for account_id in (set(primary_entries) | set(secondary_entries)) & account_id_set:
                    primary_entry = primary_entries.get(account_id)
                    secondary_entry = secondary_entries.get(account_id)
                    reference_entry = primary_entry or secondary_entry
                    if reference_entry is None:
                        continue
                    additional_quotas_by_account.setdefault(account_id, []).append(
                        AccountAdditionalQuota(
                            quota_key=quota_key,
                            limit_name=reference_entry.limit_name,
                            metered_feature=reference_entry.metered_feature,
                            display_label=get_additional_display_label_for_quota_key(quota_key)
                            or reference_entry.limit_name,
                            primary_window=AccountAdditionalWindow(
                                used_percent=primary_entry.used_percent,
                                reset_at=primary_entry.reset_at,
                                window_minutes=primary_entry.window_minutes,
                            )
                            if primary_entry is not None
                            else None,
                            secondary_window=AccountAdditionalWindow(
                                used_percent=secondary_entry.used_percent,
                                reset_at=secondary_entry.reset_at,
                                window_minutes=secondary_entry.window_minutes,
                            )
                            if secondary_entry is not None
                            else None,
                        )
                    )
        for account_quota_list in additional_quotas_by_account.values():
            account_quota_list.sort(key=lambda quota: quota.display_label or quota.quota_key or quota.limit_name)

        return build_account_summaries(
            accounts=accounts,
            primary_usage=primary_usage,
            secondary_usage=secondary_usage,
            request_usage_by_account=request_usage_by_account,
            additional_quotas_by_account=additional_quotas_by_account,
            encryptor=self._encryptor,
        )

    async def get_account_quota_status(
        self,
        account_id: str,
        *,
        runtime_occupancy: Mapping[str, AccountRuntimeOccupancy] | None = None,
    ) -> AccountQuotaStatus | None:
        summaries = await self.list_accounts()
        for summary in summaries:
            if summary.account_id == account_id:
                return _build_account_quota_status(summary, runtime_occupancy)
        return None

    async def list_account_quota_statuses(
        self,
        *,
        runtime_occupancy: Mapping[str, AccountRuntimeOccupancy] | None = None,
    ) -> list[AccountQuotaStatus]:
        return [_build_account_quota_status(summary, runtime_occupancy) for summary in await self.list_accounts()]

    async def get_account_trends(self, account_id: str) -> AccountTrendsResponse | None:
        account = await self._repo.get_by_id(account_id)
        if not account or not self._usage_repo:
            return None
        now = utcnow()
        since = now - timedelta(days=_SPARKLINE_DAYS)
        since_epoch = naive_utc_to_epoch(since)
        bucket_count = (_SPARKLINE_DAYS * 24 * 3600) // _DETAIL_BUCKET_SECONDS
        buckets = await self._usage_repo.trends_by_bucket(
            since=since,
            bucket_seconds=_DETAIL_BUCKET_SECONDS,
            account_id=account_id,
        )
        primary_history, secondary_history = await asyncio.gather(
            self._usage_repo.history_since(account_id, "primary", since),
            self._usage_repo.history_since(account_id, "secondary", since),
        )
        trends = build_account_usage_trends(buckets, since_epoch, _DETAIL_BUCKET_SECONDS, bucket_count)
        timeline_bucket_count = _quota_timeline_bucket_count(
            since_epoch=since_epoch,
            until_epoch=naive_utc_to_epoch(now),
            bucket_seconds=_QUOTA_TIMELINE_BUCKET_SECONDS,
        )
        quota_timeline = build_account_quota_timeline(
            primary_history=primary_history,
            secondary_history=secondary_history,
            since_epoch=since_epoch,
            bucket_seconds=_QUOTA_TIMELINE_BUCKET_SECONDS,
            bucket_count=timeline_bucket_count,
            primary_capacity_credits=usage_core.capacity_for_plan(account.plan_type, "primary"),
        )
        trend = trends.get(account_id)
        return AccountTrendsResponse(
            account_id=account_id,
            primary=trend.primary if trend else [],
            secondary=trend.secondary if trend else [],
            quota_timeline=quota_timeline,
        )

    async def test_availability(self, target_id: str) -> AccountAvailabilityResponse | None:
        account = await self._repo.get_by_id(target_id)
        if account is None:
            return None
        outcome = await self._probe_account_availability(account)
        if (
            outcome.status != account.status
            or outcome.reason != account.deactivation_reason
            or outcome.reset_at != account.reset_at
        ):
            await self._repo.update_status(account.id, outcome.status, outcome.reason, outcome.reset_at)
            get_account_selection_cache().invalidate()
        return AccountAvailabilityResponse(
            status="completed",
            target_id=target_id,
            tested_count=1 if outcome.probed else 0,
            passed_count=1 if outcome.ok else 0,
            failed_count=1 if outcome.probed and not outcome.ok else 0,
            skipped_count=0 if outcome.probed else 1,
            active_count=1 if outcome.status == AccountStatus.ACTIVE else 0,
            total_count=1,
            failed_account_ids=[] if outcome.ok else [account.id],
        )

    async def get_rate_limit_reset_credits(self, account_id: str) -> AccountRateLimitResetCreditsResponse | None:
        account = await self._repo.get_by_id(account_id)
        if account is None:
            return None
        account = await self._prepare_reset_credit_account(account)
        credits = await self._fetch_rate_limit_reset_credits(account)
        return AccountRateLimitResetCreditsResponse(
            account_id=account.id,
            available_count=credits.available_count,
        )

    async def consume_rate_limit_reset_credit(
        self,
        account_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> AccountRateLimitResetConsumeResponse | None:
        account = await self._repo.get_by_id(account_id)
        if account is None:
            return None
        account = await self._prepare_reset_credit_account(account)
        result = await self._consume_rate_limit_reset_credit(account, idempotency_key or str(uuid4()))

        if result.code in {"reset", "already_redeemed"} and self._usage_updater is not None:
            try:
                refresh_account = await self._repo.get_by_id(account.id)
                await self._usage_updater.refresh_account_now(refresh_account or account)
            except Exception as exc:
                logger.warning(
                    "Reset credit consume could not refresh account usage account_id=%s error=%s",
                    account.id,
                    exc,
                )

        available_count: int | None = None
        if result.code == "no_credit":
            available_count = 0
        else:
            try:
                refreshed = await self.get_rate_limit_reset_credits(account.id)
                available_count = refreshed.available_count if refreshed is not None else None
            except AccountResetCreditError as exc:
                logger.info(
                    "Reset credit consume could not refresh available count account_id=%s error=%s",
                    account.id,
                    exc,
                )

        return AccountRateLimitResetConsumeResponse(
            account_id=account.id,
            outcome=result.code,
            available_count=available_count,
            windows_reset=result.windows_reset,
        )

    async def import_account(self, raw: bytes) -> AccountImportResponse:
        try:
            auth = parse_auth_json(raw)
        except (json.JSONDecodeError, ValidationError, UnicodeDecodeError, TypeError) as exc:
            raise InvalidAuthJsonError("Invalid auth.json payload") from exc
        claims = claims_from_auth(auth)

        email = claims.email or DEFAULT_EMAIL
        raw_account_id = claims.account_id
        account_id = generate_unique_account_id(raw_account_id, email)
        plan_type = coerce_account_plan_type(claims.plan_type, DEFAULT_PLAN)
        last_refresh = to_utc_naive(auth.last_refresh_at) if auth.last_refresh_at else utcnow()

        account = Account(
            id=account_id,
            chatgpt_account_id=raw_account_id,
            email=email,
            plan_type=plan_type,
            provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
            access_token_encrypted=self._encryptor.encrypt(auth.tokens.access_token),
            refresh_token_encrypted=self._encryptor.encrypt(auth.tokens.refresh_token),
            id_token_encrypted=self._encryptor.encrypt(auth.tokens.id_token),
            last_refresh=last_refresh,
            status=AccountStatus.ACTIVE,
            deactivation_reason=None,
        )

        saved = await self._repo.upsert_openai_reauth(account)
        if self._usage_repo and self._usage_updater:
            latest_usage = await self._usage_repo.latest_by_account(window="primary")
            await self._usage_updater.refresh_accounts([saved], latest_usage)
        get_account_selection_cache().invalidate()
        return AccountImportResponse(
            account_id=saved.id,
            email=saved.email,
            plan_type=saved.plan_type,
            status=saved.status,
        )

    async def create_api_provider(self, payload: ApiProviderCreateRequest) -> ApiProviderCreateResponse:
        name = payload.name.strip()
        api_key = payload.api_key.strip()
        if not name:
            raise InvalidApiProviderError("Provider name is required")
        if not api_key:
            raise InvalidApiProviderError("API key is required")

        try:
            probe = await probe_upstream_provider(base_url=payload.base_url, api_key=api_key)
        except UpstreamProbeError as exc:
            raise InvalidApiProviderError(str(exc)) from exc

        account = Account(
            id=f"provider_{uuid4().hex[:12]}",
            chatgpt_account_id=None,
            email=_provider_account_label(name, probe.base_url),
            plan_type="api_key_provider",
            provider_kind=ACCOUNT_PROVIDER_API_KEY,
            upstream_base_url=probe.base_url,
            upstream_wire_api=probe.wire_api,
            upstream_priority=payload.priority,
            supported_models_json=json.dumps(list(probe.supported_models), ensure_ascii=True)
            if probe.supported_models
            else None,
            access_token_encrypted=self._encryptor.encrypt(api_key),
            refresh_token_encrypted=b"",
            id_token_encrypted=b"",
            last_refresh=utcnow(),
            status=AccountStatus.ACTIVE,
            deactivation_reason=None,
        )
        saved = await self._repo.upsert(account, merge_by_email=False)
        get_account_selection_cache().invalidate()
        return ApiProviderCreateResponse(
            account_id=saved.id,
            email=saved.email,
            plan_type=saved.plan_type,
            status=saved.status,
            base_url=probe.base_url,
            wire_api=probe.wire_api,
            priority=saved.upstream_priority,
            supported_models=list(probe.supported_models),
        )

    async def reactivate_account(self, account_id: str) -> bool:
        result = await self._repo.update_status(account_id, AccountStatus.ACTIVE, None, None, blocked_at=None)
        if result:
            get_account_selection_cache().invalidate()
        return result

    async def pause_account(self, account_id: str) -> bool:
        result = await self._repo.update_status(account_id, AccountStatus.PAUSED, None, None, blocked_at=None)
        if result:
            get_account_selection_cache().invalidate()
        return result

    async def delete_account(self, account_id: str) -> bool:
        result = await self._repo.delete(account_id)
        if result:
            get_account_selection_cache().invalidate()
            get_api_key_cache().clear()
            poller = get_cache_invalidation_poller()
            if poller is not None:
                await poller.bump(NAMESPACE_API_KEY)
        return result

    async def merge_accounts(self, source_account_id: str, target_account_id: str) -> AccountMergeResponse:
        if source_account_id == target_account_id:
            raise AccountMergeValidationError("Source and target accounts must be different")
        source = await self._repo.get_by_id(source_account_id)
        if source is None:
            raise AccountMergeValidationError(f"Source account not found: {source_account_id}")
        target = await self._repo.get_by_id(target_account_id)
        if target is None:
            raise AccountMergeValidationError(f"Target account not found: {target_account_id}")

        result = await self._repo.merge_account_data(source_account_id, target_account_id)
        if result is None:
            raise AccountMergeValidationError("Account merge could not be completed")

        get_account_selection_cache().invalidate()
        get_api_key_cache().clear()
        poller = get_cache_invalidation_poller()
        if poller is not None:
            await poller.bump(NAMESPACE_API_KEY)

        return AccountMergeResponse(
            status="merged",
            source_account_id=result.source_account_id,
            target_account_id=result.target_account_id,
            usage_history_rows=result.usage_history_rows,
            additional_usage_history_rows=result.additional_usage_history_rows,
            request_log_rows=result.request_log_rows,
            sticky_session_rows=result.sticky_session_rows,
            http_bridge_session_rows=result.http_bridge_session_rows,
            api_key_assignment_rows=result.api_key_assignment_rows,
            duplicate_api_key_assignment_rows=result.duplicate_api_key_assignment_rows,
            account_group_rows=result.account_group_rows,
            duplicate_account_group_rows=result.duplicate_account_group_rows,
        )

    async def update_account(self, account_id: str, payload: AccountUpdateRequest) -> AccountSummary | None:
        update_kwargs = {
            "configured_priority": payload.configured_priority,
            "kyc_enabled": payload.kyc_enabled,
            "fast_service_tier_enabled": payload.fast_service_tier_enabled,
            "primary_drain_priority_enabled": payload.primary_drain_priority_enabled,
            "groups": _normalize_group_names(payload.groups) if payload.groups is not None else None,
        }
        if "subscription_renews_at" in payload.model_fields_set:
            update_kwargs["subscription_renews_at"] = (
                to_utc_naive(payload.subscription_renews_at)
                if payload.subscription_renews_at is not None
                else None
            )
        updated = await self._repo.update_routing_settings(account_id, **update_kwargs)
        if updated is None:
            return None
        get_account_selection_cache().invalidate()
        return build_account_summaries(
            accounts=[updated],
            primary_usage={},
            secondary_usage={},
            request_usage_by_account={},
            additional_quotas_by_account={},
            encryptor=self._encryptor,
        )[0]

    async def update_all_fast_service_tier(self, *, enabled: bool) -> AccountFastServiceTierBulkUpdateResponse:
        updated_count = await self._repo.update_all_fast_service_tier(enabled=enabled)
        get_account_selection_cache().invalidate()
        return AccountFastServiceTierBulkUpdateResponse(enabled=enabled, updated_count=updated_count)

    async def _prepare_reset_credit_account(self, account: Account) -> Account:
        if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
            raise AccountResetCreditError("Rate-limit reset credits are only available for OpenAI OAuth accounts")
        try:
            return await self._auth_manager.ensure_fresh(
                account,
                deactivate_on_permanent_error=False,
            )
        except RefreshError as exc:
            raise AccountResetCreditError(exc.message) from exc

    async def _fetch_rate_limit_reset_credits(self, account: Account) -> RateLimitResetCreditBankPayload:
        try:
            return await self._fetch_rate_limit_reset_credits_once(account)
        except UsageFetchError as exc:
            if exc.status_code != 401:
                raise AccountResetCreditError(exc.message) from exc
        try:
            refreshed = await self._auth_manager.ensure_fresh(
                account,
                force=True,
                deactivate_on_permanent_error=False,
            )
            return await self._fetch_rate_limit_reset_credits_once(refreshed)
        except RefreshError as exc:
            raise AccountResetCreditError(exc.message) from exc
        except UsageFetchError as exc:
            raise AccountResetCreditError(exc.message) from exc

    async def _fetch_rate_limit_reset_credits_once(self, account: Account) -> RateLimitResetCreditBankPayload:
        access_token = self._encryptor.decrypt(account.access_token_encrypted)
        return await fetch_rate_limit_reset_credits(
            access_token=access_token,
            account_id=account.chatgpt_account_id,
            account_label=account.email,
            base_url=account.upstream_base_url,
        )

    async def _consume_rate_limit_reset_credit(
        self,
        account: Account,
        idempotency_key: str,
    ) -> RateLimitResetConsumePayload:
        credits = await self._fetch_rate_limit_reset_credits(account)
        available_credit = credits.available_credits[0] if credits.available_credits else None
        if available_credit is None:
            return RateLimitResetConsumePayload(code="no_credit", windows_reset=0)
        try:
            return await self._consume_rate_limit_reset_credit_once(account, available_credit.id, idempotency_key)
        except UsageFetchError as exc:
            if exc.status_code != 401:
                raise AccountResetCreditError(exc.message) from exc
        try:
            refreshed = await self._auth_manager.ensure_fresh(
                account,
                force=True,
                deactivate_on_permanent_error=False,
            )
            refreshed_credits = await self._fetch_rate_limit_reset_credits(refreshed)
            refreshed_available_credit = (
                refreshed_credits.available_credits[0] if refreshed_credits.available_credits else None
            )
            if refreshed_available_credit is None:
                return RateLimitResetConsumePayload(code="no_credit", windows_reset=0)
            return await self._consume_rate_limit_reset_credit_once(
                refreshed,
                refreshed_available_credit.id,
                idempotency_key,
            )
        except RefreshError as exc:
            raise AccountResetCreditError(exc.message) from exc
        except UsageFetchError as exc:
            raise AccountResetCreditError(exc.message) from exc

    async def _consume_rate_limit_reset_credit_once(
        self,
        account: Account,
        credit_id: str,
        idempotency_key: str,
    ) -> RateLimitResetConsumePayload:
        access_token = self._encryptor.decrypt(account.access_token_encrypted)
        return await consume_upstream_rate_limit_reset_credit(
            access_token=access_token,
            account_id=account.chatgpt_account_id,
            credit_id=credit_id,
            idempotency_key=idempotency_key,
            account_label=account.email,
            base_url=account.upstream_base_url,
        )

    async def _probe_account_availability(self, account: Account) -> _AvailabilityOutcome:
        if account.status == AccountStatus.PAUSED:
            return _AvailabilityOutcome(
                ok=False,
                status=AccountStatus.PAUSED,
                reason=account.deactivation_reason,
                reset_at=account.reset_at,
                probed=False,
            )
        if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
            try:
                api_key = self._encryptor.decrypt(account.access_token_encrypted)
                probe = await probe_upstream_provider(base_url=account.upstream_base_url or "", api_key=api_key)
                if probe.supported_models:
                    account.supported_models_json = json.dumps(list(probe.supported_models), ensure_ascii=True)
                return _AvailabilityOutcome(ok=True, status=AccountStatus.ACTIVE)
            except Exception as exc:
                logger.info(
                    "Availability provider probe failed without deactivating account_id=%s error=%s",
                    account.id,
                    exc,
                )
                return _availability_probe_failure(account)
        try:
            refreshed = await self._auth_manager.ensure_fresh(
                account,
                force=True,
                deactivate_on_permanent_error=False,
            )
            await self._sync_plan_type_from_usage(refreshed)
            return _AvailabilityOutcome(ok=True, status=AccountStatus.ACTIVE)
        except RefreshError as exc:
            logger.info(
                "Availability refresh probe failed without deactivating account_id=%s code=%s permanent=%s message=%s",
                account.id,
                exc.code,
                exc.is_permanent,
                exc.message,
            )
            return _availability_probe_failure(account)
        except Exception as exc:
            logger.info(
                "Availability refresh probe errored without deactivating account_id=%s error=%s",
                account.id,
                exc,
            )
            return _availability_probe_failure(account)

    async def _sync_plan_type_from_usage(self, account: Account) -> None:
        try:
            access_token = self._encryptor.decrypt(account.access_token_encrypted)
            payload = await fetch_usage(
                access_token=access_token,
                account_id=account.chatgpt_account_id,
                account_label=account.email,
            )
        except UsageFetchError as exc:
            logger.info(
                "Availability probe could not sync plan from usage account_id=%s status=%s message=%s",
                account.id,
                exc.status_code,
                exc.message,
            )
            return
        except Exception as exc:
            logger.info(
                "Availability probe could not sync plan from usage account_id=%s error=%s",
                account.id,
                exc,
            )
            return

        next_plan_type = coerce_account_plan_type(payload.plan_type, account.plan_type or DEFAULT_PLAN)
        if next_plan_type == account.plan_type:
            return

        account.plan_type = next_plan_type
        await self._repo.update_tokens(
            account.id,
            access_token_encrypted=account.access_token_encrypted,
            refresh_token_encrypted=account.refresh_token_encrypted,
            id_token_encrypted=account.id_token_encrypted,
            last_refresh=account.last_refresh,
            plan_type=next_plan_type,
            email=account.email,
            chatgpt_account_id=account.chatgpt_account_id,
        )


def _provider_account_label(name: str, base_url: str) -> str:
    host = urlparse(base_url).netloc
    if not host:
        return name
    return f"{name} ({host})"


def _normalize_group_names(group_names: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for group_name in group_names:
        value = group_name.strip().lower()
        if not value:
            continue
        if len(value) > 128:
            raise ValueError("Group names must be at most 128 characters")
        if value in seen or is_reserved_account_group(value):
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _build_account_quota_status(
    summary: AccountSummary,
    runtime_occupancy: Mapping[str, AccountRuntimeOccupancy] | None,
) -> AccountQuotaStatus:
    usage = summary.usage
    occupancy = (
        runtime_occupancy.get(summary.account_id, AccountRuntimeOccupancy())
        if runtime_occupancy
        else AccountRuntimeOccupancy()
    )
    return AccountQuotaStatus(
        account_id=summary.account_id,
        email=summary.email,
        display_name=summary.display_name,
        status=summary.status,
        primary_window=AccountQuotaWindow(
            remaining_percent=usage.primary_remaining_percent if usage else None,
            reset_at=summary.reset_at_primary,
            window_minutes=summary.window_minutes_primary,
            capacity_credits=summary.capacity_credits_primary,
            remaining_credits=summary.remaining_credits_primary,
        ),
        secondary_window=AccountQuotaWindow(
            remaining_percent=usage.secondary_remaining_percent if usage else None,
            reset_at=summary.reset_at_secondary,
            window_minutes=summary.window_minutes_secondary,
            capacity_credits=summary.capacity_credits_secondary,
            remaining_credits=summary.remaining_credits_secondary,
        ),
        runtime=AccountRuntimeState(
            occupied=occupancy.occupied,
            sessions=occupancy.sessions,
            pending_requests=occupancy.pending_requests,
            queued_requests=occupancy.queued_requests,
            busy_sessions=occupancy.busy_sessions,
            codex_sessions=occupancy.codex_sessions,
            reconnect_requested_sessions=occupancy.reconnect_requested_sessions,
        ),
    )


def _quota_timeline_bucket_count(*, since_epoch: int, until_epoch: int, bucket_seconds: int) -> int:
    aligned_start = (since_epoch // bucket_seconds) * bucket_seconds
    if until_epoch <= aligned_start:
        return 1
    return max(1, math.ceil((until_epoch - aligned_start) / bucket_seconds))


def _availability_failure_reason(message: str) -> str:
    normalized = message.strip() or "Probe request failed"
    return f"Availability probe failed: {normalized}"


def _availability_probe_failure(account: Account) -> _AvailabilityOutcome:
    return _AvailabilityOutcome(
        ok=False,
        status=account.status,
        reason=account.deactivation_reason,
        reset_at=account.reset_at,
    )
