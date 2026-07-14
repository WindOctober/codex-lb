## ADDED Requirements

### Requirement: HTTP bridge startup failure respects stream commitment

The HTTP bridge MUST preserve an upstream startup failure as an HTTP error when no downstream event has been emitted, and MUST emit a terminal `response.failed` SSE event instead of raising through the ASGI server when a keepalive has already committed the stream.

#### Scenario: Startup fails before stream commitment
- **WHEN** bridge session acquisition fails before any downstream event is emitted
- **THEN** the original proxy error status and error envelope remain available to the API response layer

#### Scenario: Startup fails after keepalive
- **WHEN** the bridge emits a startup keepalive and session acquisition later fails
- **THEN** the stream emits one terminal `response.failed` event carrying the normalized failure detail
- **AND** the failure does not escape as an ASGI application exception

#### Scenario: Downstream abandons startup wait
- **WHEN** the downstream closes after a startup keepalive while session acquisition is still pending
- **THEN** the bridge cancels and settles its owned acquisition task
- **AND** a later task failure does not appear as an unretrieved exception
