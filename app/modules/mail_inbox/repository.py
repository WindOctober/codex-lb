from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import MailAccount, MailFocusRule, MailMessage


class MailInboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_account(self, account: MailAccount) -> MailAccount:
        self._session.add(account)
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def get_account(self, account_id: str) -> MailAccount | None:
        return await self._session.get(MailAccount, account_id)

    async def list_accounts(self) -> Sequence[MailAccount]:
        result = await self._session.execute(select(MailAccount).order_by(MailAccount.created_at.desc()))
        return result.scalars().all()

    async def update_account(self, account: MailAccount) -> MailAccount:
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def upsert_message(self, message: MailMessage) -> MailMessage:
        existing = await self.get_message_by_provider_id(message.account_id, message.provider_message_id)
        if existing is None:
            self._session.add(message)
            await self._session.commit()
            loaded = await self.get_message(message.id)
            assert loaded is not None
            return loaded

        existing.thread_id = message.thread_id
        existing.sender_email = message.sender_email
        existing.sender_name = message.sender_name
        existing.recipients_json = message.recipients_json
        existing.subject = message.subject
        existing.snippet = message.snippet
        existing.received_at = message.received_at
        existing.unread = message.unread
        existing.starred = message.starred
        existing.has_attachments = message.has_attachments
        existing.focused = message.focused
        existing.focus_label = message.focus_label
        await self._session.commit()
        loaded = await self.get_message(existing.id)
        assert loaded is not None
        return loaded

    async def get_message(self, message_id: str) -> MailMessage | None:
        result = await self._session.execute(
            select(MailMessage).options(selectinload(MailMessage.account)).where(MailMessage.id == message_id)
        )
        return result.scalar_one_or_none()

    async def get_message_by_provider_id(self, account_id: str, provider_message_id: str) -> MailMessage | None:
        result = await self._session.execute(
            select(MailMessage).where(
                MailMessage.account_id == account_id,
                MailMessage.provider_message_id == provider_message_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_messages(
        self,
        *,
        account_id: str | None,
        focused: bool | None,
        unread: bool | None,
        limit: int,
    ) -> Sequence[MailMessage]:
        stmt = select(MailMessage).options(selectinload(MailMessage.account)).order_by(MailMessage.received_at.desc())
        if account_id is not None:
            stmt = stmt.where(MailMessage.account_id == account_id)
        if focused is not None:
            stmt = stmt.where(MailMessage.focused.is_(focused))
        if unread is not None:
            stmt = stmt.where(MailMessage.unread.is_(unread))
        result = await self._session.execute(stmt.limit(limit))
        return result.scalars().all()

    async def create_focus_rule(self, rule: MailFocusRule) -> MailFocusRule:
        self._session.add(rule)
        await self._session.commit()
        await self._session.refresh(rule)
        return rule

    async def get_focus_rule(self, rule_id: str) -> MailFocusRule | None:
        return await self._session.get(MailFocusRule, rule_id)

    async def list_focus_rules(self, *, enabled_only: bool = False) -> Sequence[MailFocusRule]:
        stmt = select(MailFocusRule).order_by(MailFocusRule.created_at.desc())
        if enabled_only:
            stmt = stmt.where(MailFocusRule.enabled.is_(True))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def update_focus_rule(self, rule: MailFocusRule) -> MailFocusRule:
        await self._session.commit()
        await self._session.refresh(rule)
        return rule

    async def list_all_messages(self) -> Sequence[MailMessage]:
        result = await self._session.execute(select(MailMessage))
        return result.scalars().all()

    async def commit(self) -> None:
        await self._session.commit()
