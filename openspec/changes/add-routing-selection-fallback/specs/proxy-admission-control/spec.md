## MODIFIED Requirements

### Requirement: Local overload responses are explicit

When the proxy rejects a request locally because an admission lane, expensive-work stage, or local account/model concurrency budget is full, it MUST return a local-overload response with a `Retry-After` header. HTTP requests MUST use an OpenAI-style error envelope and websocket handshake denials MUST use an HTTP denial response instead of a pre-accept close frame.

#### Scenario: Account/model concurrency exhaustion is not reported as no accounts
- **WHEN** otherwise routable accounts exist
- **AND** all such accounts are excluded only because local account/model concurrency or bridge-session budgets are full
- **THEN** the proxy reports local overload instead of `no_accounts`
- **AND** the error message identifies local account/model concurrency pressure
