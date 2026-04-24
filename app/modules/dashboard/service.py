from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app.core import usage as usage_core
from app.core.crypto import TokenEncryptor
from app.core.usage.types import UsageWindowRow
from app.core.utils.time import utcnow
from app.db.models import ACCOUNT_PROVIDER_OPENAI_OAUTH, Account, UsageHistory
from app.modules.accounts.mappers import build_account_summaries
from app.modules.accounts.schemas import AccountRequestUsage, AccountSummary, AccountUsage
from app.modules.dashboard.builders import (
    build_dashboard_overview_summary,
    build_overview_timeframe,
    resolve_overview_timeframe,
)
from app.modules.dashboard.repository import DashboardRepository
from app.modules.dashboard.schemas import (
    DashboardOverviewResponse,
    DashboardOverviewTimeframeKey,
    DashboardUsageWindows,
    DepletionResponse,
)
from app.modules.usage.builders import (
    align_bucket_window_start,
    build_activity_summaries,
    build_trends_from_buckets,
    build_usage_window_response,
)
from app.modules.usage.depletion_service import (
    compute_aggregate_depletion,
    compute_depletion_for_account,
)


class DashboardService:
    def __init__(self, repo: DashboardRepository) -> None:
        self._repo = repo
        self._encryptor = TokenEncryptor()

    async def get_overview(
        self,
        timeframe_key: DashboardOverviewTimeframeKey = "7d",
    ) -> DashboardOverviewResponse:
        now = utcnow()
        overview_timeframe = resolve_overview_timeframe(timeframe_key)
        accounts = await self._repo.list_accounts()
        request_usage_rows = await self._repo.list_request_usage_summary_by_account([account.id for account in accounts])
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
        primary_usage = await self._repo.latest_usage_by_account("primary")
        secondary_usage = await self._repo.latest_usage_by_account("secondary")

        account_summaries = build_account_summaries(
            accounts=accounts,
            primary_usage=primary_usage,
            secondary_usage=secondary_usage,
            request_usage_by_account=request_usage_by_account,
            encryptor=self._encryptor,
            include_auth=False,
        )

        primary_rows_raw = _rows_from_latest(primary_usage)
        secondary_rows_raw = _rows_from_latest(secondary_usage)
        primary_rows, secondary_rows = usage_core.normalize_weekly_only_rows(
            primary_rows_raw,
            secondary_rows_raw,
        )

        bucket_since = now - timedelta(minutes=overview_timeframe.window_minutes)
        bucket_query_since = align_bucket_window_start(
            bucket_since,
            overview_timeframe.bucket_seconds,
        )
        bucket_rows = await self._repo.aggregate_logs_by_bucket(
            bucket_query_since,
            overview_timeframe.bucket_seconds,
        )
        trends, _, _ = build_trends_from_buckets(
            bucket_rows,
            bucket_since,
            bucket_seconds=overview_timeframe.bucket_seconds,
            bucket_count=overview_timeframe.bucket_count,
        )
        activity_aggregate = await self._repo.aggregate_activity_since(bucket_since)
        top_error = await self._repo.top_error_since(bucket_since)
        activity_metrics, activity_cost = build_activity_summaries(
            activity_aggregate,
            top_error=top_error,
        )

        summary = build_dashboard_overview_summary(
            accounts=accounts,
            primary_rows=primary_rows,
            secondary_rows=secondary_rows,
            activity_metrics=activity_metrics,
            activity_cost=activity_cost,
        )

        secondary_minutes = usage_core.resolve_window_minutes("secondary", secondary_rows)
        primary_window_minutes = usage_core.resolve_window_minutes("primary", primary_rows)

        windows = DashboardUsageWindows(
            primary=build_usage_window_response(
                window_key="primary",
                window_minutes=primary_window_minutes,
                usage_rows=primary_rows,
                accounts=accounts,
            ),
            secondary=build_usage_window_response(
                window_key="secondary",
                window_minutes=secondary_minutes,
                usage_rows=secondary_rows,
                accounts=accounts,
            ),
        )

        # Compute depletion separately for primary-window and secondary-window
        # accounts so the aggregate is not skewed by mixing different window
        # durations.  The response includes a "window" field that tells the
        # frontend which donut to render the safe-line marker on.
        normalized_primary_ids = {row.account_id for row in primary_rows}
        all_account_ids = set(primary_usage.keys()) | set(secondary_usage.keys())

        # Batch fetch: collect account IDs and determine the widest lookback
        # per window so we can issue at most 2 bulk queries instead of O(N).
        pri_fetch_ids: list[str] = []
        sec_fetch_ids: list[str] = []
        pri_since = now  # will be narrowed to the earliest needed
        sec_since = now
        # Per-account cutoffs for in-memory filtering after bulk fetch
        pri_cutoffs: dict[str, datetime] = {}
        sec_cutoffs: dict[str, datetime] = {}
        weekly_only_ids: set[str] = set()
        weekly_only_history_sources: dict[str, str] = {}

        for account_id in all_account_ids:
            if account_id in normalized_primary_ids:
                usage_entry = primary_usage[account_id]
                acct_window = usage_entry.window_minutes if usage_entry.window_minutes else 300
                acct_since = now - timedelta(minutes=acct_window)
                pri_fetch_ids.append(account_id)
                pri_cutoffs[account_id] = acct_since
                if acct_since < pri_since:
                    pri_since = acct_since
                if account_id in secondary_usage:
                    sec_entry = secondary_usage[account_id]
                    sec_window = sec_entry.window_minutes if sec_entry.window_minutes else 10080
                    s_since = now - timedelta(minutes=sec_window)
                    sec_fetch_ids.append(account_id)
                    sec_cutoffs[account_id] = s_since
                    if s_since < sec_since:
                        sec_since = s_since
            elif account_id in primary_usage:
                weekly_only_ids.add(account_id)
                primary_entry = primary_usage[account_id]
                sec_entry = secondary_usage.get(account_id)
                use_primary_stream = _should_use_weekly_primary_history(primary_entry, sec_entry)
                weekly_only_history_sources[account_id] = "primary" if use_primary_stream else "secondary"
                current_entry = primary_entry if use_primary_stream else sec_entry
                acct_window = current_entry.window_minutes if current_entry and current_entry.window_minutes else 10080
                acct_since = now - timedelta(minutes=acct_window)
                if use_primary_stream:
                    pri_fetch_ids.append(account_id)
                    pri_cutoffs[account_id] = acct_since
                    if acct_since < pri_since:
                        pri_since = acct_since
                else:
                    sec_fetch_ids.append(account_id)
                    sec_cutoffs[account_id] = acct_since
                    if acct_since < sec_since:
                        sec_since = acct_since
            else:
                sec_entry = secondary_usage[account_id]
                acct_window = sec_entry.window_minutes if sec_entry.window_minutes else 10080
                acct_since = now - timedelta(minutes=acct_window)
                sec_fetch_ids.append(account_id)
                sec_cutoffs[account_id] = acct_since
                if acct_since < sec_since:
                    sec_since = acct_since

        # Issue at most 2 bulk queries
        all_pri_rows = (
            await self._repo.bulk_usage_history_since(pri_fetch_ids, "primary", pri_since) if pri_fetch_ids else {}
        )
        all_sec_rows = (
            await self._repo.bulk_usage_history_since(sec_fetch_ids, "secondary", sec_since) if sec_fetch_ids else {}
        )

        # Filter in-memory to each account's actual cutoff
        primary_history: dict[str, list[UsageHistory]] = {}
        secondary_history: dict[str, list[UsageHistory]] = {}

        for account_id in all_account_ids:
            if account_id in normalized_primary_ids:
                cutoff = pri_cutoffs[account_id]
                rows = [r for r in all_pri_rows.get(account_id, []) if r.recorded_at >= cutoff]
                if rows:
                    primary_history[account_id] = rows
                if account_id in sec_cutoffs:
                    s_cutoff = sec_cutoffs[account_id]
                    s_rows = [r for r in all_sec_rows.get(account_id, []) if r.recorded_at >= s_cutoff]
                    if s_rows:
                        secondary_history[account_id] = s_rows
            elif account_id in weekly_only_ids:
                source = weekly_only_history_sources[account_id]
                if source == "primary":
                    cutoff = pri_cutoffs[account_id]
                    rows = [r for r in all_pri_rows.get(account_id, []) if r.recorded_at >= cutoff]
                else:
                    cutoff = sec_cutoffs[account_id]
                    rows = [r for r in all_sec_rows.get(account_id, []) if r.recorded_at >= cutoff]
                if rows:
                    secondary_history[account_id] = rows
            else:
                cutoff = sec_cutoffs[account_id]
                rows = [r for r in all_sec_rows.get(account_id, []) if r.recorded_at >= cutoff]
                if rows:
                    secondary_history[account_id] = rows

        pri_depletion, sec_depletion = _build_depletion_by_window(primary_history, secondary_history, now)

        additional_ts = await self._repo.latest_additional_recorded_at()
        return DashboardOverviewResponse(
            last_sync_at=_latest_recorded_at(primary_usage, secondary_usage, additional_ts),
            timeframe=build_overview_timeframe(overview_timeframe),
            accounts=account_summaries,
            grouped_accounts=_build_grouped_dashboard_accounts(accounts, account_summaries),
            summary=summary,
            windows=windows,
            trends=trends,
            depletion_primary=pri_depletion,
            depletion_secondary=sec_depletion,
        )


def _build_depletion_by_window(
    primary_history: dict[str, list[UsageHistory]],
    secondary_history: dict[str, list[UsageHistory]],
    now,
) -> tuple[DepletionResponse | None, DepletionResponse | None]:
    """Compute depletion independently per window."""

    def _aggregate(history: dict[str, list[UsageHistory]], window: str) -> DepletionResponse | None:
        metrics = []
        for account_id, rows in history.items():
            m = compute_depletion_for_account(
                account_id=account_id,
                limit_name="standard",
                window=window,
                history=rows,
                now=now,
            )
            metrics.append(m)
        agg = compute_aggregate_depletion(metrics)
        if agg is None:
            return None
        return DepletionResponse(
            risk=agg.risk,
            risk_level=agg.risk_level,
            burn_rate=agg.burn_rate,
            safe_usage_percent=agg.safe_usage_percent,
            projected_exhaustion_at=agg.projected_exhaustion_at,
            seconds_until_exhaustion=agg.seconds_until_exhaustion,
        )

    return _aggregate(primary_history, "primary"), _aggregate(secondary_history, "secondary")


def _rows_from_latest(latest: dict[str, UsageHistory]) -> list[UsageWindowRow]:
    return [
        UsageWindowRow(
            account_id=entry.account_id,
            used_percent=entry.used_percent,
            reset_at=entry.reset_at,
            window_minutes=entry.window_minutes,
            recorded_at=entry.recorded_at,
        )
        for entry in latest.values()
    ]


def _should_use_weekly_primary_history(
    primary_entry: UsageHistory,
    secondary_entry: UsageHistory | None,
) -> bool:
    return usage_core.should_use_weekly_primary(
        _usage_history_to_window_row(primary_entry),
        _usage_history_to_window_row(secondary_entry) if secondary_entry is not None else None,
    )


def _usage_history_to_window_row(entry: UsageHistory) -> UsageWindowRow:
    return UsageWindowRow(
        account_id=entry.account_id,
        used_percent=entry.used_percent,
        reset_at=entry.reset_at,
        window_minutes=entry.window_minutes,
        recorded_at=entry.recorded_at,
    )


def _latest_recorded_at(
    primary_usage: dict[str, UsageHistory],
    secondary_usage: dict[str, UsageHistory],
    additional_ts: datetime | None = None,
):
    timestamps = [
        entry.recorded_at
        for entry in list(primary_usage.values()) + list(secondary_usage.values())
        if entry.recorded_at is not None
    ]
    if additional_ts is not None:
        timestamps.append(additional_ts)
    return max(timestamps) if timestamps else None


def _build_grouped_dashboard_accounts(
    accounts: list[Account],
    account_summaries: list[AccountSummary],
) -> list[AccountSummary]:
    summary_by_id = {summary.account_id: summary for summary in account_summaries}
    grouped_by_domain: dict[str, list[AccountSummary]] = defaultdict(list)

    for account in accounts:
        summary = summary_by_id.get(account.id)
        if summary is None:
            continue
        domain = _groupable_openai_domain(account)
        if domain is None:
            continue
        grouped_by_domain[domain].append(summary)

    emitted_domains: set[str] = set()
    grouped: list[AccountSummary] = []
    for account in accounts:
        summary = summary_by_id.get(account.id)
        if summary is None:
            continue
        domain = _groupable_openai_domain(account)
        if domain is None:
            grouped.append(summary)
            continue
        domain_members = grouped_by_domain[domain]
        if len(domain_members) <= 1:
            grouped.append(summary)
            continue
        if domain in emitted_domains:
            continue
        emitted_domains.add(domain)
        grouped.append(_merge_domain_group(domain, domain_members))

    return grouped


def _groupable_openai_domain(account: Account) -> str | None:
    if account.provider_kind != ACCOUNT_PROVIDER_OPENAI_OAUTH:
        return None
    email = (account.email or "").strip().lower()
    if "@" not in email:
        return None
    local_part, _, domain = email.rpartition("@")
    if not local_part or not domain:
        return None
    return domain


def _merge_domain_group(domain: str, members: list[AccountSummary]) -> AccountSummary:
    member_count = len(members)
    active_count = sum(1 for member in members if member.status == "active")
    primary_remaining = _weighted_remaining_percent(
        members,
        capacity_attr="capacity_credits_primary",
        remaining_attr="remaining_credits_primary",
    )
    secondary_remaining = _weighted_remaining_percent(
        members,
        capacity_attr="capacity_credits_secondary",
        remaining_attr="remaining_credits_secondary",
    )
    plan_type = Counter(member.plan_type for member in members if member.plan_type).most_common(1)
    routing_priority = min((member.routing_priority for member in members), default=0)
    routing_tier = _aggregate_routing_tier(member.routing_tier for member in members)
    return AccountSummary(
        account_id=f"domain:{domain}",
        email=f"{active_count} available / {member_count} total",
        display_name=domain,
        plan_type=plan_type[0][0] if plan_type else "openai_oauth",
        provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
        routing_tier=routing_tier,
        routing_priority=routing_priority,
        configured_priority=0,
        status=_aggregate_group_status(member.status for member in members),
        usage=AccountUsage(
            primary_remaining_percent=primary_remaining,
            secondary_remaining_percent=secondary_remaining,
        ),
        capacity_credits_primary=sum(member.capacity_credits_primary or 0.0 for member in members),
        remaining_credits_primary=sum(member.remaining_credits_primary or 0.0 for member in members),
        capacity_credits_secondary=sum(member.capacity_credits_secondary or 0.0 for member in members),
        remaining_credits_secondary=sum(member.remaining_credits_secondary or 0.0 for member in members),
        request_usage=_sum_request_usage(member.request_usage for member in members),
    )


def _aggregate_group_status(statuses: list[str] | tuple[str, ...] | object) -> str:
    values = list(statuses) if not isinstance(statuses, list) else statuses
    if any(status == "active" for status in values):
        return "active"
    if any(status == "rate_limited" for status in values):
        return "rate_limited"
    if any(status == "quota_exceeded" for status in values):
        return "quota_exceeded"
    if any(status == "paused" for status in values):
        return "paused"
    if any(status == "deactivated" for status in values):
        return "deactivated"
    return "unknown"


def _aggregate_routing_tier(values: object) -> str:
    tiers = [value for value in values if isinstance(value, str)]
    if "openai_free" in tiers:
        return "openai_free"
    if "openai_paid" in tiers:
        return "openai_paid"
    return "openai_paid"


def _weighted_remaining_percent(
    members: list[AccountSummary],
    *,
    capacity_attr: str,
    remaining_attr: str,
) -> float | None:
    total_capacity = 0.0
    total_remaining = 0.0
    for member in members:
        capacity_value = getattr(member, capacity_attr)
        remaining_value = getattr(member, remaining_attr)
        if capacity_value is None or remaining_value is None:
            continue
        total_capacity += float(capacity_value)
        total_remaining += float(remaining_value)
    if total_capacity <= 0:
        return None
    return max(0.0, min(100.0, (total_remaining / total_capacity) * 100.0))


def _sum_request_usage(values) -> AccountRequestUsage | None:
    rows = [value for value in values if value is not None]
    if not rows:
        return None
    currencies = {row.estimated_total_cost_currency for row in rows if row.estimated_total_cost_currency}
    display_currency = next(iter(currencies)) if len(currencies) == 1 else None
    display_amount = (
        round(sum(row.estimated_total_cost or 0.0 for row in rows), 6)
        if display_currency is not None
        else None
    )
    return AccountRequestUsage(
        request_count=sum(row.request_count for row in rows),
        total_tokens=sum(row.total_tokens for row in rows),
        tokens_7d=sum(row.tokens_7d for row in rows),
        cached_input_tokens=sum(row.cached_input_tokens for row in rows),
        total_cost_usd=round(sum(row.total_cost_usd for row in rows), 6),
        estimated_total_cost=display_amount,
        estimated_total_cost_currency=display_currency,
    )
