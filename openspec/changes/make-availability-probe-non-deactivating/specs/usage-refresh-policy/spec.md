# usage-refresh-policy Delta

## ADDED Requirements
### Requirement: Manual availability probes do not deactivate accounts on refresh failure

Manual account availability probes MUST NOT persist `deactivated` solely because a forced OAuth token refresh fails. The probe MAY report the account as failing the check while preserving its previous account status and deactivation reason.

#### Scenario: Permanent refresh error during manual probe
- **WHEN** an operator runs a manual availability probe for an active OAuth account
- **AND** the forced token refresh returns a permanent refresh-token error
- **THEN** the probe reports a failed check
- **AND** the account remains `active`
- **AND** no deactivation reason is written

#### Scenario: Normal refresh still deactivates on permanent refresh error
- **WHEN** a non-probe token refresh path receives a permanent refresh-token error
- **THEN** the account is marked `deactivated`
- **AND** the deactivation reason is persisted
