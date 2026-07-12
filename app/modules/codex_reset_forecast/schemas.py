from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.modules.shared.schemas import DashboardModel

ResetSignalKind = Literal[
    "explicit_reset_announcement",
    "tibo_ok_request",
    "sam_trigger",
    "issue_acknowledged",
    "limit_fix",
    "confirmed_reset",
    "no_active_signal",
]
ProbabilityLevel = Literal["low", "elevated", "high"]
ForecastConfidence = Literal["low", "medium", "high"]
CodexResetXItemKind = Literal["post", "reply", "quote"]
CodexResetXItemRelevance = Literal["none", "weak", "strong"]


class CodexResetEvidence(DashboardModel):
    kind: ResetSignalKind
    label: str
    summary: str
    observed_at: datetime | None = None
    url: str | None = None
    source: str
    age_hours: float | None = Field(default=None, ge=0.0)
    score: float = Field(ge=0.0, le=1.0)


class CodexResetScoreFactor(DashboardModel):
    key: str
    label: str
    value: float = Field(ge=0.0, le=1.0)
    description: str


class CodexResetHistoricalExample(DashboardModel):
    label: str
    classification: str
    signal_at: datetime
    reset_at: datetime
    lead_time_hours: float = Field(gt=0.0)
    signal_summary: str
    reset_summary: str
    signal_url: str
    reset_url: str


class CodexResetCollectionStatus(DashboardModel):
    refresh_enabled: bool
    refresh_in_progress: bool
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    next_refresh_due_at: datetime | None = None


class CodexResetXActivityItem(DashboardModel):
    class ParentItem(DashboardModel):
        author_handle: str
        text: str
        translated_text_zh: str
        url: str | None = None

    author_handle: str
    kind: CodexResetXItemKind
    observed_at: datetime
    text: str
    translated_text_zh: str
    url: str
    reply_to: str | None = None
    parent: ParentItem | None = None
    reset_relevance: CodexResetXItemRelevance
    relevance_summary: str


class CodexResetForecastResponse(DashboardModel):
    generated_at: datetime
    horizon_hours: int = Field(gt=0)
    probability: float = Field(ge=0.0, le=1.0)
    probability_percent: int = Field(ge=0, le=100)
    probability_level: ProbabilityLevel
    confidence: ForecastConfidence
    summary: str
    model_version: str
    source_note: str
    collection_status: CodexResetCollectionStatus
    current_evidence: list[CodexResetEvidence]
    latest_x_items: list[CodexResetXActivityItem]
    score_factors: list[CodexResetScoreFactor]
    historical_examples: list[CodexResetHistoricalExample]
