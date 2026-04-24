from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.usage.pricing import (
    UsageTokens,
    _effective_rates,
    calculate_cost_from_usage,
    get_pricing_for_model,
)
from app.db.models import (
    ACCOUNT_PROVIDER_API_KEY,
    ACCOUNT_PROVIDER_OPENAI_OAUTH,
    Account,
    AccountStatus,
    DashboardSettings,
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


class AccountIdentityConflictError(Exception):
    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(
            f"Cannot overwrite account for email '{email}' because multiple matching accounts exist. "
            "Remove duplicates or enable import without overwrite."
        )


class AccountsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, account_id: str) -> Account | None:
        return await self._session.get(Account, account_id)

    async def list_accounts(self) -> list[Account]:
        result = await self._session.execute(select(Account).order_by(Account.email))
        return list(result.scalars().all())

    async def list_openai_accounts(self) -> list[Account]:
        result = await self._session.execute(
            select(Account)
            .where(Account.provider_kind == ACCOUNT_PROVIDER_OPENAI_OAUTH)
            .order_by(Account.email)
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
        since_7d = datetime.utcnow() - timedelta(days=7)
        accounts_stmt = select(Account)
        if account_ids:
            accounts_stmt = accounts_stmt.where(Account.id.in_(account_ids))
        accounts_result = await self._session.execute(accounts_stmt)
        accounts_by_id = {account.id: account for account in accounts_result.scalars().all()}
        output_tokens_expr = func.coalesce(RequestLog.output_tokens, RequestLog.reasoning_tokens, 0)
        stmt = select(
            RequestLog.account_id,
            RequestLog.model,
            RequestLog.service_tier,
            func.count(RequestLog.id).label("request_count"),
            func.coalesce(func.sum(RequestLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(output_tokens_expr), 0).label("output_tokens"),
            func.coalesce(func.sum(RequestLog.cached_input_tokens), 0).label("cached_input_tokens"),
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
            tokens_7d,
        ) in result.all():
            if not account_id:
                continue
            input_sum = int(input_tokens or 0)
            output_sum = int(output_tokens or 0)
            cached_sum = int(cached_input_tokens or 0)
            cached_sum = max(0, min(cached_sum, input_sum))
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
            )
            resolved = get_pricing_for_model(model or "", None, None)
            if resolved is None:
                continue
            _, price = resolved
            cost_usd = calculate_cost_from_usage(
                usage,
                price,
                service_tier=service_tier,
            )
            if cost_usd is not None:
                entry["total_cost_usd"] = float(entry["total_cost_usd"] or 0.0) + cost_usd

            account = accounts_by_id.get(account_id)
            if account is None:
                continue
            estimated_cost, currency = _calculate_display_cost(
                account=account,
                usage=usage,
                price=price,
                service_tier=service_tier,
                fallback_usd=cost_usd,
            )
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

    async def update_priority(self, account_id: str, configured_priority: int) -> Account | None:
        result = await self._session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(upstream_priority=configured_priority)
            .returning(Account.id)
        )
        updated_id = result.scalar_one_or_none()
        await self._session.commit()
        if updated_id is None:
            return None
        return await self.get_by_id(updated_id)

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
    target.access_token_encrypted = source.access_token_encrypted
    target.refresh_token_encrypted = source.refresh_token_encrypted
    target.id_token_encrypted = source.id_token_encrypted
    target.last_refresh = source.last_refresh
    target.status = source.status
    target.deactivation_reason = source.deactivation_reason
    target.reset_at = source.reset_at
    target.blocked_at = source.blocked_at


def _advisory_lock_key(scope: str, value: str) -> int:
    digest = hashlib.sha256(f"{scope}:{value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
