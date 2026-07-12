## ADDED Requirements

### Requirement: Primary drain bypasses budget-safe routing

When selecting an account under `primary_drain`, the proxy MUST NOT apply the sticky reallocation budget threshold as a prefilter. Otherwise eligible `primary_drain` candidates MUST remain eligible even when their primary or secondary usage is above the configured sticky reallocation budget threshold.

#### Scenario: Over-threshold drain target remains eligible

- **GIVEN** `primary_drain` routing is active
- **AND** one candidate has the strongest primary-drain score but is above the configured budget threshold
- **AND** another candidate is at or below the configured budget threshold
- **WHEN** account selection runs
- **THEN** the over-threshold drain target remains eligible for primary-drain selection.

#### Scenario: Non-primary-drain strategies keep budget-safe prefilter

- **GIVEN** account selection uses a routing strategy other than `primary_drain`
- **AND** one candidate is above the configured budget threshold
- **AND** another candidate is at or below the configured budget threshold
- **WHEN** account selection runs
- **THEN** the budget-safe prefilter may prefer the candidate at or below the configured budget threshold.
