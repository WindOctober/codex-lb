from __future__ import annotations

import asyncio
import time
from typing import Protocol

from app.core import usage as usage_core
from app.core.usage.types import UsageWindowRow
from app.db.models import Account, AdditionalUsageHistory, UsageHistory
from app.modules.proxy.helpers import (
    _credits_headers,
    _credits_snapshot,
    _plan_type_for_accounts,
    _rate_limit_details,
    _rate_limit_headers,
    _select_accounts_for_limits,
    _summarize_window,
    _window_snapshot,
)
from app.modules.proxy.rate_limit_cache import get_rate_limit_headers_cache
from app.modules.proxy.repo_bundle import ProxyRepoFactory, ProxyRepositories
from app.modules.proxy.types import (
    AdditionalRateLimitData,
    RateLimitStatusDetailsData,
    RateLimitStatusPayloadData,
    RateLimitWindowSnapshotData,
)
from app.modules.usage.additional_quota_keys import get_additional_display_label_for_quota_key
from app.modules.usage.latest_model import get_latest_model_quota_key
from app.modules.usage.updater import UsageUpdater, background_usage_refresh_repo_context


class _RateLimitRuntimeService(Protocol):
    _repo_factory: ProxyRepoFactory

    async def _latest_usage_rows(
        self,
        repos: ProxyRepositories,
        account_map: dict[str, Account],
        window: str,
    ) -> list[UsageWindowRow]: ...

    async def _latest_usage_entries(
        self,
        repos: ProxyRepositories,
        account_map: dict[str, Account],
    ) -> list[UsageHistory]: ...

    async def _refresh_usage(self, repos: ProxyRepositories, accounts: list[Account]) -> None: ...

    async def _build_additional_rate_limits(
        self,
        repos: ProxyRepositories,
        account_map: dict[str, Account],
        now_epoch: int,
    ) -> list[AdditionalRateLimitData]: ...

    async def _compute_rate_limit_headers(self) -> dict[str, str]: ...


class _RateLimitRuntimeMixin:
    async def rate_limit_headers(self: _RateLimitRuntimeService) -> dict[str, str]:
        return await get_rate_limit_headers_cache().get(self._compute_rate_limit_headers)

    async def _compute_rate_limit_headers(self: _RateLimitRuntimeService) -> dict[str, str]:
        headers: dict[str, str] = {}
        async with self._repo_factory() as repos:
            accounts = await repos.accounts.list_accounts()
            selected_accounts = _select_accounts_for_limits(accounts)
            if not selected_accounts:
                return headers

            account_map = {account.id: account for account in selected_accounts}
            primary_rows_raw, secondary_rows_raw = await asyncio.gather(
                self._latest_usage_rows(repos, account_map, "primary"),
                self._latest_usage_rows(repos, account_map, "secondary"),
            )
            primary_rows, secondary_rows = usage_core.normalize_weekly_only_rows(
                primary_rows_raw,
                secondary_rows_raw,
            )

            primary_summary = _summarize_window(primary_rows, account_map, "primary")
            if primary_summary is not None:
                headers.update(_rate_limit_headers("primary", primary_summary))

            secondary_summary = _summarize_window(secondary_rows, account_map, "secondary")
            if secondary_summary is not None:
                headers.update(_rate_limit_headers("secondary", secondary_summary))

            headers.update(_credits_headers(await self._latest_usage_entries(repos, account_map)))
        return headers

    async def get_rate_limit_payload(self: _RateLimitRuntimeService) -> RateLimitStatusPayloadData:
        async with self._repo_factory() as repos:
            accounts = await repos.accounts.list_accounts()
            await self._refresh_usage(repos, accounts)
            selected_accounts = _select_accounts_for_limits(accounts)
            if not selected_accounts:
                return RateLimitStatusPayloadData(plan_type="guest")

            account_map = {account.id: account for account in selected_accounts}
            primary_rows_raw, secondary_rows_raw = await asyncio.gather(
                self._latest_usage_rows(repos, account_map, "primary"),
                self._latest_usage_rows(repos, account_map, "secondary"),
            )
            primary_rows, secondary_rows = usage_core.normalize_weekly_only_rows(
                primary_rows_raw,
                secondary_rows_raw,
            )

            primary_summary = _summarize_window(primary_rows, account_map, "primary")
            secondary_summary = _summarize_window(secondary_rows, account_map, "secondary")
            now_epoch = int(time.time())
            primary_window = _window_snapshot(primary_summary, primary_rows, "primary", now_epoch)
            secondary_window = _window_snapshot(secondary_summary, secondary_rows, "secondary", now_epoch)

            return RateLimitStatusPayloadData(
                plan_type=_plan_type_for_accounts(selected_accounts),
                rate_limit=_rate_limit_details(primary_window, secondary_window),
                credits=_credits_snapshot(await self._latest_usage_entries(repos, account_map)),
                additional_rate_limits=await self._build_additional_rate_limits(repos, account_map, now_epoch),
            )

    async def _refresh_usage(
        self: _RateLimitRuntimeService,
        repos: ProxyRepositories,
        accounts: list[Account],
    ) -> None:
        latest_usage = await repos.usage.latest_by_account(window="primary")
        updater = UsageUpdater(
            repos.usage,
            repos.accounts,
            repos.additional_usage,
            repo_factory=background_usage_refresh_repo_context,
        )
        await updater.refresh_accounts(accounts, latest_usage)

    async def _latest_usage_rows(
        self: _RateLimitRuntimeService,
        repos: ProxyRepositories,
        account_map: dict[str, Account],
        window: str,
    ) -> list[UsageWindowRow]:
        if not account_map:
            return []
        latest = await repos.usage.latest_by_account(window=window)
        rows_by_account = {
            entry.account_id: _usage_window_row_from_entry(entry)
            for entry in latest.values()
            if entry.account_id in account_map
        }
        latest_quota_key = get_latest_model_quota_key()
        if latest_quota_key:
            latest_additional = await repos.additional_usage.latest_by_account(
                latest_quota_key,
                window,
                account_ids=list(account_map),
            )
            for entry in latest_additional.values():
                rows_by_account[entry.account_id] = _usage_window_row_from_entry(entry)
        return list(rows_by_account.values())

    async def _latest_usage_entries(
        self: _RateLimitRuntimeService,
        repos: ProxyRepositories,
        account_map: dict[str, Account],
    ) -> list[UsageHistory]:
        if not account_map:
            return []
        latest = await repos.usage.latest_by_account()
        return [entry for entry in latest.values() if entry.account_id in account_map]

    async def _build_additional_rate_limits(
        self: _RateLimitRuntimeService,
        repos: ProxyRepositories,
        account_map: dict[str, Account],
        now_epoch: int,
    ) -> list[AdditionalRateLimitData]:
        if not account_map:
            return []

        limit_names = await repos.additional_usage.list_limit_names(account_ids=list(account_map))
        additional_limits: list[AdditionalRateLimitData] = []
        for limit_name in limit_names:
            latest_entries = await repos.additional_usage.latest_by_account(
                limit_name=limit_name,
                window="primary",
            )
            latest_secondary = await repos.additional_usage.latest_by_account(
                limit_name=limit_name,
                window="secondary",
            )
            filtered_entries = {
                account_id: entry for account_id, entry in latest_entries.items() if account_id in account_map
            }
            filtered_secondary = {
                account_id: entry for account_id, entry in latest_secondary.items() if account_id in account_map
            }
            if not filtered_entries and not filtered_secondary:
                continue

            first_entry = (
                next(iter(filtered_entries.values())) if filtered_entries else next(iter(filtered_secondary.values()))
            )
            window_snapshot: RateLimitWindowSnapshotData | None = None
            avg_used_percent: float | None = None
            if filtered_entries:
                used_percents = [
                    entry.used_percent for entry in filtered_entries.values() if entry.used_percent is not None
                ]
                if used_percents:
                    avg_used_percent = sum(used_percents) / len(used_percents)
                    window_minutes_values = [
                        entry.window_minutes for entry in filtered_entries.values() if entry.window_minutes
                    ]
                    reset_at_values = [
                        entry.reset_at for entry in filtered_entries.values() if entry.reset_at is not None
                    ]
                    if window_minutes_values and reset_at_values:
                        reset_at = int(min(reset_at_values))
                        window_snapshot = RateLimitWindowSnapshotData(
                            used_percent=int(max(0.0, min(100.0, avg_used_percent))),
                            limit_window_seconds=int(max(window_minutes_values) * 60),
                            reset_after_seconds=max(0, reset_at - now_epoch),
                            reset_at=reset_at,
                        )
                    else:
                        window_snapshot = RateLimitWindowSnapshotData(
                            used_percent=int(max(0.0, min(100.0, avg_used_percent))),
                        )

            secondary_window_snapshot: RateLimitWindowSnapshotData | None = None
            if filtered_secondary:
                secondary_used_percents = [
                    entry.used_percent for entry in filtered_secondary.values() if entry.used_percent is not None
                ]
                if secondary_used_percents:
                    secondary_average = sum(secondary_used_percents) / len(secondary_used_percents)
                    secondary_window_values = [
                        entry.window_minutes for entry in filtered_secondary.values() if entry.window_minutes
                    ]
                    secondary_reset_values = [
                        entry.reset_at for entry in filtered_secondary.values() if entry.reset_at is not None
                    ]
                    if secondary_window_values and secondary_reset_values:
                        secondary_reset_at = int(min(secondary_reset_values))
                        secondary_window_snapshot = RateLimitWindowSnapshotData(
                            used_percent=int(max(0.0, min(100.0, secondary_average))),
                            limit_window_seconds=int(max(secondary_window_values) * 60),
                            reset_after_seconds=max(0, secondary_reset_at - now_epoch),
                            reset_at=secondary_reset_at,
                        )
                    else:
                        secondary_window_snapshot = RateLimitWindowSnapshotData(
                            used_percent=int(max(0.0, min(100.0, secondary_average))),
                        )

            rate_limit_details: RateLimitStatusDetailsData | None = None
            if avg_used_percent is not None or secondary_window_snapshot is not None:
                all_account_ids = set(filtered_entries) | set(filtered_secondary)
                any_available = any(
                    (filtered_entries[account_id].used_percent if account_id in filtered_entries else 0.0) < 100.0
                    and (filtered_secondary[account_id].used_percent if account_id in filtered_secondary else 0.0)
                    < 100.0
                    for account_id in all_account_ids
                )
                rate_limit_details = RateLimitStatusDetailsData(
                    allowed=any_available,
                    limit_reached=not any_available,
                    primary_window=window_snapshot,
                    secondary_window=secondary_window_snapshot,
                )

            additional_limits.append(
                AdditionalRateLimitData(
                    quota_key=limit_name,
                    limit_name=first_entry.limit_name,
                    display_label=get_additional_display_label_for_quota_key(limit_name) or first_entry.limit_name,
                    metered_feature=first_entry.metered_feature,
                    rate_limit=rate_limit_details,
                )
            )
        return additional_limits


def _usage_window_row_from_entry(entry: UsageHistory | AdditionalUsageHistory) -> UsageWindowRow:
    return UsageWindowRow(
        account_id=entry.account_id,
        used_percent=entry.used_percent,
        reset_at=entry.reset_at,
        window_minutes=entry.window_minutes,
        recorded_at=entry.recorded_at,
    )
