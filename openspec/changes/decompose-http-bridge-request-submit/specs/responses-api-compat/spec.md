## ADDED Requirements

### Requirement: HTTP bridge request submission has a typed implementation boundary

HTTP bridge submit, cleanup, detach, prewarm, and replay behavior MUST be implemented outside the proxy orchestration service behind an explicit typed dependency boundary.

#### Scenario: Submit interruption cleanup

- **GIVEN** a bridge request is interrupted before or after queue insertion
- **WHEN** cleanup executes through the extracted request-submit slice
- **THEN** queue counts, pending state, response-create admission, and account concurrency leases are released exactly once

#### Scenario: Detached downstream request

- **GIVEN** a downstream consumer disconnects while its bridge request is pending
- **WHEN** the request is detached
- **THEN** its event queue is disconnected
- **AND** bridge queue, admission, account concurrency, and API-key reservation state are settled consistently

### Requirement: Request-submit decomposition preserves public behavior

Moving request-submit methods MUST NOT change queue admission, prewarm, replay safety, account rotation, durable continuity, upstream websocket use, downstream events, or error envelopes.

#### Scenario: Extracted request submission delegates without semantic changes

- **GIVEN** a request accepted by the existing HTTP bridge contract
- **WHEN** the extracted request-submit slice handles that request
- **THEN** queue admission, continuity, replay, settlement, and downstream events MUST match the pre-decomposition behavior
