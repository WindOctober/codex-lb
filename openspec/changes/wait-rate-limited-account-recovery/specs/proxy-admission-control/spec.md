## MODIFIED Requirements

### Requirement: Recoverable account exhaustion waits within request budgets

When no account is immediately selectable because every otherwise eligible account is temporarily rate-limited, quota-limited, or cooling down with a known recovery time, the proxy MUST treat the selection result as recoverable until the existing request budget expires. The proxy MUST NOT wait for permanently unavailable states such as paused, deactivated, missing model support, or unknown recovery time.

#### Scenario: All eligible accounts have known recovery times

- **WHEN** every eligible account is temporarily unavailable due to a rate limit, quota reset, or cooldown
- **AND** at least one unavailable account has a known future recovery time
- **THEN** account selection reports a retry-after duration instead of a terminal `no_accounts` condition
- **AND** callers may wait within their existing request budgets for a later selection attempt.

#### Scenario: Permanent unavailability still fails fast

- **WHEN** every matching account is paused, deactivated, unsupported, or unavailable without a known recovery time
- **THEN** account selection reports a terminal failure
- **AND** the proxy does not hold the request open solely for account recovery.
