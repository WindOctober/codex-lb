## ADDED Requirements

### Requirement: Durable bridge heartbeat renewal does not block live session reuse
Ordinary reuse of a live in-memory HTTP bridge session MUST NOT wait for durable lease heartbeat database I/O while holding the global bridge session lock. The service MUST schedule the heartbeat outside the request-critical path, MUST limit each session to one in-flight heartbeat, and MUST enforce a minimum interval between heartbeat attempts that remains safely below the durable lease TTL.

#### Scenario: Repeated requests reuse one live bridge session
- **WHEN** multiple requests reuse the same live HTTP bridge session within one heartbeat interval
- **THEN** each request can acquire the session without waiting for durable heartbeat completion
- **AND** the service schedules at most one durable heartbeat for that session

#### Scenario: Heartbeat attempt fails
- **WHEN** a background durable heartbeat fails
- **THEN** the live request remains unaffected
- **AND** a later reuse may schedule another heartbeat after the minimum interval

#### Scenario: Session closes during a heartbeat
- **WHEN** a bridge session begins closing while its durable heartbeat is still in flight
- **THEN** the service settles that heartbeat before releasing the durable ownership lease
- **AND** the closing sequence does not leave the session lease renewed after release

### Requirement: Continuity metadata remains request-synchronous
Deferring ordinary durable heartbeat renewal MUST NOT defer persistence of newly observed turn-state aliases, completed response IDs, or completed input metadata used for durable continuation recovery.

#### Scenario: Upstream response completes
- **WHEN** an HTTP bridge request completes with a response ID and verifiable input metadata
- **THEN** the service persists that continuity metadata before treating the durable registration operation as complete
- **AND** ordinary background heartbeat scheduling does not replace that write
