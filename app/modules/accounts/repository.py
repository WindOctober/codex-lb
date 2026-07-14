from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.core.usage.pricing import (
    UsageTokens,
    _effective_rates,
    calculate_cost_from_usage,
    get_pricing_for_model,
)
from app.core.utils.time import utcnow
from app.db.models import (
    ACCOUNT_PROVIDER_API_KEY,
    ACCOUNT_PROVIDER_OPENAI_OAUTH,
    Account,
    AccountGroupMembership,
    AccountStatus,
    AdditionalUsageHistory,
    ApiKeyAccountAssignment,
    Base,
    DashboardSettings,
    HttpBridgeSessionRecord,
    RequestLog,
    StickySession,
    UsageHistory,
)

_SETTINGS_ROW_ID = 1
_DUPLICATE_ACCOUNT_SUFFIX = "__copy"
_UNSET = object()
_DUCKCODING_CNY_INPUT_PER_1M = 1.25
_DUCKCODING_CNY_CACHED_PER_1M = _DUCKCODING_CNY_INPUT_PER_1M / 10.0


@dataclass(frozen=True, slots=True)
class AccountRequestUsageSummary:
    request_count: int
    total_tokens: int
    tokens_7d: int
    cached_input_tokens: int
    total_cost_usd: float
    estimated_total_cost: float | None = None
    estimated_total_cost_currency: str | None = None


@dataclass(frozen=True, slots=True)
class AccountMergeResult:
    source_account_id: str
    target_account_id: str
    usage_history_rows: int
    additional_usage_history_rows: int
    request_log_rows: int
    sticky_session_rows: int
    http_bridge_session_rows: int
    api_key_assignment_rows: int
    duplicate_api_key_assignment_rows: int
    account_group_rows: int
    duplicate_account_group_rows: int


class AccountIdentityConflictError(Exception):
    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(
            f"Cannot automatically merge account for email '{email}' because matching accounts have "
            "different identities. Merge them manually if they represent the same user."
        )


class AccountReauthTargetError(Exception):
    pass


class AccountsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, account_id: str) -> Account | None:
        result = await self._session.execute(
            select(Account).options(selectinload(Account.group_memberships)).where(Account.id == account_id)
        )
        return result.scalar_one_or_none()

    async def list_accounts(self) -> list[Account]:
        result = await self._session.execute(
            select(Account).options(selectinload(Account.group_memberships)).order_by(Account.email)
        )
        return list(result.scalars().all())

    async def list_openai_accounts(self) -> list[Account]:
        result = await self._session.execute(
            select(Account).where(Account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH).order_by(Account.email)
        )
        return list(result.scalars().all())

    async def has_active_api_key_accounts(self) -> bool:
        result = await self._session.execute(
            select(Account.id)
            .where(Account.provider_kind == ACCOUNT_PROVIDER_API_KEY)
            .where(Account.status.notin_((AccountStatus.PAUSED, AccountStatus.DEACTIVATED)))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_request_usage_summary_by_account(
        self,
        account_ids: list[str] | None = None,
    ) -> dict[str, AccountRequestUsageSummary]:
        since_7d = utcnow() - timedelta(days=7)
        accounts_stmt = select(Account)
        if account_ids:
            accounts_stmt = accounts_stmt.where(Account.id.in_(account_ids))
        accounts_result = await self._session.execute(accounts_stmt)
        accounts_by_id = {account.id: account for account in accounts_result.scalars().all()}
        output_tokens_expr = func.coalesce(RequestLog.output_tokens, RequestLog.reasoning_tokens, 0)
        missing_cost_expr = RequestLog.cost_usd.is_(None)
        stmt = select(
            RequestLog.account_id,
            RequestLog.model,
            RequestLog.service_tier,
            func.count(RequestLog.id).label("request_count"),
            func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(output_tokens_expr), 0).label("output_tokens"),
            func.coalesce(func.sum(RequestLog.cached_input_tokens), 0).label("cached_input_tokens"),
            func.coalesce(func.sum(RequestLog.cache_write_tokens), 0).label("cache_write_tokens"),
            func.count(RequestLog.cost_usd).label("persisted_cost_count"),
            func.coalesce(func.sum(RequestLog.cost_usd), 0.0).label("persisted_cost_usd"),
            func.coalesce(
                func.sum(case((missing_cost_expr, func.coalesce(RequestLog.input_tokens, 0)), else_=0)),
                0,
            ).label("legacy_input_tokens"),
            func.coalesce(
                func.sum(case((missing_cost_expr, func.coalesce(output_tokens_expr, 0)), else_=0)),
                0,
            ).label("legacy_output_tokens"),
            func.coalesce(
                func.sum(case((missing_cost_expr, func.coalesce(RequestLog.cached_input_tokens, 0)), else_=0)),
                0,
            ).label("legacy_cached_input_tokens"),
            func.coalesce(
                func.sum(case((missing_cost_expr, func.coalesce(RequestLog.cache_write_tokens, 0)), else_=0)),
                0,
            ).label("legacy_cache_write_tokens"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            RequestLog.requested_at >= since_7d,
                            func.coalesce(RequestLog.input_tokens, 0) + func.coalesce(output_tokens_expr, 0),
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("tokens_7d"),
        ).group_by(RequestLog.account_id, RequestLog.model, RequestLog.service_tier)
        if account_ids:
            stmt = stmt.where(RequestLog.account_id.in_(account_ids))

        result = await self._session.execute(stmt)
        rollup: dict[str, dict[str, float | int | str | None]] = {}
        for (
            account_id,
            model,
            service_tier,
            request_count,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            cache_write_tokens,
            persisted_cost_count,
            persisted_cost_usd,
            legacy_input_tokens,
            legacy_output_tokens,
            legacy_cached_input_tokens,
            legacy_cache_write_tokens,
            tokens_7d,
        ) in result.all():
            if not account_id:
                continue
            input_sum = int(input_tokens or 0)
            output_sum = int(output_tokens or 0)
            cached_sum = int(cached_input_tokens or 0)
            cached_sum = max(0, min(cached_sum, input_sum))
            cache_write_sum = max(0, min(int(cache_write_tokens or 0), input_sum - cached_sum))
            tokens_sum = input_sum + output_sum

            entry = rollup.setdefault(
                account_id,
                {
                    "request_count": 0,
                    "total_tokens": 0,
                    "tokens_7d": 0,
                    "cached_input_tokens": 0,
                    "total_cost_usd": 0.0,
                    "estimated_total_cost": 0.0,
                    "estimated_total_cost_currency": None,
                },
            )
            entry["request_count"] = int(entry["request_count"] or 0) + int(request_count or 0)
            entry["total_tokens"] = int(entry["total_tokens"] or 0) + tokens_sum
            entry["tokens_7d"] = int(entry["tokens_7d"] or 0) + int(tokens_7d or 0)
            entry["cached_input_tokens"] = int(entry["cached_input_tokens"] or 0) + cached_sum

            usage = UsageTokens(
                input_tokens=float(input_sum),
                output_tokens=float(output_sum),
                cached_input_tokens=float(cached_sum),
                cache_write_tokens=float(cache_write_sum),
            )
            legacy_input_sum = int(legacy_input_tokens or 0)
            legacy_output_sum = int(legacy_output_tokens or 0)
            legacy_cached_sum = max(
                0,
                min(int(legacy_cached_input_tokens or 0), legacy_input_sum),
            )
            legacy_cache_write_sum = max(
                0,
                min(int(legacy_cache_write_tokens or 0), legacy_input_sum - legacy_cached_sum),
            )
            legacy_usage = UsageTokens(
                input_tokens=float(legacy_input_sum),
                output_tokens=float(legacy_output_sum),
                cached_input_tokens=float(legacy_cached_sum),
                cache_write_tokens=float(legacy_cache_write_sum),
            )
            group_cost_usd = float(persisted_cost_usd or 0.0)
            resolved = get_pricing_for_model(model or "", None, None)
            legacy_cost_usd: float | None = None
            if resolved is not None and (legacy_input_sum > 0 or legacy_output_sum > 0):
                _, price = resolved
                legacy_cost_usd = calculate_cost_from_usage(
                    legacy_usage,
                    price,
                    service_tier=service_tier,
                )
                if legacy_cost_usd is not None:
                    group_cost_usd += legacy_cost_usd
            entry["total_cost_usd"] = float(entry["total_cost_usd"] or 0.0) + group_cost_usd

            account = accounts_by_id.get(account_id)
            if account is None:
                continue
            if _is_duckcoding_account(account):
                if resolved is None:
                    continue
                _, price = resolved
                estimated_cost, currency = _calculate_display_cost(
                    account=account,
                    usage=usage,
                    price=price,
                    service_tier=service_tier,
                    fallback_usd=None,
                )
            elif int(persisted_cost_count or 0) > 0 or legacy_cost_usd is not None:
                estimated_cost, currency = group_cost_usd, "USD"
            else:
                continue
            if estimated_cost is None:
                continue
            if entry["estimated_total_cost_currency"] is None:
                entry["estimated_total_cost_currency"] = currency
            if entry["estimated_total_cost_currency"] == currency:
                entry["estimated_total_cost"] = float(entry["estimated_total_cost"] or 0.0) + estimated_cost

        return {
            account_id: AccountRequestUsageSummary(
                request_count=int(values["request_count"] or 0),
                total_tokens=int(values["total_tokens"] or 0),
                tokens_7d=int(values["tokens_7d"] or 0),
                cached_input_tokens=int(values["cached_input_tokens"] or 0),
                total_cost_usd=round(float(values["total_cost_usd"] or 0.0), 6),
                estimated_total_cost=round(float(values["estimated_total_cost"] or 0.0), 6)
                if values["estimated_total_cost_currency"] is not None
                else None,
                estimated_total_cost_currency=(
                    str(values["estimated_total_cost_currency"])
                    if values["estimated_total_cost_currency"] is not None
                    else None
                ),
            )
            for account_id, values in rollup.items()
        }

    async def exists_active_chatgpt_account_id(self, chatgpt_account_id: str) -> bool:
        result = await self._session.execute(
            select(Account.id)
            .where(Account.chatgpt_account_id == chatgpt_account_id)
            .where(Account.status.notin_((AccountStatus.DEACTIVATED, AccountStatus.PAUSED)))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def upsert(self, account: Account, *, merge_by_email: bool | None = None) -> Account:
        dialect_name = self._dialect_name()
        sqlite_lock_acquired = False
        if merge_by_email is None:
            if dialect_name == "sqlite":
                await self._acquire_sqlite_merge_lock()
                sqlite_lock_acquired = True
            merge_by_email = await self._merge_by_email_enabled()

        if merge_by_email:
            if dialect_name == "sqlite" and not sqlite_lock_acquired:
                await self._acquire_sqlite_merge_lock()
            elif dialect_name == "postgresql":
                await self._acquire_postgresql_merge_lock(account.email)
        else:
            if dialect_name == "sqlite" and not sqlite_lock_acquired:
                await self._acquire_sqlite_merge_lock()
            elif dialect_name == "postgresql":
                await self._acquire_postgresql_identity_lock(account.id)

        existing = await self._session.get(Account, account.id)
        if existing:
            if merge_by_email:
                _apply_account_updates(existing, account)
                await self._session.commit()
                await self._session.refresh(existing)
                return existing
            account.id = await self._next_available_account_id(account.id)

        if merge_by_email:
            existing_by_email = await self._single_account_by_email(account.email)
            if existing_by_email:
                _apply_account_updates(existing_by_email, account)
                await self._session.commit()
                await self._session.refresh(existing_by_email)
                return existing_by_email

        self._session.add(account)
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def upsert_openai_reauth(self, account: Account) -> Account:
        if account.provider_kind != ACCOUNT_PROVIDER_OPENAI_OAUTH:
            raise ValueError("upsert_openai_reauth only supports OpenAI OAuth accounts")

        dialect_name = self._dialect_name()
        if dialect_name == "sqlite":
            await self._acquire_sqlite_merge_lock()
        elif dialect_name == "postgresql":
            await self._acquire_postgresql_merge_lock(_normalize_email(account.email))

        same_email = await self._openai_accounts_by_normalized_email(account.email)
        same_identity = [
            existing for existing in same_email if existing.chatgpt_account_id == account.chatgpt_account_id
        ]
        conflicting_identity = [
            existing for existing in same_email if existing.chatgpt_account_id != account.chatgpt_account_id
        ]
        if conflicting_identity:
            raise AccountIdentityConflictError(account.email)

        if not same_identity:
            existing_by_id = await self._session.get(Account, account.id)
            if existing_by_id is not None:
                raise AccountIdentityConflictError(account.email)
            self._session.add(account)
            await self._session.commit()
            await self._session.refresh(account)
            return account

        target = _choose_openai_reauth_target(same_identity, incoming_account_id=account.id)
        for source in same_identity:
            if source.id == target.id:
                continue
            _merge_local_account_metadata(target, source)
            await self._merge_account_data_unlocked(source.id, target.id)

        _apply_openai_reauth_updates(target, account)
        await self._session.commit()
        await self._session.refresh(target)
        return target

    async def update_openai_reauth_target(self, target_account_id: str, account: Account) -> Account:
        if account.provider_kind != ACCOUNT_PROVIDER_OPENAI_OAUTH:
            raise ValueError("update_openai_reauth_target only supports OpenAI OAuth accounts")

        dialect_name = self._dialect_name()
        if dialect_name == "sqlite":
            await self._acquire_sqlite_merge_lock()
        elif dialect_name == "postgresql":
            for account_id in sorted({target_account_id, account.id}):
                await self._acquire_postgresql_identity_lock(account_id)

        target = await self._session.get(Account, target_account_id)
        if target is None:
            raise AccountReauthTargetError(f"Account not found: {target_account_id}")
        if target.provider_kind != ACCOUNT_PROVIDER_OPENAI_OAUTH:
            raise AccountReauthTargetError("Only OpenAI OAuth accounts can be re-authenticated")

        existing_identity = await self._openai_account_by_chatgpt_account_id(account.chatgpt_account_id)
        if existing_identity is not None and existing_identity.id != target.id:
            raise AccountReauthTargetError(
                "Incoming OpenAI identity already belongs to another local account. "
                "Merge accounts manually before re-authenticating this target."
            )

        _apply_openai_reauth_updates(target, account)
        await self._session.commit()
        await self._session.refresh(target)
        return target

    async def update_status(
        self,
        account_id: str,
        status: AccountStatus,
        deactivation_reason: str | None = None,
        reset_at: int | None = None,
        blocked_at: int | None | object = _UNSET,
    ) -> bool:
        values: dict[str, object | None] = {
            "status": status,
            "deactivation_reason": deactivation_reason,
            "reset_at": reset_at,
        }
        if blocked_at is not _UNSET:
            values["blocked_at"] = blocked_at
        result = await self._session.execute(
            update(Account).where(Account.id == account_id).values(**values).returning(Account.id)
        )
        await self._session.commit()
        return result.scalar_one_or_none() is not None

    async def update_status_if_current(
        self,
        account_id: str,
        status: AccountStatus,
        deactivation_reason: str | None = None,
        reset_at: int | None = None,
        blocked_at: int | None | object = _UNSET,
        *,
        expected_status: AccountStatus,
        expected_deactivation_reason: str | None = None,
        expected_reset_at: int | None = None,
        expected_blocked_at: int | None | object = _UNSET,
    ) -> bool:
        values: dict[str, object | None] = {
            "status": status,
            "deactivation_reason": deactivation_reason,
            "reset_at": reset_at,
        }
        if blocked_at is not _UNSET:
            values["blocked_at"] = blocked_at
        stmt = (
            update(Account)
            .where(Account.id == account_id)
            .where(Account.status == expected_status)
            .values(**values)
            .returning(Account.id)
        )
        if expected_deactivation_reason is None:
            stmt = stmt.where(Account.deactivation_reason.is_(None))
        else:
            stmt = stmt.where(Account.deactivation_reason == expected_deactivation_reason)
        if expected_reset_at is None:
            stmt = stmt.where(Account.reset_at.is_(None))
        else:
            stmt = stmt.where(Account.reset_at == expected_reset_at)
        if expected_blocked_at is not _UNSET:
            if expected_blocked_at is None:
                stmt = stmt.where(Account.blocked_at.is_(None))
            else:
                stmt = stmt.where(Account.blocked_at == expected_blocked_at)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.scalar_one_or_none() is not None

    async def update_routing_settings(
        self,
        account_id: str,
        *,
        configured_priority: int,
        kyc_enabled: bool | None = None,
        fast_service_tier_enabled: bool | None = None,
        primary_drain_priority_enabled: bool | None = None,
        subscription_renews_at: datetime | None | object = _UNSET,
        groups: list[str] | None = None,
    ) -> Account | None:
        values: dict[str, object] = {"upstream_priority": configured_priority}
        if kyc_enabled is not None:
            values["kyc_enabled"] = kyc_enabled
        if fast_service_tier_enabled is not None:
            values["fast_service_tier_enabled"] = fast_service_tier_enabled
        if primary_drain_priority_enabled is not None:
            values["primary_drain_priority_enabled"] = primary_drain_priority_enabled
        if subscription_renews_at is not _UNSET:
            values["subscription_renews_at"] = subscription_renews_at
        result = await self._session.execute(
            update(Account).where(Account.id == account_id).values(**values).returning(Account.id)
        )
        updated_id = result.scalar_one_or_none()
        if updated_id is not None and groups is not None:
            await self._session.execute(
                delete(AccountGroupMembership).where(AccountGroupMembership.account_id == account_id)
            )
            for group_name in groups:
                self._session.add(AccountGroupMembership(account_id=account_id, group_name=group_name))
        await self._session.commit()
        if updated_id is None:
            return None
        return await self.get_by_id(updated_id)

    async def update_priority(self, account_id: str, configured_priority: int) -> Account | None:
        return await self.update_routing_settings(account_id, configured_priority=configured_priority)

    async def update_all_fast_service_tier(self, *, enabled: bool) -> int:
        result = await self._session.execute(
            update(Account).values(fast_service_tier_enabled=enabled).returning(Account.id)
        )
        updated_ids = result.scalars().all()
        await self._session.commit()
        return len(updated_ids)

    async def clear_primary_drain_priority(self) -> int:
        result = await self._session.execute(
            update(Account)
            .where(Account.primary_drain_priority_enabled.is_(True))
            .values(primary_drain_priority_enabled=False)
            .returning(Account.id)
        )
        updated_ids = result.scalars().all()
        await self._session.commit()
        return len(updated_ids)

    async def merge_account_data(self, source_account_id: str, target_account_id: str) -> AccountMergeResult | None:
        if source_account_id == target_account_id:
            return None

        dialect_name = self._dialect_name()
        if dialect_name == "sqlite":
            await self._acquire_sqlite_merge_lock()
        elif dialect_name == "postgresql":
            for account_id in sorted((source_account_id, target_account_id)):
                await self._acquire_postgresql_identity_lock(account_id)

        result = await self._merge_account_data_unlocked(source_account_id, target_account_id)
        if result is None:
            await self._session.rollback()
            return None
        await self._session.commit()

        return result

    async def _merge_account_data_unlocked(
        self,
        source_account_id: str,
        target_account_id: str,
    ) -> AccountMergeResult | None:
        source = await self._session.get(Account, source_account_id)
        target = await self._session.get(Account, target_account_id)
        if source is None or target is None:
            return None
        usage_history_rows = await self._count_usage_history_rows(source_account_id)
        additional_usage_history_rows = await self._count_additional_usage_history_rows(source_account_id)
        request_log_rows = await self._count_request_log_rows(source_account_id)
        sticky_session_rows = await self._count_sticky_session_rows(source_account_id)
        http_bridge_session_rows = await self._count_http_bridge_session_rows(source_account_id)
        api_key_assignment_rows = await self._count_api_key_assignment_rows(source_account_id)
        account_group_rows = await self._count_account_group_rows(source_account_id)

        duplicate_api_key_assignment_rows = await self._delete_duplicate_api_key_assignments(
            source_account_id,
            target_account_id,
        )
        duplicate_account_group_rows = await self._delete_duplicate_account_groups(
            source_account_id,
            target_account_id,
        )

        await self._session.execute(
            update(UsageHistory)
            .where(UsageHistory.account_id == source_account_id)
            .values(account_id=target_account_id)
        )
        await self._session.execute(
            update(AdditionalUsageHistory)
            .where(AdditionalUsageHistory.account_id == source_account_id)
            .values(account_id=target_account_id)
        )
        await self._session.execute(
            update(RequestLog).where(RequestLog.account_id == source_account_id).values(account_id=target_account_id)
        )
        await self._session.execute(
            update(StickySession)
            .where(StickySession.account_id == source_account_id)
            .values(account_id=target_account_id)
        )
        await self._session.execute(
            update(HttpBridgeSessionRecord)
            .where(HttpBridgeSessionRecord.account_id == source_account_id)
            .values(account_id=target_account_id)
        )
        await self._session.execute(
            update(ApiKeyAccountAssignment)
            .where(ApiKeyAccountAssignment.account_id == source_account_id)
            .values(account_id=target_account_id)
        )
        await self._session.execute(
            update(AccountGroupMembership)
            .where(AccountGroupMembership.account_id == source_account_id)
            .values(account_id=target_account_id)
        )
        await self._session.execute(delete(Account).where(Account.id == source_account_id))

        return AccountMergeResult(
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            usage_history_rows=usage_history_rows,
            additional_usage_history_rows=additional_usage_history_rows,
            request_log_rows=request_log_rows,
            sticky_session_rows=sticky_session_rows,
            http_bridge_session_rows=http_bridge_session_rows,
            api_key_assignment_rows=api_key_assignment_rows,
            duplicate_api_key_assignment_rows=duplicate_api_key_assignment_rows,
            account_group_rows=account_group_rows,
            duplicate_account_group_rows=duplicate_account_group_rows,
        )

    async def delete(self, account_id: str) -> bool:
        await self._session.execute(delete(UsageHistory).where(UsageHistory.account_id == account_id))
        await self._session.execute(delete(RequestLog).where(RequestLog.account_id == account_id))
        await self._session.execute(delete(StickySession).where(StickySession.account_id == account_id))
        result = await self._session.execute(delete(Account).where(Account.id == account_id).returning(Account.id))
        await self._session.commit()
        return result.scalar_one_or_none() is not None

    async def update_tokens(
        self,
        account_id: str,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes,
        id_token_encrypted: bytes,
        last_refresh: datetime,
        plan_type: str | None = None,
        email: str | None = None,
        chatgpt_account_id: str | None = None,
    ) -> bool:
        values: dict[str, bytes | datetime | str] = {
            "access_token_encrypted": access_token_encrypted,
            "refresh_token_encrypted": refresh_token_encrypted,
            "id_token_encrypted": id_token_encrypted,
            "last_refresh": last_refresh,
        }
        if plan_type is not None:
            values["plan_type"] = plan_type
        if email is not None:
            values["email"] = email
        if chatgpt_account_id is not None:
            values["chatgpt_account_id"] = chatgpt_account_id
        result = await self._session.execute(
            update(Account).where(Account.id == account_id).values(**values).returning(Account.id)
        )
        await self._session.commit()
        return result.scalar_one_or_none() is not None

    async def _merge_by_email_enabled(self) -> bool:
        settings = await self._session.get(DashboardSettings, _SETTINGS_ROW_ID)
        if settings is None:
            return True
        return not settings.import_without_overwrite

    async def _next_available_account_id(self, base_id: str) -> str:
        candidate = base_id
        sequence = 2
        while await self._session.get(Account, candidate) is not None:
            candidate = f"{base_id}{_DUPLICATE_ACCOUNT_SUFFIX}{sequence}"
            sequence += 1
        return candidate

    async def _single_account_by_email(self, email: str) -> Account | None:
        result = await self._session.execute(
            select(Account).where(Account.email == email).order_by(Account.created_at.asc(), Account.id.asc()).limit(2)
        )
        matches = list(result.scalars().all())
        if not matches:
            return None
        if len(matches) > 1:
            raise AccountIdentityConflictError(email)
        return matches[0]

    async def _openai_accounts_by_normalized_email(self, email: str) -> list[Account]:
        result = await self._session.execute(
            select(Account)
            .options(selectinload(Account.group_memberships))
            .where(Account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH)
            .where(func.lower(Account.email) == _normalize_email(email))
            .order_by(Account.created_at.asc(), Account.id.asc())
        )
        return list(result.scalars().all())

    async def _openai_account_by_chatgpt_account_id(self, chatgpt_account_id: str | None) -> Account | None:
        if not chatgpt_account_id:
            return None
        result = await self._session.execute(
            select(Account)
            .where(Account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH)
            .where(Account.chatgpt_account_id == chatgpt_account_id)
            .order_by(Account.created_at.asc(), Account.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    def _dialect_name(self) -> str:
        return self._session.get_bind().dialect.name

    async def _acquire_sqlite_merge_lock(self) -> None:
        try:
            await self._session.execute(text("BEGIN IMMEDIATE"))
        except OperationalError as exc:
            message = str(exc).lower()
            if "within a transaction" not in message:
                raise
            # A no-op write escalates the current deferred transaction to a write
            # transaction, serializing concurrent writers.
            await self._session.execute(text("UPDATE accounts SET id = id WHERE 1 = 0"))

    async def _acquire_postgresql_merge_lock(self, email: str) -> None:
        lock_key = _advisory_lock_key("merge-email", email)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def _acquire_postgresql_identity_lock(self, account_id: str) -> None:
        lock_key = _advisory_lock_key("account-id", account_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def _count_usage_history_rows(self, account_id: str) -> int:
        return await self._count_rows(UsageHistory.account_id == account_id, UsageHistory)

    async def _count_additional_usage_history_rows(self, account_id: str) -> int:
        return await self._count_rows(AdditionalUsageHistory.account_id == account_id, AdditionalUsageHistory)

    async def _count_request_log_rows(self, account_id: str) -> int:
        return await self._count_rows(RequestLog.account_id == account_id, RequestLog)

    async def _count_sticky_session_rows(self, account_id: str) -> int:
        return await self._count_rows(StickySession.account_id == account_id, StickySession)

    async def _count_http_bridge_session_rows(self, account_id: str) -> int:
        return await self._count_rows(HttpBridgeSessionRecord.account_id == account_id, HttpBridgeSessionRecord)

    async def _count_api_key_assignment_rows(self, account_id: str) -> int:
        return await self._count_rows(ApiKeyAccountAssignment.account_id == account_id, ApiKeyAccountAssignment)

    async def _count_account_group_rows(self, account_id: str) -> int:
        return await self._count_rows(AccountGroupMembership.account_id == account_id, AccountGroupMembership)

    async def _delete_duplicate_api_key_assignments(self, source_account_id: str, target_account_id: str) -> int:
        target_api_key_ids = select(ApiKeyAccountAssignment.api_key_id).where(
            ApiKeyAccountAssignment.account_id == target_account_id
        )
        result = await self._session.execute(
            delete(ApiKeyAccountAssignment)
            .where(ApiKeyAccountAssignment.account_id == source_account_id)
            .where(ApiKeyAccountAssignment.api_key_id.in_(target_api_key_ids))
        )
        return _rowcount(result.rowcount)

    async def _delete_duplicate_account_groups(self, source_account_id: str, target_account_id: str) -> int:
        target_group_names = select(AccountGroupMembership.group_name).where(
            AccountGroupMembership.account_id == target_account_id
        )
        result = await self._session.execute(
            delete(AccountGroupMembership)
            .where(AccountGroupMembership.account_id == source_account_id)
            .where(AccountGroupMembership.group_name.in_(target_group_names))
        )
        return _rowcount(result.rowcount)

    async def _count_rows(self, criterion: ColumnElement[bool], model: type[Base]) -> int:
        result = await self._session.execute(select(func.count()).select_from(model).where(criterion))
        return int(result.scalar_one() or 0)


def _calculate_display_cost(
    *,
    account: Account,
    usage: UsageTokens,
    price,
    service_tier: str | None,
    fallback_usd: float | None,
) -> tuple[float | None, str | None]:
    if _is_duckcoding_account(account):
        input_rate, _, output_rate = _effective_rates(
            usage,
            price,
            service_tier=service_tier,
        )
        duck_output_rate = (
            _DUCKCODING_CNY_INPUT_PER_1M * (output_rate / input_rate)
            if input_rate > 0
            else _DUCKCODING_CNY_INPUT_PER_1M
        )
        billable_input = max(0.0, usage.input_tokens - usage.cached_input_tokens)
        cost_cny = (
            (billable_input / 1_000_000.0) * _DUCKCODING_CNY_INPUT_PER_1M
            + (usage.cache_write_tokens / 1_000_000.0)
            * _DUCKCODING_CNY_INPUT_PER_1M
            * max(0.0, price.cache_write_multiplier - 1.0)
            + (usage.cached_input_tokens / 1_000_000.0) * _DUCKCODING_CNY_CACHED_PER_1M
            + (usage.output_tokens / 1_000_000.0) * duck_output_rate
        )
        return cost_cny, "CNY"
    if fallback_usd is None:
        return None, None
    return fallback_usd, "USD"


def _is_duckcoding_account(account: Account) -> bool:
    if account.provider_kind != ACCOUNT_PROVIDER_API_KEY:
        return False
    email = (account.email or "").strip().lower()
    base_url = (account.upstream_base_url or "").strip().lower()
    return "duckcoding" in email or "duckcoding.com" in base_url


def _apply_account_updates(target: Account, source: Account) -> None:
    target.chatgpt_account_id = source.chatgpt_account_id
    target.email = source.email
    target.plan_type = source.plan_type
    if source.provider_kind is not None:
        target.provider_kind = source.provider_kind
    if source.upstream_base_url is not None:
        target.upstream_base_url = source.upstream_base_url
    if source.upstream_wire_api is not None:
        target.upstream_wire_api = source.upstream_wire_api
    if source.upstream_priority is not None:
        target.upstream_priority = source.upstream_priority
    target.supported_models_json = (
        source.supported_models_json if source.supported_models_json is not None else target.supported_models_json
    )
    target.kyc_enabled = bool(getattr(source, "kyc_enabled", False))
    target.fast_service_tier_enabled = bool(getattr(source, "fast_service_tier_enabled", False))
    if getattr(source, "subscription_renews_at", None) is not None:
        target.subscription_renews_at = source.subscription_renews_at
    target.access_token_encrypted = source.access_token_encrypted
    target.refresh_token_encrypted = source.refresh_token_encrypted
    target.id_token_encrypted = source.id_token_encrypted
    target.last_refresh = source.last_refresh
    target.status = source.status
    target.deactivation_reason = source.deactivation_reason
    target.reset_at = source.reset_at
    target.blocked_at = source.blocked_at


def _apply_openai_reauth_updates(target: Account, source: Account) -> None:
    target.chatgpt_account_id = source.chatgpt_account_id
    target.email = source.email
    target.plan_type = source.plan_type
    target.provider_kind = ACCOUNT_PROVIDER_OPENAI_OAUTH
    target.access_token_encrypted = source.access_token_encrypted
    target.refresh_token_encrypted = source.refresh_token_encrypted
    target.id_token_encrypted = source.id_token_encrypted
    target.last_refresh = source.last_refresh
    target.status = source.status
    target.deactivation_reason = source.deactivation_reason
    target.reset_at = source.reset_at
    target.blocked_at = source.blocked_at


def _choose_openai_reauth_target(accounts: list[Account], *, incoming_account_id: str) -> Account:
    exact_matches = [account for account in accounts if account.id == incoming_account_id]
    if exact_matches:
        return exact_matches[0]

    active_accounts = [account for account in accounts if account.status == AccountStatus.ACTIVE]
    if active_accounts:
        return active_accounts[0]

    non_copy_accounts = [account for account in accounts if _DUPLICATE_ACCOUNT_SUFFIX not in account.id]
    if non_copy_accounts:
        return non_copy_accounts[0]

    return accounts[0]


def _merge_local_account_metadata(target: Account, source: Account) -> None:
    target.kyc_enabled = bool(getattr(target, "kyc_enabled", False) or getattr(source, "kyc_enabled", False))
    target.fast_service_tier_enabled = bool(
        getattr(target, "fast_service_tier_enabled", False) or getattr(source, "fast_service_tier_enabled", False)
    )
    if target.upstream_priority == 100 and source.upstream_priority != 100:
        target.upstream_priority = source.upstream_priority
    if target.supported_models_json is None and source.supported_models_json is not None:
        target.supported_models_json = source.supported_models_json
    if target.subscription_renews_at is None and source.subscription_renews_at is not None:
        target.subscription_renews_at = source.subscription_renews_at


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _advisory_lock_key(scope: str, value: str) -> int:
    digest = hashlib.sha256(f"{scope}:{value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _rowcount(value: int | None) -> int:
    if value is None or value < 0:
        return 0
    return int(value)
