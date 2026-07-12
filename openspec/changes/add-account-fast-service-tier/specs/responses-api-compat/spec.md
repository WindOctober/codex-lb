## ADDED Requirements

### Requirement: Account fast mode forwards priority tier
When a Responses-compatible request is routed to an account with fast mode enabled, the proxy MUST forward the upstream request with `service_tier=priority` unless the authenticating API key enforces another service tier. The proxy MUST NOT override explicit `flex` requests.

#### Scenario: Account fast mode promotes default tier
- **WHEN** a request without an API-key-enforced service tier is routed to an account with fast mode enabled
- **AND** the request service tier is missing, `auto`, `default`, `fast`, or `priority`
- **THEN** the upstream payload uses `service_tier=priority`

#### Scenario: API key enforced tier wins
- **WHEN** the authenticating API key enforces a service tier
- **AND** the selected account has fast mode enabled
- **THEN** the API-key-enforced service tier is preserved
