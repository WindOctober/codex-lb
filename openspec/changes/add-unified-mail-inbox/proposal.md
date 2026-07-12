## Why

Operators need a single read-only mail surface for several personal inboxes used around account operations, support cases, subscription notices, and security alerts. Checking 163, Gmail, and Outlook separately makes it easy to miss messages from high-priority senders.

## What Changes

- Add a standalone Mail Inbox module and dashboard section, separate from proxy, account routing, and API-key management.
- Store mail account connection records, normalized message metadata, and focus rules for senders/domains/keywords.
- Provide dashboard APIs to list accounts, list messages, manage focus rules, and inspect sync status.
- Render a unified inbox UI with Focused, Unread, provider/account filters, and visual differentiation for focused messages.

## Non-Goals

- Sending mail.
- Acting as an SMTP server.
- Background live push notifications in the first version.
- Persisting full message bodies by default.
- Public multi-user hosted mail service behavior.

## Impact

- Specs: `mail-inbox`, `frontend-architecture`
- Code: `app/db/models.py`, Alembic migrations, `app/modules/mail_inbox/*`, `app/dependencies.py`, `app/main.py`, dashboard frontend routing and feature code.
- Tests: API integration tests, schema tests, focused-message UI coverage.
