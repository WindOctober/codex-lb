## ADDED Requirements

### Requirement: Reset primer pre-selection

Before applying the configured routing strategy, account selection MUST prefer an otherwise eligible account whose secondary quota window has explicit 0% usage and whose secondary reset time is at least 24 hours in the future. This reset-primer pre-selection MUST apply to `usage_weighted`, `capacity_weighted`, `high_waterline`, and `primary_drain`.

#### Scenario: Fully reset account is primed before strategy routing

- **GIVEN** one eligible account has secondary usage at 0%
- **AND** its secondary reset time is at least 24 hours in the future
- **AND** another account would be selected by the configured routing strategy
- **WHEN** account selection runs
- **THEN** the fully reset account is selected first.

#### Scenario: Near-reset account is not primed

- **GIVEN** one eligible account has secondary usage at 0%
- **AND** its secondary reset time is less than 24 hours in the future
- **WHEN** account selection runs
- **THEN** the reset-primer pre-selection does not select that account only because it is at 0% usage.

#### Scenario: Recently primed account is not immediately repeated

- **GIVEN** one eligible account has secondary usage at 0%
- **AND** its secondary reset time is at least 24 hours in the future
- **AND** the same account was selected within the reset-primer cooldown window
- **WHEN** account selection runs
- **THEN** the reset-primer pre-selection does not select that account.
