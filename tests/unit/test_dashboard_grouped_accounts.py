from __future__ import annotations

import pytest

from app.modules.accounts.schemas import AccountSummary
from app.modules.dashboard.service import _merge_domain_group

pytestmark = pytest.mark.unit


def _summary(account_id: str, status: str) -> AccountSummary:
    return AccountSummary(
        account_id=account_id,
        email=f"{account_id}@example.com",
        display_name=f"{account_id}@example.com",
        plan_type="plus",
        status=status,
    )


def test_merge_domain_group_exposes_availability_breakdown_without_fail_count() -> None:
    grouped = _merge_domain_group(
        "example.com",
        [
            _summary("active_1", "active"),
            _summary("active_2", "active"),
            _summary("limited", "rate_limited"),
            _summary("quota", "quota_exceeded"),
            _summary("paused", "paused"),
            _summary("unknown", "unknown"),
        ],
    )

    assert grouped.email == "2 available / 6 total"
    assert grouped.availability is not None
    assert grouped.availability.total == 6
    assert grouped.availability.active == 2
    assert grouped.availability.rate_limited == 1
    assert grouped.availability.quota_limited == 1
    assert grouped.availability.paused == 1
    assert grouped.availability.deactivated == 0
    assert not hasattr(grouped.availability, "failed")
