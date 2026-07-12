## ADDED Requirements

### Requirement: Response-create payload policy has one canonical implementation

The proxy MUST implement shared `response.create` historical slimming, payload-size error construction, image capability detection, and payload summarization through one canonical core policy boundary. HTTP bridge, ordinary streaming, and WebSocket transports MUST preserve their existing externally observable payload and error behavior when consuming that boundary.

#### Scenario: Oversized historical payload can be slimmed

- **WHEN** a `response.create` payload exceeds the upstream WebSocket limit and historical input contains eligible inline images or large tool output
- **THEN** every transport MUST apply the same recent-user suffix preservation and omission notices
- **AND** the latest user turn and subsequent input MUST remain unchanged

#### Scenario: Payload remains oversized

- **WHEN** the effective payload remains above the configured maximum after eligible slimming
- **THEN** the proxy MUST return the existing 413 `payload_too_large` error contract
- **AND** service-level diagnostics MUST preserve the existing dump naming, metadata, and redaction behavior

#### Scenario: Image capabilities are detected

- **WHEN** a Responses request contains an input-image part or an image-generation tool
- **THEN** the canonical policy MUST report the same capability result used by current transport selection and account checks

### Requirement: Legacy payload-policy seams remain explicit

The service and core upstream client MUST retain explicit threshold injection points and compatible helper exports while using the canonical implementation. Extracted policy modules MUST NOT import or dynamically inspect `app.modules.proxy.service`.

#### Scenario: Transport-specific threshold replacement

- **WHEN** a focused test or local integration replaces a legacy module's warning, maximum, or dump-directory value
- **THEN** requests through that module MUST observe the replacement
- **AND** the other transport module's thresholds MUST remain independent
