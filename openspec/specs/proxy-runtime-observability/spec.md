# proxy-runtime-observability Specification

## Purpose

See context docs for background.
## Requirements
### Requirement: Proxy 4xx/5xx responses are logged with error detail
When the proxy returns a 4xx or 5xx response for a proxied request, the system MUST log the request id, method, path, status code, error code, and error message to the console. For local admission rejections, the log MUST also include the rejection stage or lane.

#### Scenario: Local admission rejection is logged
- **WHEN** the proxy rejects a request locally because a downstream or expensive-work admission lane is full
- **THEN** the console log includes the local response status, normalized error code and message
- **AND** it includes which admission lane or stage rejected the request

### Requirement: Expected bridge startup failures have one terminal signal

An HTTP bridge startup failure MUST be reported through the request's applicable HTTP or SSE error channel without also producing a shield-future warning, unretrieved-task warning, or duplicate ASGI exception for the same failure.

#### Scenario: Failure completes during keepalive polling
- **WHEN** the session-acquisition task fails while the bridge is polling it for keepalive delivery
- **THEN** the task exception is consumed by the request stream or its cleanup owner exactly once
- **AND** the event loop does not report an exception in a shielded future

#### Scenario: Post-commit failure is observable
- **WHEN** the downstream stream has already committed and bridge startup fails
- **THEN** the terminal SSE error remains visible to the client
- **AND** normal bridge diagnostics identify the startup failure without an ASGI traceback
