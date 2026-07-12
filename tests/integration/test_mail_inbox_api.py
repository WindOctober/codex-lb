from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.crypto import TokenEncryptor
from app.db.models import MailAccount
from app.db.session import SessionLocal
from app.modules.mail_inbox.imap_sync import ImapFetchedMessage


@pytest.mark.asyncio
async def test_mail_account_create_hides_credential(async_client):
    response = await async_client.post(
        "/api/mail/accounts",
        json={
            "provider": "imap",
            "address": "Betsun2908@163.com",
            "displayName": "163 support mailbox",
            "imapHost": "imap.163.com",
            "imapPort": 993,
            "imapUsername": "betsun2908@163.com",
            "credential": "imap-app-password",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["address"] == "betsun2908@163.com"
    assert payload["provider"] == "imap"
    assert payload["syncStatus"] == "never_synced"
    assert "credential" not in payload

    async with SessionLocal() as session:
        account = await session.scalar(select(MailAccount).where(MailAccount.id == payload["id"]))
        assert account is not None
        assert account.credential_encrypted is not None
        assert TokenEncryptor().decrypt(account.credential_encrypted) == "imap-app-password"


@pytest.mark.asyncio
async def test_mail_messages_focus_filters_and_rule_disable(async_client):
    account_response = await async_client.post(
        "/api/mail/accounts",
        json={
            "provider": "gmail",
            "address": "alerts@example.com",
            "displayName": "Alerts",
        },
    )
    assert account_response.status_code == 200
    account_id = account_response.json()["id"]

    rule_response = await async_client.post(
        "/api/mail/focus-rules",
        json={"kind": "sender_domain", "value": "openai.com", "label": "OpenAI"},
    )
    assert rule_response.status_code == 200
    rule_id = rule_response.json()["id"]

    focused_message = await async_client.post(
        "/api/mail/messages",
        json={
            "accountId": account_id,
            "providerMessageId": "msg-openai",
            "threadId": "thread-1",
            "senderEmail": "support@openai.com",
            "senderName": "OpenAI Support",
            "recipients": ["alerts@example.com"],
            "subject": "Case update",
            "snippet": "Your case has an update",
            "receivedAt": datetime(2026, 6, 19, 8, 0, tzinfo=UTC).isoformat(),
            "unread": True,
            "starred": False,
            "hasAttachments": False,
        },
    )
    assert focused_message.status_code == 200
    assert focused_message.json()["focused"] is True
    assert focused_message.json()["focusLabel"] == "OpenAI"

    plain_message = await async_client.post(
        "/api/mail/messages",
        json={
            "accountId": account_id,
            "providerMessageId": "msg-newsletter",
            "senderEmail": "newsletter@example.net",
            "subject": "Weekly digest",
            "snippet": "Nothing urgent",
            "receivedAt": datetime(2026, 6, 19, 7, 0, tzinfo=UTC).isoformat(),
            "unread": False,
            "starred": False,
            "hasAttachments": False,
        },
    )
    assert plain_message.status_code == 200
    assert plain_message.json()["focused"] is False

    focused_list = await async_client.get("/api/mail/messages", params={"focused": "true"})
    assert focused_list.status_code == 200
    assert [message["providerMessageId"] for message in focused_list.json()["messages"]] == ["msg-openai"]

    unread_list = await async_client.get("/api/mail/messages", params={"unread": "true"})
    assert unread_list.status_code == 200
    assert [message["providerMessageId"] for message in unread_list.json()["messages"]] == ["msg-openai"]

    disable_response = await async_client.patch(f"/api/mail/focus-rules/{rule_id}", json={"enabled": False})
    assert disable_response.status_code == 200
    assert disable_response.json()["enabled"] is False

    focused_after_disable = await async_client.get("/api/mail/messages", params={"focused": "true"})
    assert focused_after_disable.status_code == 200
    assert focused_after_disable.json()["messages"] == []


@pytest.mark.asyncio
async def test_mail_account_sync_imports_imap_messages(async_client, monkeypatch):
    def fake_fetch(settings):
        assert settings.host == "imap.gmail.com"
        assert settings.username == "alerts@example.com"
        assert settings.password == "imap-app-password"
        return [
            ImapFetchedMessage(
                provider_message_id="<case-1@example.com>",
                sender_email="support@openai.com",
                sender_name="OpenAI Support",
                recipients=["alerts@example.com"],
                subject="Case update",
                snippet="Your case has an update",
                received_at=datetime(2026, 6, 19, 8, 0),
                unread=True,
                starred=False,
            )
        ]

    monkeypatch.setattr("app.modules.mail_inbox.service.fetch_imap_messages", fake_fetch)

    account_response = await async_client.post(
        "/api/mail/accounts",
        json={
            "provider": "gmail",
            "address": "alerts@example.com",
            "credential": "imap-app-password",
        },
    )
    assert account_response.status_code == 200
    account_id = account_response.json()["id"]

    rule_response = await async_client.post(
        "/api/mail/focus-rules",
        json={"kind": "sender_domain", "value": "openai.com", "label": "OpenAI"},
    )
    assert rule_response.status_code == 200

    sync_response = await async_client.post(f"/api/mail/accounts/{account_id}/sync")
    assert sync_response.status_code == 200
    sync_payload = sync_response.json()
    assert sync_payload["importedCount"] == 1
    assert sync_payload["account"]["syncStatus"] == "synced"
    assert sync_payload["account"]["lastSyncAt"] is not None
    assert "credential" not in sync_payload["account"]

    focused_list = await async_client.get("/api/mail/messages", params={"focused": "true"})
    assert focused_list.status_code == 200
    messages = focused_list.json()["messages"]
    assert [message["providerMessageId"] for message in messages] == ["<case-1@example.com>"]
    assert messages[0]["focusLabel"] == "OpenAI"
