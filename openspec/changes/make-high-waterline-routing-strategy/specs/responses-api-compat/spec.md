## ADDED Requirements

### Requirement: High-waterline is the default routing strategy

The proxy MUST support `high_waterline` as a first-class account routing strategy and MUST use it as the default dashboard routing strategy for new settings rows. High-waterline routing MUST choose an eligible account whose remaining usage percentage is at least one percentage point above the eligible-account average after account scope, group, model, health-tier, local budget, and sticky-session constraints have been applied.

#### Scenario: High-waterline account is selected

- **GIVEN** multiple eligible accounts remain after routing constraints
- **AND** one account has a remaining usage percentage at least one percentage point above the eligible pool average
- **WHEN** account selection uses `high_waterline`
- **THEN** the proxy selects that account before applying capacity-weighted fallback.

#### Scenario: Near-even accounts fall back to capacity-weighted

- **GIVEN** no eligible account has a remaining usage percentage at least one percentage point above the eligible pool average
- **WHEN** account selection uses `high_waterline`
- **THEN** the proxy falls back to capacity-weighted selection.

### Requirement: Round-robin routing is not an operator-selectable strategy

The proxy MUST NOT expose `round_robin` as an accepted dashboard routing strategy. Existing persisted `round_robin` values MUST be normalized to a supported strategy during migration or request-time strategy resolution.

#### Scenario: Persisted round-robin is normalized

- **GIVEN** an existing dashboard settings row stores `round_robin`
- **WHEN** the routing default migration runs or the proxy resolves the setting
- **THEN** subsequent account selection uses `high_waterline`.
