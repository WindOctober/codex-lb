## ADDED Requirements

### Requirement: Proxy request logging has a typed responsibility boundary

Request-log persistence and stream preflight error projection MUST be implemented outside the proxy transport orchestration service behind an explicit typed capability boundary.

#### Scenario: Successful request-log persistence

- **WHEN** a proxy transport records a terminal request outcome
- **THEN** the runtime MUST preserve all existing account, API key, session, model, token, reasoning, transport, service-tier, latency, status, and error fields
- **AND** the persistence transaction MUST remain shielded from caller cancellation

#### Scenario: Request-log persistence failure

- **WHEN** the request-log repository raises an exception
- **THEN** the runtime MUST isolate the failure from the proxied request and retain the existing warning behavior

#### Scenario: Stream preflight failure

- **WHEN** a streaming request fails before an upstream stream is established
- **THEN** the runtime MUST record an error log with elapsed latency, requested reasoning effort, service tier, and the existing default HTTP transport

#### Scenario: Completed-request model rewrite

- **WHEN** a completed proxied request needs its recorded model replaced with the public model name
- **THEN** the runtime MUST preserve the existing bounded retry delays and fresh repository scope per attempt
- **AND** it MUST stop after the first updated row, keep blank identifiers as no-ops, and isolate persistence failures

### Requirement: Request logging remains transport-neutral

The extracted runtime MUST depend only on an explicit repository-factory capability and shared transport-neutral helpers, and MUST NOT import or dynamically inspect `app.modules.proxy.service` or WebSocket event internals.

#### Scenario: Existing service caller

- **WHEN** HTTP, HTTP bridge, or WebSocket code calls `ProxyService._write_request_log()` or `ProxyService._write_stream_preflight_error()`
- **THEN** method lookup and behavior MUST remain compatible through inheritance

#### Scenario: Session identifier normalization

- **WHEN** a caller supplies an absent, blank, or whitespace-padded session identifier
- **THEN** the persisted value MUST retain the existing `None` or stripped-string semantics from a shared transport-neutral helper
