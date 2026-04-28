from __future__ import annotations

import json

import pytest

from app.core.usage import capacity_for_plan
from app.db.models import ACCOUNT_PROVIDER_OPENAI_OAUTH, Account, AccountStatus, AdditionalUsageHistory
from app.modules.accounts.mappers import build_account_summaries
from app.modules.usage.latest_model import clear_latest_model_cache, get_latest_model_config

pytestmark = pytest.mark.unit


def test_latest_model_config_reads_quota_key(monkeypatch, tmp_path) -> None:
    path = tmp_path / "latest_model.json"
    path.write_text(
        json.dumps(
            {
                "model_id": "gpt-5.5",
                "quota_key": "gpt_5_5",
                "display_label": "GPT-5.5",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_LB_LATEST_MODEL_FILE", str(path))
    clear_latest_model_cache()

    config = get_latest_model_config()

    assert config is not None
    assert config.model_id == "gpt-5.5"
    assert config.quota_key == "gpt_5_5"
    assert config.display_label == "GPT-5.5"


def test_account_summary_can_use_latest_model_additional_weekly_quota() -> None:
    account = Account(
        id="acc_plus",
        email="plus@example.com",
        plan_type="plus",
        provider_kind=ACCOUNT_PROVIDER_OPENAI_OAUTH,
        status=AccountStatus.ACTIVE,
    )
    latest_secondary = AdditionalUsageHistory(
        account_id=account.id,
        quota_key="gpt_5_5",
        limit_name="GPT-5.5",
        metered_feature="gpt_5_5",
        window="secondary",
        used_percent=25.0,
        window_minutes=10080,
        reset_at=3000,
    )

    summary = build_account_summaries(
        accounts=[account],
        primary_usage={},
        secondary_usage={account.id: latest_secondary},
        request_usage_by_account={},
        additional_quotas_by_account={},
        encryptor=object(),  # type: ignore[arg-type]
        include_auth=False,
    )[0]

    assert summary.usage is not None
    assert summary.usage.secondary_remaining_percent == 75.0
    assert summary.window_minutes_secondary == 10080
    assert summary.capacity_credits_secondary == capacity_for_plan("plus", "secondary")
    assert summary.capacity_credits_secondary == pytest.approx(11340.0)
    assert summary.remaining_credits_secondary == pytest.approx(8505.0)
