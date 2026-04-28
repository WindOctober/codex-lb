from __future__ import annotations

import pytest

import app.modules.dashboard.service as dashboard_service
from app.db.models import ACCOUNT_PROVIDER_OPENAI_OAUTH, Account
from app.modules.accounts.schemas import AccountSummary
from app.modules.dashboard.service import _build_grouped_dashboard_accounts, _merge_domain_group

pytestmark = pytest.mark.unit


def _summary(
    account_id: str,
    status: str,
    *,
    plan_type: str = "plus",
    secondary_capacity: float | None = None,
    secondary_remaining: float | None = None,
) -> AccountSummary:
    return AccountSummary(
        account_id=account_id,
        email=f"{account_id}@example.com",
        display_name=f"{account_id}@example.com",
        plan_type=plan_type,
        status=status,
        capacity_credits_secondary=secondary_capacity,
        remaining_credits_secondary=secondary_remaining,
    )


def _account(account_id: str, domain: str = "example.com") -> Account:
    return Account(
        id=account_id,
        email=f"{account_id}@{domain}",
        provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
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


def test_grouped_dashboard_accounts_skip_fully_deactivated_domain_groups() -> None:
    grouped = _build_grouped_dashboard_accounts(
        [
            _account("dead_1", "dead.example"),
            _account("dead_2", "dead.example"),
            _account("active_1", "active.example"),
            _account("dead_3", "active.example"),
        ],
        [
            _summary("dead_1", "deactivated").model_copy(update={"email": "dead_1@dead.example"}),
            _summary("dead_2", "deactivated").model_copy(update={"email": "dead_2@dead.example"}),
            _summary("active_1", "active").model_copy(update={"email": "active_1@active.example"}),
            _summary("dead_3", "deactivated").model_copy(update={"email": "dead_3@active.example"}),
        ],
    )

    assert [account.account_id for account in grouped] == ["domain:active.example"]
    assert grouped[0].availability is not None
    assert grouped[0].availability.total == 2
    assert grouped[0].availability.active == 1
    assert grouped[0].availability.deactivated == 1


def test_merge_domain_group_quota_ignores_deactivated_and_latest_model_unsupported_members(monkeypatch) -> None:
    class _Registry:
        def plan_types_for_model(self, model: str) -> frozenset[str]:
            assert model == "gpt-5.5"
            return frozenset({"plus", "team"})

    monkeypatch.setattr(dashboard_service, "get_latest_model_id", lambda: "gpt-5.5")
    monkeypatch.setattr(dashboard_service, "get_model_registry", lambda: _Registry())

    grouped = _merge_domain_group(
        "outlook.com",
        [
            _summary(
                "plus_active",
                "active",
                plan_type="plus",
                secondary_capacity=11340.0,
                secondary_remaining=11340.0,
            ),
            _summary(
                "free_active",
                "active",
                plan_type="free",
                secondary_capacity=1134.0,
                secondary_remaining=11.34,
            ),
            _summary(
                "team_deactivated",
                "deactivated",
                plan_type="team",
                secondary_capacity=11340.0,
                secondary_remaining=0.0,
            ),
        ],
    )

    assert grouped.availability is not None
    assert grouped.availability.total == 3
    assert grouped.availability.active == 2
    assert grouped.availability.deactivated == 1
    assert grouped.capacity_credits_secondary == pytest.approx(11340.0)
    assert grouped.remaining_credits_secondary == pytest.approx(11340.0)
    assert grouped.usage is not None
    assert grouped.usage.secondary_remaining_percent == pytest.approx(100.0)
