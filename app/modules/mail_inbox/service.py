from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.crypto import TokenEncryptor
from app.db.models import (
    MailAccount,
    MailAccountProvider,
    MailFocusRule,
    MailFocusRuleKind,
    MailMessage,
    MailSyncStatus,
)
from app.modules.mail_inbox.imap_sync import ImapConnectionSettings, fetch_imap_messages
from app.modules.mail_inbox.repository import MailInboxRepository


class MailInboxValidationError(ValueError):
    pass


class MailInboxNotFoundError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MailAccountCreateData:
    provider: str
    address: str
    display_name: str | None
    imap_host: str | None
    imap_port: int | None
    imap_username: str | None
    credential: str | None


@dataclass(frozen=True, slots=True)
class MailMessageIngestData:
    account_id: str
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


@dataclass(frozen=True, slots=True)
class FocusMatch:
    focused: bool
    label: str | None


class MailInboxService:
    def __init__(self, repository: MailInboxRepository, encryptor: TokenEncryptor | None = None) -> None:
        self._repository = repository
        self._encryptor = encryptor or TokenEncryptor()

    async def create_account(self, data: MailAccountCreateData) -> MailAccount:
        provider = _coerce_provider(data.provider)
        if provider == MailAccountProvider.IMAP and (not data.imap_host or data.imap_port is None):
            raise MailInboxValidationError("IMAP host and port are required")
        credential_encrypted = self._encryptor.encrypt(data.credential) if data.credential else None
        account = MailAccount(
            provider=provider,
            address=_normalize_email(data.address),
            display_name=data.display_name.strip() if data.display_name else None,
            imap_host=data.imap_host,
            imap_port=data.imap_port,
            imap_username=data.imap_username,
            credential_encrypted=credential_encrypted,
            sync_status=MailSyncStatus.NEVER_SYNCED,
        )
        return await self._repository.create_account(account)

    async def list_accounts(self) -> Sequence[MailAccount]:
        return await self._repository.list_accounts()

    async def update_account(self, account_id: str, *, enabled: bool | None, display_name: str | None) -> MailAccount:
        account = await self._repository.get_account(account_id)
        if account is None:
            raise MailInboxNotFoundError("Mail account not found")
        if enabled is not None:
            account.enabled = enabled
            account.sync_status = MailSyncStatus.NEVER_SYNCED if enabled else MailSyncStatus.DISABLED
        if display_name is not None:
            account.display_name = display_name.strip() or None
        return await self._repository.update_account(account)

    async def ingest_message(self, data: MailMessageIngestData) -> MailMessage:
        account = await self._repository.get_account(data.account_id)
        if account is None:
            raise MailInboxNotFoundError("Mail account not found")
        rules = await self._repository.list_focus_rules(enabled_only=True)
        match = _evaluate_focus(
            sender_email=_normalize_email(data.sender_email),
            subject=data.subject,
            snippet=data.snippet,
            rules=rules,
        )
        message = MailMessage(
            account_id=data.account_id,
            provider_message_id=data.provider_message_id,
            thread_id=data.thread_id,
            sender_email=_normalize_email(data.sender_email),
            sender_name=data.sender_name,
            recipients_json=json.dumps([recipient.strip() for recipient in data.recipients if recipient.strip()]),
            subject=data.subject,
            snippet=data.snippet,
            received_at=data.received_at,
            unread=data.unread,
            starred=data.starred,
            has_attachments=data.has_attachments,
            focused=match.focused,
            focus_label=match.label,
        )
        return await self._repository.upsert_message(message)

    async def list_messages(
        self,
        *,
        account_id: str | None,
        focused: bool | None,
        unread: bool | None,
        limit: int,
    ) -> Sequence[MailMessage]:
        return await self._repository.list_messages(account_id=account_id, focused=focused, unread=unread, limit=limit)

    async def sync_account(self, account_id: str, *, limit: int = 50) -> tuple[MailAccount, int]:
        account = await self._repository.get_account(account_id)
        if account is None:
            raise MailInboxNotFoundError("Mail account not found")
        if not account.enabled:
            raise MailInboxValidationError("Mail account is disabled")
        try:
            settings = self._imap_settings(account, limit=limit)
            fetched_messages = await asyncio.to_thread(fetch_imap_messages, settings)
            imported_count = 0
            for fetched in fetched_messages:
                await self.ingest_message(
                    MailMessageIngestData(
                        account_id=account.id,
                        provider_message_id=fetched.provider_message_id,
                        thread_id=None,
                        sender_email=fetched.sender_email,
                        sender_name=fetched.sender_name,
                        recipients=fetched.recipients,
                        subject=fetched.subject,
                        snippet=fetched.snippet,
                        received_at=fetched.received_at,
                        unread=fetched.unread,
                        starred=fetched.starred,
                        has_attachments=False,
                    )
                )
                imported_count += 1
        except MailInboxValidationError:
            raise
        except Exception as exc:
            account.sync_status = MailSyncStatus.ERROR
            account.last_sync_error = str(exc)
            await self._repository.update_account(account)
            raise MailInboxValidationError(f"Mail sync failed: {exc}") from exc

        account.sync_status = MailSyncStatus.SYNCED
        account.last_sync_at = datetime.now(tz=UTC).replace(tzinfo=None)
        account.last_sync_error = None
        updated = await self._repository.update_account(account)
        return updated, imported_count

    async def create_focus_rule(self, *, kind: str, value: str, label: str | None) -> MailFocusRule:
        rule = MailFocusRule(
            kind=_coerce_focus_rule_kind(kind),
            value=_normalize_rule_value(value),
            label=label.strip() if label else None,
            enabled=True,
        )
        created = await self._repository.create_focus_rule(rule)
        await self.recompute_focus()
        return created

    async def list_focus_rules(self) -> Sequence[MailFocusRule]:
        return await self._repository.list_focus_rules()

    async def update_focus_rule(self, rule_id: str, *, enabled: bool) -> MailFocusRule:
        rule = await self._repository.get_focus_rule(rule_id)
        if rule is None:
            raise MailInboxNotFoundError("Focus rule not found")
        rule.enabled = enabled
        updated = await self._repository.update_focus_rule(rule)
        await self.recompute_focus()
        return updated

    async def recompute_focus(self) -> None:
        rules = await self._repository.list_focus_rules(enabled_only=True)
        messages = await self._repository.list_all_messages()
        for message in messages:
            match = _evaluate_focus(
                sender_email=message.sender_email,
                subject=message.subject,
                snippet=message.snippet,
                rules=rules,
            )
            message.focused = match.focused
            message.focus_label = match.label
        await self._repository.commit()

    def _imap_settings(self, account: MailAccount, *, limit: int) -> ImapConnectionSettings:
        if account.credential_encrypted is None:
            raise MailInboxValidationError("Mail account credential is required for sync")
        host, port = _resolve_imap_endpoint(account)
        username = account.imap_username or account.address
        return ImapConnectionSettings(
            host=host,
            port=port,
            username=username,
            password=self._encryptor.decrypt(account.credential_encrypted),
            limit=limit,
        )

def _coerce_provider(value: str) -> MailAccountProvider:
    try:
        return MailAccountProvider(value)
    except ValueError as exc:
        raise MailInboxValidationError("Invalid mail provider") from exc


def _coerce_focus_rule_kind(value: str) -> MailFocusRuleKind:
    try:
        return MailFocusRuleKind(value)
    except ValueError as exc:
        raise MailInboxValidationError("Invalid focus rule kind") from exc


def _normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise MailInboxValidationError("Email address is required")
    return normalized


def _normalize_rule_value(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise MailInboxValidationError("Focus rule value is required")
    return normalized


def _resolve_imap_endpoint(account: MailAccount) -> tuple[str, int]:
    if account.provider == MailAccountProvider.GMAIL:
        return account.imap_host or "imap.gmail.com", account.imap_port or 993
    if account.provider == MailAccountProvider.OUTLOOK:
        return account.imap_host or "outlook.office365.com", account.imap_port or 993
    if account.imap_host and account.imap_port is not None:
        return account.imap_host, account.imap_port
    raise MailInboxValidationError("IMAP host and port are required")


def _evaluate_focus(
    *,
    sender_email: str,
    subject: str,
    snippet: str,
    rules: Sequence[MailFocusRule],
) -> FocusMatch:
    sender_domain = sender_email.rsplit("@", maxsplit=1)[-1] if "@" in sender_email else ""
    searchable = f"{subject}\n{snippet}".lower()
    for rule in rules:
        if rule.kind == MailFocusRuleKind.SENDER_EMAIL and sender_email == rule.value:
            return FocusMatch(True, rule.label or rule.value)
        if rule.kind == MailFocusRuleKind.SENDER_DOMAIN and sender_domain == rule.value:
            return FocusMatch(True, rule.label or rule.value)
        if rule.kind == MailFocusRuleKind.KEYWORD and rule.value in searchable:
            return FocusMatch(True, rule.label or rule.value)
    return FocusMatch(False, None)
