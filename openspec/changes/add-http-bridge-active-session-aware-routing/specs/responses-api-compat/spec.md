## ADDED Requirements

### Requirement: HTTP bridge creation SHOULD prefer high-waterline accounts

Fresh HTTP bridge session creation without a preferred continuity account SHOULD bias account selection toward active accounts whose remaining usage percentage is above the current eligible-account average. The proxy MUST continue excluding accounts that have reached local account/model concurrency or HTTP bridge session budgets, so a high-waterline account can receive traffic up to its configured parallel capacity before other accounts are selected.

#### Scenario: Fresh prompt-cache bridge concentrates on a high-waterline account

- **GIVEN** account `acc-high` has a remaining usage percentage at least one percentage point above the eligible-account average
- **AND** account `acc-high` has not reached the local HTTP bridge account/model session budget
- **WHEN** a fresh HTTP bridge session is created without a preferred continuity account
- **THEN** account selection chooses `acc-high`
- **AND** it does not exclude `acc-high` merely because it already owns active local HTTP bridge sessions.

#### Scenario: High-waterline account falls back after local parallel capacity is full

- **GIVEN** account `acc-high` is above the eligible-account average
- **AND** account `acc-high` has reached the local HTTP bridge account/model session budget for the request model
- **WHEN** a fresh HTTP bridge session is created
- **THEN** account selection excludes `acc-high` for local capacity reasons
- **AND** the proxy may select another eligible account.

#### Scenario: Near-even accounts use the configured routing strategy

- **GIVEN** no eligible account is at least one percentage point above the remaining-usage average
- **WHEN** a fresh HTTP bridge session is created
- **THEN** account selection falls back to the configured routing strategy.

#### Scenario: Preferred continuity account is not overridden

- **GIVEN** bridge creation has a preferred continuity account
- **WHEN** account selection runs
- **THEN** high-waterline bias MUST NOT override the preferred continuity account.
