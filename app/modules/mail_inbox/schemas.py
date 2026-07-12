from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.modules.shared.schemas import DashboardModel

MailProvider = Literal["gmail", "outlook", "imap"]
MailSyncStatusValue = Literal["never_synced", "synced", "error", "disabled"]
MailFocusRuleKindValue = Literal["sender_email", "sender_domain", "keyword"]


class MailAccountCreateRequest(DashboardModel):
    provider: MailProvider
    address: str
    display_name: str | None = None
    imap_host: str | None = None
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_username: str | None = None
    credential: str | None = None

    @field_validator("address")
    @classmethod
    def _normalize_address(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Address is required")
        return normalized


class MailAccountUpdateRequest(DashboardModel):
    enabled: bool | None = None
    display_name: str | None = None


class MailAccountResponse(DashboardModel):
    id: str
    provider: MailProvider
    address: str
    display_name: str | None
    enabled: bool
    sync_status: MailSyncStatusValue
    last_sync_at: datetime | None
    last_sync_error: str | None
    imap_host: str | None
    imap_port: int | None
    imap_username: str | None
    created_at: datetime
    updated_at: datetime


class MailAccountsResponse(DashboardModel):
    accounts: list[MailAccountResponse]


class MailAccountSyncResponse(DashboardModel):
    account: MailAccountResponse
    imported_count: int


class MailMessageIngestRequest(DashboardModel):
    account_id: str
    provider_message_id: str
    thread_id: str | None = None
    sender_email: str
    sender_name: str | None = None
    recipients: list[str] = Field(default_factory=list)
    subject: str
    snippet: str = ""
    received_at: datetime
    unread: bool = False
    starred: bool = False
    has_attachments: bool = False

    @field_validator("sender_email")
    @classmethod
    def _normalize_sender(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Sender email is required")
        return normalized


class MailMessageResponse(DashboardModel):
    id: str
    account_id: str
    account_address: str
    account_provider: MailProvider
    provider_message_id: str
    thread_id: str | None
    sender_email: str
    sender_name: str | None
    recipients: list[str]
    subject: str
    snippet: str
    received_at: datetime
    unread: bool
    starred: bool
    has_attachments: bool
    focused: bool
    focus_label: str | None


class MailMessagesResponse(DashboardModel):
    messages: list[MailMessageResponse]


class MailFocusRuleCreateRequest(DashboardModel):
    kind: MailFocusRuleKindValue
    value: str
    label: str | None = None

    @field_validator("value")
    @classmethod
    def _normalize_value(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Rule value is required")
        return normalized


class MailFocusRuleUpdateRequest(DashboardModel):
    enabled: bool


class MailFocusRuleResponse(DashboardModel):
    id: str
    kind: MailFocusRuleKindValue
    value: str
    label: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class MailFocusRulesResponse(DashboardModel):
    rules: list[MailFocusRuleResponse]
