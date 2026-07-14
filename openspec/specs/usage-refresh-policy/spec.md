# usage-refresh-policy Specification

## Purpose
Define how background usage refresh reacts to auth-like failures without permanently hammering bad accounts.
## Requirements
### Requirement: Usage refresh cools down repeated auth-like failures

Background usage refresh MUST apply a cooldown to accounts that repeatedly fail usage refresh with ambiguous `401` or `403` responses. Accounts in that cooldown window MUST be skipped until the cooldown expires or a later successful refresh clears it.

#### Scenario: Ambiguous usage 401 enters cooldown
- **WHEN** usage refresh receives a `401` that does not match a permanent deactivation signal
- **THEN** the account is not deactivated immediately
- **AND** subsequent refresh cycles skip the account until the cooldown window expires

#### Scenario: Successful refresh clears cooldown
- **WHEN** a later usage refresh succeeds for an account that had been cooled down
- **THEN** the cooldown is cleared
- **AND** normal refresh cadence resumes

### Requirement: Usage refresh deactivates on clear deactivation signals

The system MUST deactivate accounts when usage refresh receives a permanent deactivation signal. At minimum, `402`, `404`, and `401` responses whose message explicitly indicates that the OpenAI account has been deactivated MUST be treated as deactivation signals.

#### Scenario: Usage 401 deactivation message deactivates the account
- **WHEN** usage refresh receives HTTP `401`
- **AND** the upstream message states that the OpenAI account has been deactivated
- **THEN** the account is marked `deactivated`
- **AND** later usage refresh cycles skip that account

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

#### Scenario: Weekly replacement persistence fails
- **WHEN** a weekly-only refresh needs to replace an older primary snapshot
- **AND** persistence of the new weekly snapshot fails
- **THEN** the older primary snapshot MUST remain available as the last known quota state
- **AND** retirement of the older snapshot MUST occur only after replacement persistence succeeds
