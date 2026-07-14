## ADDED Requirements

### Requirement: Codex alpha search is proxied through managed accounts

The service MUST accept `POST /backend-api/codex/alpha/search`, authenticate the LB caller, select an eligible managed Codex account, replace caller authorization with that account's upstream credentials, and return the upstream JSON search result.

#### Scenario: Search succeeds
- **WHEN** an authenticated Codex client submits a valid alpha search request
- **THEN** the service posts the request to the selected account's Codex `alpha/search` upstream endpoint
- **AND** returns the upstream `output` and optional `encrypted_output` fields as JSON

#### Scenario: Caller authentication is required
- **WHEN** proxy API-key authentication is enabled and the request has no valid LB API key
- **THEN** the service rejects the request without selecting an account or contacting the upstream

#### Scenario: Upstream credentials are isolated
- **WHEN** the service forwards an authenticated search request
- **THEN** the inbound LB authorization value is not sent upstream
- **AND** the selected account access token and account header are used instead

### Requirement: Alpha search honors routing and model policy

The service MUST enforce API-key model access and account/group assignment restrictions, MUST use the search request ID as the prompt-cache affinity shared with the parent Codex Responses turn, and MUST refresh selected account credentials before forwarding.

#### Scenario: Existing session affinity is reused
- **WHEN** the search request ID has an eligible prompt-cache account binding from a Codex Responses turn
- **THEN** the service prefers that account subject to current routing eligibility and API-key restrictions

#### Scenario: Model is not allowed
- **WHEN** the LB API key does not allow the requested search model
- **THEN** the service rejects the request before contacting the upstream

#### Scenario: Selected credentials are stale
- **WHEN** the selected account token requires refresh
- **THEN** the service refreshes the token within the request budget before forwarding the search

### Requirement: Alpha search failures remain bounded and structured

The service MUST return normalized JSON errors, MUST NOT retry account-neutral client failures, and MAY fail over once to another eligible account for an account-scoped or transient upstream failure.

#### Scenario: Account is rate limited
- **WHEN** the selected account receives an upstream rate-limit response before a search result is returned
- **THEN** the service records the account failure and attempts one other eligible account within the request budget

#### Scenario: Upstream rejects the request body
- **WHEN** the upstream returns an account-neutral invalid-request error
- **THEN** the service returns that error status and envelope without trying another account

#### Scenario: No account is eligible
- **WHEN** no account satisfies current model, API-key, routing, and health constraints
- **THEN** the service returns the existing structured no-account or recoverable-capacity error
