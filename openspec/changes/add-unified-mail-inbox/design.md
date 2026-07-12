## Design

The feature is a new read-only module named `mail_inbox`. It owns its persistence, API schemas, and frontend route. Existing proxy/account modules do not depend on it.

## Data Model

- `mail_accounts`: configured mailbox connections.
  - Provider kind: `gmail`, `outlook`, or `imap`.
  - Address, display name, enabled flag, sync status, last sync time, and encrypted credential fields.
- `mail_messages`: normalized message metadata.
  - Account reference, provider message ID, thread ID, sender/recipients, subject, snippet, received time, unread/starred/attachment flags, and focus match state.
- `mail_focus_rules`: user-managed rules.
  - Rule kind: sender email, sender domain, or keyword.
  - Enabled flag and optional label.

## Sync Strategy

The first implementation provides the persistence and dashboard APIs plus a manual metadata ingestion path for tests and future sync workers. Provider-specific workers are intentionally behind the same repository/service boundary:

- Gmail: Gmail API OAuth incremental sync.
- Outlook: Microsoft Graph mail delta query.
- IMAP/163: IMAP UID-based polling using an app password/authorization code.

## Focus Evaluation

Focused state is derived when a message is stored or when focus rules change. A message is focused if an enabled rule matches:

- sender email exactly,
- sender domain exactly,
- or case-insensitive keyword in subject or snippet.

## Security

- Credentials must be encrypted before persistence.
- Dashboard APIs use existing dashboard authentication.
- Message bodies are not persisted by default.
- Send-mail functionality is out of scope.

## UI

The Mail route is an operational inbox, not a marketing page. It uses a dense list/detail layout:

- Sidebar filters: Focused, All, Unread, and account/provider facets.
- Message rows show focused messages with a distinct marker and label.
- Rules management is available from the same route.
