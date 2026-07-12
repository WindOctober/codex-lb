## ADDED Requirements

### Requirement: Account reset credits are visible per account

The system SHALL expose the number of earned Codex rate-limit reset credits currently available for an OpenAI OAuth account.

#### Scenario: Admin reads reset credits

- **WHEN** an authenticated dashboard admin requests reset-credit status for an account
- **THEN** the response includes `availableCount`
- **AND** the account access token is refreshed before retrying a 401 usage request

### Requirement: Account reset credit consumption is explicit

The system SHALL allow an authenticated dashboard admin to consume one earned Codex rate-limit reset credit for a selected OpenAI OAuth account only through an explicit account-level action.

#### Scenario: Admin consumes one reset

- **WHEN** an authenticated dashboard admin confirms reset consumption for an account with an available reset
- **THEN** the system calls the upstream reset-credit consume endpoint with an idempotency key
- **AND** returns the upstream outcome
- **AND** refreshes stored usage state after a successful reset or idempotent success

#### Scenario: No reset is consumed

- **WHEN** upstream reports `no_credit` or `nothing_to_reset`
- **THEN** the response preserves that outcome
- **AND** the dashboard does not report a successful reset
