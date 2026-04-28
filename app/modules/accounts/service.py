from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import ValidationError

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
from app.core.crypto import TokenEncryptor
from app.core.plan_types import coerce_account_plan_type
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
from app.modules.accounts.mappers import build_account_summaries, build_account_usage_trends
from app.modules.accounts.repository import AccountsRepository
from app.modules.accounts.schemas import (
    AccountAdditionalQuota,
    AccountAdditionalWindow,
    AccountAvailabilityResponse,
    AccountImportResponse,
    AccountRequestUsage,
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


class InvalidAuthJsonError(Exception):
    pass


class InvalidApiProviderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _AvailabilityOutcome:
    ok: bool
    status: AccountStatus
    reason: str | None = None
    reset_at: int | None = None
    probed: bool = True


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
        trends = build_account_usage_trends(buckets, since_epoch, _DETAIL_BUCKET_SECONDS, bucket_count)
        trend = trends.get(account_id)
        return AccountTrendsResponse(
            account_id=account_id,
            primary=trend.primary if trend else [],
            secondary=trend.secondary if trend else [],
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

        saved = await self._repo.upsert(account)
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

    async def update_account(self, account_id: str, payload: AccountUpdateRequest) -> AccountSummary | None:
        updated = await self._repo.update_priority(account_id, payload.configured_priority)
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
                return _AvailabilityOutcome(
                    ok=False,
                    status=AccountStatus.DEACTIVATED,
                    reason=_availability_failure_reason(str(exc) or "Provider probe failed"),
                )
        try:
            await self._auth_manager.ensure_fresh(account, force=True)
            return _AvailabilityOutcome(ok=True, status=AccountStatus.ACTIVE)
        except RefreshError as exc:
            return _AvailabilityOutcome(
                ok=False,
                status=AccountStatus.DEACTIVATED,
                reason=_availability_failure_reason(exc.message or exc.code or "Token refresh failed"),
            )
        except Exception as exc:
            return _AvailabilityOutcome(
                ok=False,
                status=AccountStatus.DEACTIVATED,
                reason=_availability_failure_reason(str(exc) or "Token refresh failed"),
            )


def _provider_account_label(name: str, base_url: str) -> str:
    host = urlparse(base_url).netloc
    if not host:
        return name
    return f"{name} ({host})"


def _availability_failure_reason(message: str) -> str:
    normalized = message.strip() or "Probe request failed"
    return f"Availability probe failed: {normalized}"
