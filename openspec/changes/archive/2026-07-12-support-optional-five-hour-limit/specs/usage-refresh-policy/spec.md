## ADDED Requirements

### Requirement: Weekly-only usage refresh supersedes stale primary state
After a successful generic usage refresh reports a weekly window but no distinct five-hour window, the system MUST treat the weekly-only shape as authoritative for that account and MUST NOT continue using an older generic primary row as current state.

#### Scenario: Weekly-only payload follows an old full primary snapshot
- **WHEN** an account has historical generic primary usage
- **AND** a successful upstream refresh contains only a 604800-second generic window
- **THEN** the refreshed account exposes the window as weekly usage
- **AND** the historical primary usage no longer participates in current dashboard status or routing

#### Scenario: Primary window returns later
- **WHEN** a later successful upstream refresh contains a distinct primary window
- **THEN** the system records and enforces that new primary window according to the current operator setting

#### Scenario: Usage refresh fails
- **WHEN** the upstream usage refresh fails or returns no authoritative generic rate-limit object
- **THEN** the system MUST NOT retire existing primary usage solely because of that failure or absence
