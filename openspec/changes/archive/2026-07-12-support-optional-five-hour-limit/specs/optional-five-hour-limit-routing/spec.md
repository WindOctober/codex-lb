## ADDED Requirements

### Requirement: Operator can disable five-hour usage-snapshot enforcement
The system MUST persist an operator setting that controls whether generic five-hour usage snapshots participate in account routing, and the Accounts interface MUST expose that setting.

#### Scenario: Operator enables the override
- **WHEN** an operator enables `Ignore 5h` on the Accounts page
- **THEN** the setting is persisted through the dashboard settings API
- **AND** subsequent account selections ignore generic primary-window usage snapshots

#### Scenario: Operator disables the override
- **WHEN** an operator disables `Ignore 5h`
- **THEN** subsequent account selections enforce generic primary-window usage snapshots normally

### Requirement: Five-hour override preserves independent admission controls
When five-hour usage-snapshot enforcement is disabled, the system MUST continue to enforce weekly quota, model-specific additional quota, explicit upstream rate-limit cooldowns, account status, account scope, model compatibility, and local concurrency admission.

#### Scenario: Weekly quota is exhausted
- **WHEN** five-hour enforcement is disabled
- **AND** an account's weekly usage is exhausted
- **THEN** the account remains unavailable for routing

#### Scenario: Upstream returned an explicit rate limit
- **WHEN** five-hour enforcement is disabled
- **AND** an account has a current upstream rate-limit block marker
- **THEN** the account remains rate limited until the existing recovery rules clear it

#### Scenario: Only a stale five-hour snapshot is full
- **WHEN** five-hour enforcement is disabled
- **AND** an otherwise eligible account has a full primary usage snapshot without an upstream block marker
- **THEN** that primary snapshot alone MUST NOT make the account unavailable

### Requirement: Settings updates affect routing without restart
The system MUST invalidate the cached dashboard settings after an override update so new requests observe the changed five-hour policy without restarting codex-lb.

#### Scenario: Override changes during operation
- **WHEN** the dashboard settings update succeeds
- **THEN** the settings cache is invalidated
- **AND** a later account-selection request uses the new override value
