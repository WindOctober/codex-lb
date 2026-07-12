## ADDED Requirements

### Requirement: Primary drain prefers available flagged accounts

The proxy MUST support an account-level `primary_drain_priority_enabled` flag. When account selection uses `primary_drain` and at least one available scoped candidate account has `primary_drain_priority_enabled=true`, selection MUST prefer flagged accounts before computing primary-drain ordering. If all flagged accounts are unavailable due to rate limit, quota, cooldown, pause, or deactivation, selection MUST fall back to the normal available scoped candidate pool.
Among available flagged candidates, accounts whose primary/5h usage is at or above 100% MUST be selected before flagged candidates below 100%; after no flagged primary/5h-full candidate remains, selection MUST use the existing primary-drain ordering.

#### Scenario: Flagged account is drained first

- **GIVEN** account selection uses `primary_drain`
- **AND** one available scoped account has `primary_drain_priority_enabled=true`
- **WHEN** account selection runs
- **THEN** the flagged account is selected ahead of non-flagged accounts.

#### Scenario: Flagged full primary window is drained first

- **GIVEN** account selection uses `primary_drain`
- **AND** multiple available scoped accounts have `primary_drain_priority_enabled=true`
- **AND** one flagged account has primary/5h usage at 100%
- **AND** another flagged account has primary/5h usage below 100% with a stronger primary-drain score
- **WHEN** account selection runs
- **THEN** the flagged account at 100% primary/5h usage is selected first.

#### Scenario: Unavailable flagged account does not block fallback

- **GIVEN** account selection uses `primary_drain`
- **AND** all scoped accounts with `primary_drain_priority_enabled=true` are unavailable
- **AND** at least one non-flagged scoped account is available
- **WHEN** account selection runs
- **THEN** the available non-flagged account can be selected.

#### Scenario: No flagged account falls back to normal primary drain pool

- **GIVEN** account selection uses `primary_drain`
- **AND** no scoped candidate account has `primary_drain_priority_enabled=true`
- **WHEN** account selection runs
- **THEN** all otherwise eligible scoped accounts remain candidates.

#### Scenario: Leaving primary drain clears flags

- **GIVEN** one or more accounts have `primary_drain_priority_enabled=true`
- **WHEN** dashboard routing strategy is updated to any strategy other than `primary_drain`
- **THEN** all account drain-priority flags are cleared.
