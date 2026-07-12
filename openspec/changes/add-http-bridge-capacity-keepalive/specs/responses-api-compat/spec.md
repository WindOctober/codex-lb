# Responses API Compat Delta

## MODIFIED Requirements

### Requirement: HTTP bridge streaming shall keep downstream clients connected during backend waits

When codex-lb is still processing an HTTP bridge streaming request but is waiting for session creation, account capacity, reconnect recovery, or upstream response events, it SHALL periodically emit a lightweight SSE keepalive event before the existing stream idle timeout would make the downstream appear dead.

#### Scenario: Session creation waits for capacity

- **WHEN** an HTTP bridge streaming request is waiting for a bridge session to become available
- **THEN** codex-lb SHALL emit `codex.keepalive` SSE events at bounded intervals until session creation succeeds or fails
- **AND** codex-lb SHALL preserve the eventual successful response or terminal error semantics.

#### Scenario: Submitted request waits for upstream events

- **WHEN** an HTTP bridge request has been submitted and no upstream event is available yet
- **THEN** codex-lb SHALL emit `codex.keepalive` SSE events at bounded intervals
- **AND** codex-lb SHALL stop emitting keepalives once a real upstream event is available or the request ends.

#### Scenario: Upstream has not created a response yet

- **WHEN** an HTTP bridge request has not received `response.created` and the public endpoint requested initial HTTP error propagation
- **THEN** codex-lb SHALL still emit a valid `codex.keepalive` SSE frame before the downstream stream idle timeout
- **AND** an upstream error received after the first keepalive SHALL be represented as a terminal SSE event rather than terminating the transport silently
- **AND** the existing total request and stream-idle budgets SHALL still bound the request lifetime.

#### Scenario: Immediate initial error preserves HTTP status

- **WHEN** an upstream terminal error arrives before codex-lb emits the first keepalive or response event
- **THEN** codex-lb SHALL preserve the existing initial HTTP status propagation behavior.
