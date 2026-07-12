from __future__ import annotations

from datetime import datetime, timezone

from app.core.usage.types import UsageTrendBucket
from app.db.models import UsageHistory
from app.modules.accounts.mappers import build_account_quota_timeline, build_account_usage_trends


def _bucket(epoch: int, account_id: str, window: str, avg_used: float, samples: int = 1) -> UsageTrendBucket:
    return UsageTrendBucket(
        bucket_epoch=epoch,
        account_id=account_id,
        window=window,
        avg_used_percent=avg_used,
        samples=samples,
    )


# Use a value already aligned to BUCKET_SECONDS so tests are predictable
BUCKET_SECONDS = 21600  # 6h
SINCE_EPOCH = (1_700_000_000 // BUCKET_SECONDS) * BUCKET_SECONDS
BUCKET_COUNT = 4  # 4 buckets → spans 24h


class TestBuildAccountUsageTrends:
    def test_empty_buckets_returns_empty(self):
        result = build_account_usage_trends([], SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        assert result == {}

    def test_single_account_single_window(self):
        buckets = [
            _bucket(SINCE_EPOCH, "a1", "primary", 20.0),
            _bucket(SINCE_EPOCH + BUCKET_SECONDS, "a1", "primary", 40.0),
        ]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)

        assert "a1" in result
        trend = result["a1"]
        assert len(trend.primary) == BUCKET_COUNT

        # First bucket: 100 - 20 = 80
        assert trend.primary[0].v == 80.0
        # Second bucket: 100 - 40 = 60
        assert trend.primary[1].v == 60.0
        # Third and fourth buckets: forward-filled with last value (60)
        assert trend.primary[2].v == 60.0
        assert trend.primary[3].v == 60.0

    def test_values_are_remaining_percent(self):
        buckets = [_bucket(SINCE_EPOCH, "a1", "primary", 75.0)]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        assert result["a1"].primary[0].v == 25.0

    def test_used_percent_clamped_to_0_100(self):
        buckets = [_bucket(SINCE_EPOCH, "a1", "primary", 110.0)]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        # 100 - 110 = -10, clamped to 0
        assert result["a1"].primary[0].v == 0.0

    def test_missing_buckets_filled_with_default(self):
        # No data at all for bucket 0, data at bucket 1
        buckets = [_bucket(SINCE_EPOCH + BUCKET_SECONDS, "a1", "primary", 50.0)]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)

        # Bucket 0: no data → default 100.0
        assert result["a1"].primary[0].v == 100.0
        # Bucket 1: 100 - 50 = 50
        assert result["a1"].primary[1].v == 50.0

    def test_dual_window(self):
        buckets = [
            _bucket(SINCE_EPOCH, "a1", "primary", 20.0),
            _bucket(SINCE_EPOCH, "a1", "secondary", 30.0),
        ]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        trend = result["a1"]
        assert trend.primary[0].v == 80.0
        assert trend.secondary[0].v == 70.0

    def test_multiple_accounts(self):
        buckets = [
            _bucket(SINCE_EPOCH, "a1", "primary", 10.0),
            _bucket(SINCE_EPOCH, "a2", "primary", 90.0),
        ]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        assert result["a1"].primary[0].v == 90.0
        assert result["a2"].primary[0].v == 10.0

    def test_missing_window_returns_empty_list(self):
        buckets = [_bucket(SINCE_EPOCH, "a1", "primary", 20.0)]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        # secondary was not in any bucket → empty list
        assert result["a1"].secondary == []

    def test_timestamps_are_utc(self):
        buckets = [_bucket(SINCE_EPOCH, "a1", "primary", 0.0)]
        result = build_account_usage_trends(buckets, SINCE_EPOCH, BUCKET_SECONDS, BUCKET_COUNT)
        for point in result["a1"].primary:
            assert point.t.tzinfo is not None


class TestBuildAccountQuotaTimeline:
    def test_builds_primary_usage_and_secondary_remaining_buckets(self):
        bucket_seconds = 5 * 3600
        since_epoch = (1_704_067_200 // bucket_seconds) * bucket_seconds
        primary_history = [
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=30.0,
                recorded_at=datetime.fromtimestamp(since_epoch + 3600, tz=timezone.utc),
            ),
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=80.0,
                recorded_at=datetime.fromtimestamp(since_epoch + 2 * 3600, tz=timezone.utc),
            ),
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=20.0,
                recorded_at=datetime.fromtimestamp(since_epoch + bucket_seconds + 3600, tz=timezone.utc),
            ),
        ]
        secondary_history = [
            UsageHistory(
                account_id="a1",
                window="secondary",
                used_percent=40.0,
                recorded_at=datetime.fromtimestamp(since_epoch + 2 * 3600, tz=timezone.utc),
            ),
            UsageHistory(
                account_id="a1",
                window="secondary",
                used_percent=5.0,
                reset_at=since_epoch + bucket_seconds + 1800,
                recorded_at=datetime.fromtimestamp(since_epoch + bucket_seconds + 3600, tz=timezone.utc),
            ),
        ]

        timeline = build_account_quota_timeline(
            primary_history=primary_history,
            secondary_history=secondary_history,
            since_epoch=since_epoch,
            bucket_seconds=bucket_seconds,
            bucket_count=2,
            primary_capacity_credits=100.0,
        )

        assert timeline[0].primary_used_percent == 80.0
        assert timeline[0].primary_used_credits == 80.0
        assert timeline[0].secondary_remaining_percent == 60.0
        assert timeline[0].secondary_reset is False
        assert timeline[1].primary_used_percent == 0.0
        assert timeline[1].secondary_remaining_percent == 95.0
        assert timeline[1].secondary_reset is True
        assert timeline[1].secondary_reset_at is not None
        assert timeline[2].primary_used_percent == 20.0

    def test_primary_usage_reanchors_buckets_after_quota_returns_to_full(self):
        bucket_seconds = 5 * 3600
        since_epoch = int(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())
        aligned_start = (since_epoch // bucket_seconds) * bucket_seconds
        reset_anchor = aligned_start + 6 * 3600
        primary_history = [
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=80.0,
                reset_at=aligned_start + bucket_seconds,
                window_minutes=300,
                recorded_at=datetime.fromtimestamp(aligned_start + 1 * 3600, tz=timezone.utc),
            ),
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=90.0,
                reset_at=aligned_start + bucket_seconds,
                window_minutes=300,
                recorded_at=datetime.fromtimestamp(aligned_start + 4 * 3600, tz=timezone.utc),
            ),
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=7.0,
                reset_at=reset_anchor + bucket_seconds,
                window_minutes=300,
                recorded_at=datetime.fromtimestamp(reset_anchor + 15 * 60, tz=timezone.utc),
            ),
        ]

        timeline = build_account_quota_timeline(
            primary_history=primary_history,
            secondary_history=[],
            since_epoch=since_epoch,
            bucket_seconds=bucket_seconds,
            bucket_count=3,
            primary_capacity_credits=100.0,
        )

        assert timeline[0].start_at == datetime.fromtimestamp(aligned_start, tz=timezone.utc)
        assert timeline[0].end_at == datetime.fromtimestamp(aligned_start + bucket_seconds, tz=timezone.utc)
        assert timeline[0].primary_used_percent == 90.0
        assert timeline[1].start_at == datetime.fromtimestamp(aligned_start + bucket_seconds, tz=timezone.utc)
        assert timeline[1].end_at == datetime.fromtimestamp(reset_anchor, tz=timezone.utc)
        assert timeline[1].primary_used_percent == 0.0
        assert timeline[2].start_at == datetime.fromtimestamp(reset_anchor, tz=timezone.utc)
        assert timeline[2].primary_used_percent == 7.0

    def test_primary_usage_merges_tiny_tail_before_reset_anchor(self):
        bucket_seconds = 5 * 3600
        aligned_start = int(datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc).timestamp())
        reset_anchor = aligned_start + bucket_seconds + 35 * 60
        primary_history = [
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=86.0,
                reset_at=aligned_start + bucket_seconds,
                window_minutes=300,
                recorded_at=datetime.fromtimestamp(aligned_start + 4 * 3600, tz=timezone.utc),
            ),
            UsageHistory(
                account_id="a1",
                window="primary",
                used_percent=7.0,
                reset_at=reset_anchor + bucket_seconds,
                window_minutes=300,
                recorded_at=datetime.fromtimestamp(reset_anchor + 10 * 60, tz=timezone.utc),
            ),
        ]

        timeline = build_account_quota_timeline(
            primary_history=primary_history,
            secondary_history=[],
            since_epoch=aligned_start,
            bucket_seconds=bucket_seconds,
            bucket_count=3,
            primary_capacity_credits=100.0,
        )

        assert timeline[0].start_at == datetime.fromtimestamp(aligned_start, tz=timezone.utc)
        assert timeline[0].end_at == datetime.fromtimestamp(reset_anchor, tz=timezone.utc)
        assert timeline[0].primary_used_percent == 86.0
        assert timeline[1].start_at == datetime.fromtimestamp(reset_anchor, tz=timezone.utc)
        assert timeline[1].primary_used_percent == 7.0
