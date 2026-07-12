## ADDED Requirements

### Requirement: Primary drain routing is deterministic

The proxy MUST support `primary_drain` as a first-class account routing strategy. After account scope, group, source, model, health-tier, sticky-session, and hard availability constraints have been applied, `primary_drain` MUST choose candidates by deterministic ordering instead of probability.

#### Scenario: Account with stronger primary drain signal is selected

- **GIVEN** multiple eligible accounts remain after routing constraints
- **AND** one account has a stronger recent primary-window drain signal than its peers
- **WHEN** account selection uses `primary_drain`
- **THEN** the proxy selects that account deterministically.

#### Scenario: Capacity weighted fallback is used when there is no drain signal

- **GIVEN** eligible accounts do not have a meaningful recent primary-window drain signal
- **WHEN** account selection uses `primary_drain`
- **THEN** the proxy falls back to capacity-weighted selection.

#### Scenario: Primary drain does not prefilter near-exhausted candidates

- **GIVEN** an otherwise eligible account is above the dashboard sticky reallocation budget threshold
- **AND** account selection uses `primary_drain`
- **WHEN** another eligible account is below the threshold
- **THEN** the proxy MUST NOT exclude the above-threshold account solely because of that budget threshold before applying `primary_drain` ordering.
