## ADDED Requirements

### Requirement: Standalone mail inbox route

The dashboard SHALL expose Mail as a standalone route and navigation item separate from Accounts, Dashboard, API Keys, Settings, News, Scholar, and Processes.

#### Scenario: render mail route
- **WHEN** the user opens `/mail`
- **THEN** the page renders the unified mail inbox experience
- **AND** the first viewport is the usable inbox, not a landing page

### Requirement: Focused mail differentiation

The Mail page SHALL visually distinguish focused messages from other messages.

#### Scenario: focused message appears in list
- **GIVEN** a message has `focused=true`
- **WHEN** the Mail page renders the message list
- **THEN** that row includes a visible focus marker or label
- **AND** non-focused messages do not use that same marker

### Requirement: Mail focus rule management

The Mail page SHALL allow operators to create and disable focus rules without leaving the Mail route.

#### Scenario: add focus rule from Mail page
- **WHEN** the operator submits a sender, domain, or keyword focus rule
- **THEN** the Mail page refreshes focus rules and message focused state

### Requirement: Mailbox sync action

The Mail page SHALL allow operators to trigger mailbox sync for configured mail accounts without leaving the Mail route.

#### Scenario: sync mailbox from Mail page
- **WHEN** the operator clicks sync for a configured mailbox
- **THEN** the Mail page requests mailbox sync
- **AND** refreshes account sync status and message listing after success
- **AND** displays the account's last sync timestamp when it is available
