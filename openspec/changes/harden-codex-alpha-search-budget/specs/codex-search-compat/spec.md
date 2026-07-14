## MODIFIED Requirements

### Requirement: Alpha search failures remain bounded and structured

The service MUST return normalized JSON errors, MUST NOT retry account-neutral client failures, and MAY fail over once to another eligible account for an account-scoped or transient upstream failure. Alpha search MUST use a dedicated finite total request budget, and each upstream account attempt MUST be capped below that total so a stalled first attempt can leave time for failover.

The configured per-account attempt timeout MUST be strictly less than the total search budget. Initial credential freshness, forced refresh, admission or singleflight waiting, every same-account upstream retry, failure accounting, and final request logging MUST share the original total search deadline.

#### Scenario: Account is rate limited
- **WHEN** the selected account receives an upstream rate-limit response before a search result is returned
- **THEN** the service records the account failure and attempts one other eligible account within the dedicated search request budget

#### Scenario: Upstream rejects the request body
- **WHEN** the upstream returns an account-neutral invalid-request error
- **THEN** the service returns that error status and envelope without trying another account

#### Scenario: No account is eligible
- **WHEN** no account satisfies current model, API-key, routing, and health constraints
- **THEN** the service returns the existing structured no-account or recoverable-capacity error

#### Scenario: Incompatible providers do not consume attempts
- **WHEN** the eligible model pool contains native Responses providers and at least one Codex search provider
- **THEN** the service excludes providers without the Codex search wire capability before sticky resolution
- **AND** those incompatible providers do not consume either bounded search attempt

#### Scenario: Parent binding lacks search capability
- **WHEN** the versioned parent prompt-cache binding points to a provider without Codex search wire capability
- **THEN** the service treats that binding as ineligible for search
- **AND** it selects or rebinds to a capable account without forwarding search to the incompatible provider

#### Scenario: Long search outlives the general proxy budget
- **WHEN** an alpha search remains active beyond the general proxy request budget
- **AND** its dedicated total and current per-account attempt budgets remain valid
- **THEN** the service MUST keep the search active
- **AND** it MUST NOT emit `Proxy request budget exhausted` solely because the general budget elapsed

#### Scenario: First account attempt stalls
- **WHEN** the first upstream account attempt reaches its per-account timeout
- **AND** another eligible account and total search budget remain
- **THEN** the service MUST record the first account failure
- **AND** it MUST attempt the search through one other eligible account

#### Scenario: Account credentials are refreshed after unauthorized search
- **WHEN** an upstream search returns unauthorized and the service refreshes that account's credentials
- **THEN** the forced refresh and same-account retry MUST remain within the account's original attempt deadline
- **AND** credential refresh MUST NOT restart the per-account timeout window

#### Scenario: Both bounded attempts fail
- **WHEN** both permitted upstream account attempts fail before the total search deadline
- **THEN** the service MUST return the final attempted upstream failure
- **AND** it MUST NOT replace that failure with a stale or generic budget error

#### Scenario: Total search deadline expires
- **WHEN** the dedicated total search budget is exhausted before another bounded operation can begin
- **THEN** the service MUST terminate with the stable proxy budget error
- **AND** dashboard lookup and each upstream attempt MUST use hard observation timeouts
- **AND** account selection MUST use the same hard observation boundary even when settings or persistence suppresses cancellation
- **AND** a cancellation-suppressing late result MUST remain tracked and MUST NOT replace timeout handling or different-account failover

#### Scenario: Failure accounting cannot exceed the search deadline
- **WHEN** an upstream search attempt fails near the total search deadline
- **THEN** failure classification MUST complete synchronously
- **AND** health accounting MUST run as tracked background cleanup bounded by the remaining total budget
- **AND** eligible different-account failover MUST NOT wait for health accounting
- **AND** blocked accounting MUST NOT keep the request alive beyond that deadline
- **AND** accounting that suppresses cancellation MUST remain tracked without extending the request deadline

#### Scenario: Final logging cannot delay a successful search
- **WHEN** upstream search succeeds with little or no total budget remaining
- **THEN** final request logging MUST be best effort within the remaining deadline
- **AND** blocked logging MUST NOT replace or delay the successful response
- **AND** cancellation-suppressing logging MUST be hard-observed and retained in tracked cleanup after the deadline

#### Scenario: Search shutdown has a hard bookkeeping bound
- **WHEN** tracked search accounting or logging suppresses cancellation during shutdown
- **THEN** shutdown MUST stop waiting after bounded initial and post-cancellation observation intervals
- **AND** any unfinished task MUST remain tracked and the hard timeout MUST be logged

#### Scenario: Search transport errors are sanitized
- **WHEN** credential refresh, its structured or non-JSON response body, or an upstream search request contains internal network or filesystem details
- **THEN** the public search error MUST use stable bounded text
- **AND** sanitization MUST apply after the real search client converts HTTP and native transport failures
- **AND** raw exception detail MUST be logged server-side only
