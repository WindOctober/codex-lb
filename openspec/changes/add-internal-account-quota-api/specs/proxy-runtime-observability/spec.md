## ADDED Requirements

### Requirement: Accounts API exposes focused quota status

The dashboard API MUST expose a read-only account quota status endpoint that returns a specific account's current primary-window and secondary-window quota state. The response MUST include account identity, status, primary and secondary remaining percentages, reset timestamps, window durations, available credit fields when known, and process-local runtime occupancy.

#### Scenario: caller queries a specific account quota

- **WHEN** an authenticated dashboard client requests quota status for an existing account
- **THEN** the service returns the account identity and status
- **AND** the response includes the latest known primary-window and secondary-window quota values
- **AND** the response includes whether the account is currently occupied by active HTTP bridge work

### Requirement: Accounts API exposes account quota list

The dashboard API MUST expose a read-only account quota-list endpoint that returns account quota status entries for all accounts. Each entry MUST include process-local runtime occupancy, and an account MUST be marked occupied when it has active HTTP bridge sessions, pending requests, queued requests, or busy sessions.

#### Scenario: caller lists account quota candidates

- **WHEN** an authenticated dashboard client requests the account quota list
- **THEN** the service returns a list of account quota status payloads
- **AND** occupied and unoccupied accounts both remain in the list
- **AND** each account has a runtime occupancy flag and counters so the caller can choose an account using its own policy
