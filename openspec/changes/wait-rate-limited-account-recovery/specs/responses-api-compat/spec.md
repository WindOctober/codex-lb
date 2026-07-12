## MODIFIED Requirements

### Requirement: HTTP bridge streaming shall keep downstream clients connected during backend waits

When codex-lb is still processing an HTTP bridge streaming request but is waiting for session creation, account capacity, recoverable account rate-limit recovery, reconnect recovery, or upstream response events, it SHALL periodically emit a lightweight SSE keepalive event before the existing stream idle timeout would make the downstream appear dead.

#### Scenario: Session creation waits for recoverable account rate-limit recovery

- **WHEN** an HTTP bridge streaming request cannot immediately select an account because all eligible accounts are temporarily rate-limited or cooling down with a known recovery time
- **THEN** codex-lb SHALL keep waiting within the existing request budget
- **AND** codex-lb SHALL emit `codex.keepalive` SSE events at bounded intervals until selection succeeds or a terminal error occurs.
