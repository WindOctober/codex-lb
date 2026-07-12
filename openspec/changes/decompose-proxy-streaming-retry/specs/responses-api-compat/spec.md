## ADDED Requirements

### Requirement: Ordinary Responses streaming has a typed implementation boundary

Ordinary HTTP Responses streaming retry orchestration and one-attempt SSE processing MUST be implemented outside the proxy orchestration service behind an explicit typed capability boundary.

#### Scenario: Successful stream

- **GIVEN** an eligible account and valid streamed Responses request
- **WHEN** ordinary streaming handles the request
- **THEN** it MUST emit the same upstream SSE events in order
- **AND** it MUST preserve request logging, API-key settlement, account health, latency, and service-tier behavior

#### Scenario: Transient same-account retry

- **GIVEN** an attempt fails with an eligible transient upstream error before a successful terminal response
- **WHEN** the same-account retry budget remains
- **THEN** streaming MUST retry the same account with the same model and bounded timeout behavior

#### Scenario: Account failover

- **GIVEN** the selected account reaches a retryable rate, quota, authentication, or exhausted transient condition
- **WHEN** another account supports the requested model
- **THEN** streaming MUST exclude the failed account and select another account
- **AND** it MUST NOT substitute another model

### Requirement: Streaming test seams remain explicit

Settings providers and the upstream SSE stream factory required by focused tests MUST be represented by explicit service adapters. The extracted module MUST NOT import or dynamically inspect `app.modules.proxy.service`.

#### Scenario: Streaming integration replaces a declared adapter

- **GIVEN** a focused integration replaces runtime settings or the upstream SSE factory
- **WHEN** ordinary streaming executes
- **THEN** it MUST consume the replacement through a declared service adapter
- **AND** the extracted module MUST NOT inspect the proxy service module
