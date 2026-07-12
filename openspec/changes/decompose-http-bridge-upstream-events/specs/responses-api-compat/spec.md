## ADDED Requirements

### Requirement: HTTP bridge upstream event handling has a typed implementation boundary

The HTTP bridge upstream receive loop and event-processing state machine MUST be implemented outside the proxy orchestration service behind an explicit typed dependency boundary.

#### Scenario: Upstream event completion

- **GIVEN** an upstream event belongs to a pending bridge request
- **WHEN** the extracted event processor receives a terminal event
- **THEN** it MUST match and remove the same request state
- **AND** it MUST emit the same normalized downstream event and invoke the same settlement capabilities

#### Scenario: Terminal state is settled before downstream completion

- **GIVEN** an upstream terminal event belongs to a pending bridge request
- **WHEN** the event processor completes that request
- **THEN** account concurrency, response-create admission, and usage state MUST be finalized before the downstream event queue is closed
- **AND** the downstream queue MUST still be closed if finalization raises

#### Scenario: Upstream disconnect or timeout

- **GIVEN** the upstream socket disconnects or exceeds its receive deadline
- **WHEN** transparent replay is not safe or is exhausted
- **THEN** pending requests MUST receive the same terminal error
- **AND** queue, admission, concurrency, and session eviction behavior MUST remain unchanged

### Requirement: Shared WebSocket event helpers have one canonical implementation

Response identification, error extraction, previous-response matching, first-event failover classification, and terminal request removal MUST have one canonical internal implementation shared by HTTP bridge and ordinary WebSocket flows.

#### Scenario: Both WebSocket paths classify the same event

- **GIVEN** the same upstream response or error event reaches the HTTP bridge and ordinary WebSocket paths
- **WHEN** each path identifies and classifies that event
- **THEN** both paths MUST use the canonical shared helpers
- **AND** they MUST produce the same response identity and failure classification
