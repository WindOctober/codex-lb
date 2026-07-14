from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.core import usage as usage_core
from app.core.account_groups import account_builtin_group_names
from app.core.account_priorities import account_configured_priority, account_routing_priority, account_routing_tier
from app.core.auth import DEFAULT_PLAN, extract_id_token_claims
from app.core.crypto import TokenEncryptor
from app.core.plan_types import coerce_account_plan_type
from app.core.usage.types import UsageTrendBucket, UsageWindowRow
from app.core.utils.time import from_epoch_seconds
from app.db.models import ACCOUNT_PROVIDER_API_KEY, Account, AdditionalUsageHistory, UsageHistory
from app.modules.accounts.schemas import (
    AccountAdditionalQuota,
    AccountAuthStatus,
    AccountQuotaTimelineBucket,
    AccountRequestUsage,
    AccountSummary,
    AccountTokenStatus,
    AccountUsage,
    AccountUsageTrend,
    UsageTrendPoint,
)

PRIMARY_RESET_USED_THRESHOLD_PCT = 1.0
MIN_STANDALONE_PRIMARY_BUCKET_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class _PrimaryUsageBucket:
    start_epoch: float
    end_epoch: float
    used_percent: float


def build_account_summaries(
    *,
    accounts: list[Account],
    primary_usage: dict[str, UsageHistory | AdditionalUsageHistory],
    secondary_usage: dict[str, UsageHistory | AdditionalUsageHistory],
    request_usage_by_account: dict[str, AccountRequestUsage] | None = None,
    additional_quotas_by_account: dict[str, list[AccountAdditionalQuota]] | None = None,
    encryptor: TokenEncryptor,
    include_auth: bool = True,
) -> list[AccountSummary]:
    return [
        _account_to_summary(
            account,
            primary_usage.get(account.id),
            secondary_usage.get(account.id),
            request_usage_by_account.get(account.id) if request_usage_by_account else None,
            additional_quotas_by_account.get(account.id) if additional_quotas_by_account else None,
            encryptor,
            include_auth=include_auth,
        )
        for account in accounts
    ]


def _account_to_summary(
    account: Account,
    primary_usage: UsageHistory | AdditionalUsageHistory | None,
    secondary_usage: UsageHistory | AdditionalUsageHistory | None,
    request_usage: AccountRequestUsage | None,
    additional_quotas: list[AccountAdditionalQuota] | None,
    encryptor: TokenEncryptor,
    include_auth: bool = True,
) -> AccountSummary:
    plan_type = coerce_account_plan_type(account.plan_type, DEFAULT_PLAN)
    auth_status = _build_auth_status(account, encryptor) if include_auth else None
    effective_primary_usage, effective_secondary_usage = _effective_usage_windows(
        primary_usage,
        secondary_usage,
    )
    weekly_only_usage = (
        effective_primary_usage is None
        and effective_secondary_usage is not None
        and usage_core.is_weekly_window_minutes(effective_secondary_usage.window_minutes)
    )
    # Keep account payload aligned with UI semantics: weekly-only plans expose
    # their quota as secondary/7d and omit primary/5h fields.
    primary_used_percent = _normalize_used_percent(effective_primary_usage)
    secondary_used_percent = _normalize_used_percent(effective_secondary_usage)
    primary_remaining_percent = usage_core.remaining_percent_from_used(primary_used_percent)
    secondary_remaining_percent = usage_core.remaining_percent_from_used(secondary_used_percent)

    if primary_remaining_percent is None and not weekly_only_usage:
        primary_remaining_percent = 100.0
    reset_at_primary = (
        from_epoch_seconds(effective_primary_usage.reset_at) if effective_primary_usage is not None else None
    )
    reset_at_secondary = (
        from_epoch_seconds(effective_secondary_usage.reset_at) if effective_secondary_usage is not None else None
    )
    window_minutes_primary = effective_primary_usage.window_minutes if effective_primary_usage is not None else None
    window_minutes_secondary = (
        effective_secondary_usage.window_minutes if effective_secondary_usage is not None else None
    )
    capacity_primary = usage_core.capacity_for_plan(plan_type, "primary")
    capacity_secondary = usage_core.capacity_for_plan(plan_type, "secondary")
    remaining_credits_primary = usage_core.remaining_credits_from_percent(
        primary_used_percent,
        capacity_primary,
    )
    remaining_credits_secondary = usage_core.remaining_credits_from_percent(
        secondary_used_percent,
        capacity_secondary,
    )
    return AccountSummary(
        account_id=account.id,
        email=account.email,
        display_name=account.email,
        plan_type=plan_type,
        provider_kind=account.provider_kind,
        stored_api_key=_stored_provider_api_key(account, encryptor),
        routing_tier=account_routing_tier(account),
        routing_priority=account_routing_priority(account),
        configured_priority=account_configured_priority(account),
        kyc_enabled=bool(getattr(account, "kyc_enabled", False)),
        fast_service_tier_enabled=bool(getattr(account, "fast_service_tier_enabled", False)),
        primary_drain_priority_enabled=bool(getattr(account, "primary_drain_priority_enabled", False)),
        subscription_renews_at=account.subscription_renews_at,
        groups=_account_group_names(account),
        status=account.status.value,
        usage=AccountUsage(
            primary_remaining_percent=primary_remaining_percent,
            secondary_remaining_percent=secondary_remaining_percent,
        ),
        reset_at_primary=reset_at_primary,
        reset_at_secondary=reset_at_secondary,
        window_minutes_primary=window_minutes_primary,
        window_minutes_secondary=window_minutes_secondary,
        last_refresh_at=account.last_refresh,
        capacity_credits_primary=capacity_primary,
        remaining_credits_primary=remaining_credits_primary,
        capacity_credits_secondary=capacity_secondary,
        remaining_credits_secondary=remaining_credits_secondary,
        request_usage=request_usage,
        additional_quotas=additional_quotas or [],
        deactivation_reason=account.deactivation_reason,
        auth=auth_status,
    )


def _effective_usage_windows(
    primary_usage: UsageHistory | AdditionalUsageHistory | None,
    secondary_usage: UsageHistory | AdditionalUsageHistory | None,
) -> tuple[UsageHistory | AdditionalUsageHistory | None, UsageHistory | AdditionalUsageHistory | None]:
    if primary_usage is None:
        return None, secondary_usage
    if not usage_core.is_weekly_window_minutes(primary_usage.window_minutes):
        return primary_usage, secondary_usage
    if secondary_usage is None:
        return None, primary_usage
    if usage_core.should_use_weekly_primary(_to_window_row(primary_usage), _to_window_row(secondary_usage)):
        return None, primary_usage
    return None, secondary_usage


def _to_window_row(entry: UsageHistory | AdditionalUsageHistory) -> UsageWindowRow:
    return UsageWindowRow(
        account_id=entry.account_id,
        used_percent=entry.used_percent,
        reset_at=entry.reset_at,
        window_minutes=entry.window_minutes,
        recorded_at=entry.recorded_at,
    )


def _build_auth_status(account: Account, encryptor: TokenEncryptor) -> AccountAuthStatus:
    if account.provider_kind == ACCOUNT_PROVIDER_API_KEY:
        return AccountAuthStatus(
            access=AccountTokenStatus(state="stored"),
            refresh=AccountTokenStatus(state="n/a"),
            id_token=AccountTokenStatus(state="n/a"),
        )

    access_token = _decrypt_token(encryptor, account.access_token_encrypted)
    refresh_token = _decrypt_token(encryptor, account.refresh_token_encrypted)
    id_token = _decrypt_token(encryptor, account.id_token_encrypted)

    access_expires = _token_expiry(access_token)
    refresh_state = "stored" if refresh_token else "missing"
    id_state = "unknown"
    if id_token:
        claims = extract_id_token_claims(id_token)
        if claims.model_dump(exclude_none=True):
            id_state = "parsed"

    return AccountAuthStatus(
        access=AccountTokenStatus(expires_at=access_expires),
        refresh=AccountTokenStatus(state=refresh_state),
        id_token=AccountTokenStatus(state=id_state),
    )


def _stored_provider_api_key(account: Account, encryptor: TokenEncryptor) -> str | None:
    if account.provider_kind != ACCOUNT_PROVIDER_API_KEY:
        return None
    return _decrypt_token(encryptor, account.access_token_encrypted)


def _account_group_names(account: Account) -> list[str]:
    groups = {
        membership.group_name
        for membership in getattr(account, "group_memberships", [])
        if getattr(membership, "group_name", None)
    }
    groups.update(account_builtin_group_names(account))
    return sorted(groups)


def _decrypt_token(encryptor: TokenEncryptor, encrypted: bytes | None) -> str | None:
    if not encrypted:
        return None
    try:
        return encryptor.decrypt(encrypted)
    except Exception:
        return None


def _token_expiry(token: str | None) -> datetime | None:
    if not token:
        return None
    claims = extract_id_token_claims(token)
    exp = claims.exp
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    if isinstance(exp, str) and exp.isdigit():
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    return None


def _normalize_used_percent(entry: UsageHistory | AdditionalUsageHistory | None) -> float | None:
    if not entry:
        return None
    return entry.used_percent


def build_account_usage_trends(
    buckets: list[UsageTrendBucket],
    since_epoch: int,
    bucket_seconds: int,
    bucket_count: int,
) -> dict[str, AccountUsageTrend]:
    """Convert raw UsageTrendBucket rows into per-account trend data.

    Values are expressed as remaining_percent (100 - used_percent) for UI consistency.
    Empty buckets are filled with the last known value (or 100.0 if no prior data).
    """
    # Group buckets by (account_id, window)
    grouped: dict[tuple[str, str], dict[int, float]] = {}
    for b in buckets:
        key = (b.account_id, b.window)
        grouped.setdefault(key, {})[b.bucket_epoch] = b.avg_used_percent

    # Generate the full time grid, aligned to bucket boundaries (same as SQL)
    aligned_start = (since_epoch // bucket_seconds) * bucket_seconds
    time_grid = [aligned_start + i * bucket_seconds for i in range(bucket_count)]

    result: dict[str, AccountUsageTrend] = {}
    # Collect all account_ids
    account_ids = {key[0] for key in grouped}

    for account_id in account_ids:
        primary_data = grouped.get((account_id, "primary"))
        secondary_data = grouped.get((account_id, "secondary"))

        primary_points = _fill_trend_points(time_grid, primary_data) if primary_data else []
        secondary_points = _fill_trend_points(time_grid, secondary_data) if secondary_data else []

        result[account_id] = AccountUsageTrend(
            primary=primary_points,
            secondary=secondary_points,
        )

    return result


def build_account_quota_timeline(
    *,
    primary_history: list[UsageHistory],
    secondary_history: list[UsageHistory],
    since_epoch: int,
    bucket_seconds: int,
    bucket_count: int,
    primary_capacity_credits: float | None,
) -> list[AccountQuotaTimelineBucket]:
    aligned_start = (since_epoch // bucket_seconds) * bucket_seconds
    primary_usage = _bucket_primary_usage(primary_history, aligned_start, bucket_seconds, bucket_count)
    secondary_points = sorted(secondary_history, key=_usage_history_recorded_epoch)
    timeline: list[AccountQuotaTimelineBucket] = []
    secondary_index = 0
    latest_secondary_remaining: float | None = None
    previous_secondary_used: float | None = None

    for primary_bucket in primary_usage:
        start_epoch = primary_bucket.start_epoch
        end_epoch = primary_bucket.end_epoch
        secondary_reset = False
        secondary_reset_at: datetime | None = None

        while secondary_index < len(secondary_points):
            entry = secondary_points[secondary_index]
            recorded_epoch = _usage_history_recorded_epoch(entry)
            if recorded_epoch > end_epoch:
                break
            used_percent = _normalize_percent_value(entry.used_percent)
            if used_percent is not None:
                if previous_secondary_used is not None and used_percent < previous_secondary_used:
                    secondary_reset = True
                    secondary_reset_at = _datetime_from_epoch(recorded_epoch)
                latest_secondary_remaining = usage_core.remaining_percent_from_used(used_percent)
                previous_secondary_used = used_percent
            if entry.reset_at is not None and start_epoch <= int(entry.reset_at) <= end_epoch:
                secondary_reset = True
                secondary_reset_at = from_epoch_seconds(int(entry.reset_at))
            secondary_index += 1

        primary_used_percent = round(primary_bucket.used_percent, 2)
        primary_used_credits = None
        if primary_capacity_credits is not None:
            primary_used_credits = round(max(0.0, primary_capacity_credits) * primary_used_percent / 100.0, 2)
        timeline.append(
            AccountQuotaTimelineBucket(
                start_at=from_epoch_seconds(int(start_epoch)),
                end_at=from_epoch_seconds(int(end_epoch)),
                primary_used_percent=primary_used_percent,
                primary_used_credits=primary_used_credits,
                secondary_remaining_percent=(
                    round(latest_secondary_remaining, 2) if latest_secondary_remaining is not None else None
                ),
                secondary_reset=secondary_reset,
                secondary_reset_at=secondary_reset_at,
            )
        )
    return timeline


def _bucket_primary_usage(
    rows: list[UsageHistory],
    aligned_start_epoch: int,
    bucket_seconds: int,
    bucket_count: int,
) -> list[_PrimaryUsageBucket]:
    end_epoch = aligned_start_epoch + bucket_seconds * bucket_count
    sorted_rows = sorted(rows, key=_usage_history_recorded_epoch)
    reset_anchors = _primary_reset_anchors(sorted_rows, aligned_start_epoch, end_epoch)
    buckets = _primary_usage_bucket_windows(
        aligned_start_epoch=aligned_start_epoch,
        end_epoch=end_epoch,
        bucket_seconds=bucket_seconds,
        reset_anchors=reset_anchors,
    )
    bucket_usage = [0.0 for _ in buckets]
    last_used_percent = 0.0
    previous_used_percent: float | None = None
    for row in sorted_rows:
        recorded_epoch = _usage_history_recorded_epoch(row)
        if recorded_epoch < aligned_start_epoch or recorded_epoch > end_epoch:
            continue
        current_used_percent = _normalize_percent_value(row.used_percent)
        if current_used_percent is None:
            continue
        if _primary_usage_reset_detected(previous_used_percent, current_used_percent):
            last_used_percent = 0.0
        bucket_index = _primary_bucket_index_for_epoch(buckets, recorded_epoch)
        if bucket_index is None:
            previous_used_percent = current_used_percent
            last_used_percent = current_used_percent
            continue
        if current_used_percent >= last_used_percent:
            bucket_usage[bucket_index] += current_used_percent - last_used_percent
        else:
            bucket_usage[bucket_index] += current_used_percent
        last_used_percent = current_used_percent
        previous_used_percent = current_used_percent
    return [
        _PrimaryUsageBucket(
            start_epoch=bucket.start_epoch,
            end_epoch=bucket.end_epoch,
            used_percent=max(0.0, min(100.0, bucket_usage[index])),
        )
        for index, bucket in enumerate(buckets)
    ]


def _primary_reset_anchors(
    sorted_rows: list[UsageHistory],
    aligned_start_epoch: int,
    end_epoch: int,
) -> list[float]:
    anchors: list[float] = []
    previous_used_percent: float | None = None
    for row in sorted_rows:
        recorded_epoch = _usage_history_recorded_epoch(row)
        current_used_percent = _normalize_percent_value(row.used_percent)
        if current_used_percent is None:
            continue
        if _primary_usage_reset_detected(previous_used_percent, current_used_percent):
            anchor = _primary_window_start_epoch(row) or recorded_epoch
            if aligned_start_epoch < anchor < end_epoch:
                anchors.append(anchor)
        previous_used_percent = current_used_percent
    return _dedupe_close_epochs(sorted(anchors))


def _primary_usage_reset_detected(previous_used_percent: float | None, current_used_percent: float) -> bool:
    if previous_used_percent is None:
        return False
    if current_used_percent < previous_used_percent:
        return True
    return current_used_percent <= PRIMARY_RESET_USED_THRESHOLD_PCT < previous_used_percent


def _primary_window_start_epoch(row: UsageHistory) -> float | None:
    if row.reset_at is None or row.window_minutes is None or row.window_minutes <= 0:
        return None
    return float(row.reset_at - row.window_minutes * 60)


def _dedupe_close_epochs(epochs: list[float]) -> list[float]:
    deduped: list[float] = []
    for epoch in epochs:
        if deduped and abs(epoch - deduped[-1]) < 60:
            continue
        deduped.append(epoch)
    return deduped


def _primary_usage_bucket_windows(
    *,
    aligned_start_epoch: int,
    end_epoch: int,
    bucket_seconds: int,
    reset_anchors: list[float],
) -> list[_PrimaryUsageBucket]:
    buckets: list[_PrimaryUsageBucket] = []
    segment_starts = [float(aligned_start_epoch), *reset_anchors]
    for index, segment_start in enumerate(segment_starts):
        segment_end = reset_anchors[index] if index < len(reset_anchors) else float(end_epoch)
        bucket_start = segment_start
        while bucket_start < segment_end:
            bucket_end = min(bucket_start + bucket_seconds, segment_end)
            if bucket_end > bucket_start:
                is_reset_tail = index < len(reset_anchors) and bucket_end == segment_end
                bucket_duration = bucket_end - bucket_start
                if is_reset_tail and buckets and bucket_duration < MIN_STANDALONE_PRIMARY_BUCKET_SECONDS:
                    previous = buckets[-1]
                    buckets[-1] = _PrimaryUsageBucket(previous.start_epoch, bucket_end, previous.used_percent)
                    break
                buckets.append(_PrimaryUsageBucket(bucket_start, bucket_end, 0.0))
            bucket_start += bucket_seconds
    return buckets


def _primary_bucket_index_for_epoch(buckets: list[_PrimaryUsageBucket], epoch: float) -> int | None:
    for index, bucket in enumerate(buckets):
        is_last = index == len(buckets) - 1
        if bucket.start_epoch <= epoch and (epoch < bucket.end_epoch or is_last and epoch <= bucket.end_epoch):
            return index
    return None


def _normalize_percent_value(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def _usage_history_recorded_epoch(entry: UsageHistory) -> float:
    if entry.recorded_at is None:
        return 0.0
    recorded_at = (
        entry.recorded_at if entry.recorded_at.tzinfo is not None else entry.recorded_at.replace(tzinfo=timezone.utc)
    )
    return recorded_at.timestamp()


def _datetime_from_epoch(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _fill_trend_points(
    time_grid: list[int],
    bucket_data: dict[int, float],
) -> list[UsageTrendPoint]:
    """Fill missing buckets with last-known value and convert to remaining percent."""
    points: list[UsageTrendPoint] = []
    last_value = 100.0  # assume full remaining if no prior data
    for epoch in time_grid:
        if epoch in bucket_data:
            remaining = max(0.0, min(100.0, 100.0 - bucket_data[epoch]))
            last_value = remaining
        else:
            remaining = last_value
        points.append(
            UsageTrendPoint(
                t=datetime.fromtimestamp(epoch, tz=timezone.utc),
                v=round(remaining, 2),
            )
        )
    return points
