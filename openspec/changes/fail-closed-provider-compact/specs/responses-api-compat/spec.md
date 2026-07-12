## ADDED Requirements

### Requirement: Provider compact fails closed without streamed Responses surrogate

When a compact request selects an upstream provider that does not support the
Codex compact wire contract, the service MUST return an explicit compact
unsupported error instead of forwarding the request through normal streamed
Responses or synthesizing a compact-shaped response from normal Responses
output.

#### Scenario: Provider responses wire API does not implement compact

- **GIVEN** a selected API-key upstream provider uses the `responses` wire API
- **WHEN** the proxy handles `/v1/responses/compact` or `/backend-api/codex/responses/compact`
- **THEN** the proxy returns a not-implemented error
- **AND** it does not call the streamed Responses transport as a compact surrogate
- **AND** it does not penalize the selected account for the provider capability mismatch.
