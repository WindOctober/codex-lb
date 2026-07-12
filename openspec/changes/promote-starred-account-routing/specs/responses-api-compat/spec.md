## ADDED Requirements

### Requirement: Starred accounts are globally prioritized

Account selection MUST prefer eligible starred accounts before applying the configured routing strategy. This priority MUST apply to `usage_weighted`, `capacity_weighted`, `high_waterline`, and `primary_drain`.

#### Scenario: Starred account wins before strategy routing

- **GIVEN** one eligible account is starred
- **AND** another eligible account would be selected by the configured routing strategy
- **WHEN** account selection runs
- **THEN** the starred account is selected.

#### Scenario: Unavailable starred account does not block fallback

- **GIVEN** one starred account is not eligible because it is rate-limited, paused, deactivated, quota-exceeded, or in cooldown
- **AND** another account is eligible
- **WHEN** account selection runs
- **THEN** the unavailable starred account is skipped
- **AND** selection continues with the eligible account pool.

#### Scenario: Routing strategy changes preserve starred accounts

- **GIVEN** an account is starred
- **WHEN** the configured routing strategy is changed away from `primary_drain`
- **THEN** the account remains starred.
