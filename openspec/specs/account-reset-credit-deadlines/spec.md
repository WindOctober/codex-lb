# account-reset-credit-deadlines Specification

## Purpose
TBD - created by archiving change show-reset-credit-deadlines. Update Purpose after archive.
## Requirements
### Requirement: Reset-credit status exposes safe deadline metadata

The system SHALL include display-safe metadata for every currently available reset credit in the account reset-credit status response and SHALL NOT expose the upstream credit identifier.

#### Scenario: Available credits include deadlines

- **WHEN** an authenticated dashboard admin requests reset-credit status and upstream returns available credits with grant and expiry timestamps
- **THEN** the response includes each credit's reset type, title, grant time, and expiry time as ISO 8601 values
- **AND** orders the credits from earliest known expiry to latest known expiry
- **AND** does not include the upstream credit ID

#### Scenario: Upstream omits deadline metadata

- **WHEN** an available upstream credit omits its grant time, expiry time, or title
- **THEN** the response preserves the available credit with nullable metadata
- **AND** keeps the aggregate available count unchanged

### Requirement: Account detail displays every reset deadline

The Accounts page SHALL display every available reset opportunity for the selected account with a clear deadline state.

#### Scenario: Admin views reset deadlines

- **WHEN** reset-credit status contains one or more credits with expiry timestamps
- **THEN** the reset panel displays each exact deadline in the browser's local timezone
- **AND** displays a readable remaining-time or expired label
- **AND** visually prioritizes the earliest deadline

#### Scenario: Credit deadline is unavailable

- **WHEN** an available reset credit has no expiry timestamp
- **THEN** the reset panel displays that opportunity with a `Deadline unavailable` state instead of hiding it

#### Scenario: Reset is consumed

- **WHEN** an admin successfully consumes a reset credit
- **THEN** the existing reset-credit query refresh removes or updates the consumed opportunity and its displayed deadline
