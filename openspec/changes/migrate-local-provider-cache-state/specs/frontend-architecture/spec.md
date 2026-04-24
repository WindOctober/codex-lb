## MODIFIED Requirements

### Requirement: Accounts page
The Accounts page SHALL let operators manage OpenAI OAuth accounts and API-key upstream provider accounts from source-controlled React UI.

#### Scenario: Add API-key upstream provider
- **WHEN** an operator enters a provider name, base URL, API key, and priority
- **THEN** the app calls `POST /api/accounts/providers`
- **AND** the account list refreshes after a successful provider save

#### Scenario: Update account routing priority
- **WHEN** an operator changes an account routing priority
- **THEN** the app calls `PATCH /api/accounts/{account_id}`
- **AND** the account list and dashboard overview are invalidated after success

#### Scenario: Test account availability
- **WHEN** an operator requests an availability check for an account
- **THEN** the app calls `POST /api/accounts/{account_id}/availability`
- **AND** the UI reports the pass/fail result

### Requirement: Cache-backed news and scholar pages
The dashboard SHALL expose News and Scholar pages that render the migrated cache snapshots from fork backend APIs.

#### Scenario: Read migrated news cache
- **WHEN** the News page loads
- **THEN** it fetches `GET /api/news`
- **AND** it renders the cached summary, company items, rumor items, and stale state

#### Scenario: Read migrated scholar cache
- **WHEN** the Scholar page loads
- **THEN** it fetches `GET /api/scholar`
- **AND** it renders the cached summary, topics, papers, and stale state
