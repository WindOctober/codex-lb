## ADDED Requirements

### Requirement: Mail inbox accounts

The system SHALL provide dashboard APIs to create, list, update, and disable mail inbox account records independently from OpenAI account records.

#### Scenario: list configured mail accounts
- **WHEN** the dashboard requests mail accounts
- **THEN** the response includes each account address, provider kind, enabled state, sync status, and last sync timestamp
- **AND** encrypted credential material is not returned

#### Scenario: add IMAP account
- **WHEN** the dashboard creates an IMAP mail account with host, port, username, and credential
- **THEN** the system persists the account with encrypted credential material
- **AND** the response excludes the credential value

#### Scenario: sync IMAP-capable account
- **GIVEN** a configured and enabled 163, Gmail, Outlook, or custom IMAP mail account has credential material
- **WHEN** the dashboard requests a mailbox sync
- **THEN** the system fetches recent message metadata through IMAP
- **AND** stores imported messages in the unified inbox without returning credential material
- **AND** updates the account sync status, last sync timestamp, and last sync error

### Requirement: Unified message listing

The system SHALL expose a unified read-only mail message listing across configured mail accounts.

#### Scenario: list all messages
- **WHEN** the dashboard requests mail messages without filters
- **THEN** messages from all enabled mail accounts are returned in descending received-time order

#### Scenario: list focused messages
- **WHEN** the dashboard requests mail messages with `focused=true`
- **THEN** only messages that match enabled focus rules are returned

#### Scenario: list unread messages
- **WHEN** the dashboard requests mail messages with `unread=true`
- **THEN** only unread messages are returned

### Requirement: Focus rules

The system SHALL allow operators to manage focus rules for sender emails, sender domains, and keywords.

#### Scenario: sender email rule marks message focused
- **GIVEN** an enabled sender-email focus rule exists
- **WHEN** a message from that exact sender email is stored
- **THEN** the message is marked focused
- **AND** the message records the matching rule label when one is configured

#### Scenario: disabling a focus rule updates matching state
- **WHEN** a focus rule is disabled
- **THEN** messages previously matching only that rule are no longer returned by the focused filter

### Requirement: Read-only first version

The mail inbox module SHALL NOT send email or expose SMTP send APIs.

#### Scenario: no send endpoint
- **WHEN** the mail inbox router is registered
- **THEN** it exposes read/configuration endpoints only
- **AND** no endpoint accepts a mail-send payload
