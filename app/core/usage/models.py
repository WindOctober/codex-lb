from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UsageWindow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    used_percent: float | None = None
    reset_at: int | None = None
    limit_window_seconds: int | None = None
    reset_after_seconds: int | None = None


class RateLimitPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_window: UsageWindow | None = None
    secondary_window: UsageWindow | None = None


class CreditsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: str | None = None


class RateLimitResetCreditsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    available_count: int


class RateLimitResetCreditPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    reset_type: str | None = None
    title: str | None = None
    granted_at: datetime | None = None
    expires_at: datetime | None = None


class RateLimitResetCreditBankPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    credits: list[RateLimitResetCreditPayload] = Field(default_factory=list)
    available_count: int = 0

    @property
    def available_credits(self) -> list[RateLimitResetCreditPayload]:
        return [credit for credit in self.credits if credit.status == "available"]


class AdditionalRateLimitPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    limit_name: str
    metered_feature: str
    rate_limit: RateLimitPayload | None = None


class UsagePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plan_type: str | None = None
    rate_limit: RateLimitPayload | None = None
    credits: CreditsPayload | None = None
    rate_limit_reset_credits: RateLimitResetCreditsPayload | None = None
    additional_rate_limits: list[AdditionalRateLimitPayload] | None = None


RateLimitResetConsumeCode = Literal["reset", "nothing_to_reset", "no_credit", "already_redeemed"]


class RateLimitResetConsumePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: RateLimitResetConsumeCode
    windows_reset: int = 0
