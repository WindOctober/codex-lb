## MODIFIED Requirements

### Requirement: HTTP bridge safely fails over before request submission

HTTP bridge startup MUST use another eligible account for a transient connection failure when no upstream request has been submitted and both candidate and request budgets remain.

#### Scenario: Ordinary account handshake timeout
- **WHEN** an ordinary selected account fails to establish its HTTP bridge WebSocket before `response.create` is sent
- **AND** another eligible account and request budget remain
- **THEN** the failed account MUST be excluded for this request
- **AND** bridge startup MUST attempt another eligible account

#### Scenario: Retry bounds
- **WHEN** transient pre-send connection failures continue
- **THEN** retries MUST stop at the existing account-attempt limit or outer request deadline
- **AND** the final error MUST describe the connection failure unless the outer deadline actually expired

#### Scenario: Mixed connection failures
- **WHEN** successive account attempts fail with different connection error forms
- **AND** no eligible candidate remains
- **THEN** bridge startup MUST surface the final attempted connection's failure
- **AND** it MUST NOT surface a stale error from an earlier account attempt

#### Scenario: Required continuity account fails
- **WHEN** a bridge request requires a preferred account because its upstream response state cannot safely move
- **AND** the bounded same-account retry also fails
- **THEN** bridge startup MUST surface the connection failure without rebinding the request to another account

#### Scenario: Unpublished bridge session capacity is released
- **WHEN** initial bridge creation or reconnect acquires an account-model session lease
- **AND** credential refresh or upstream connection exits through cancellation or an unexpected exception before the lease transfers to a published session
- **THEN** the acquired session lease MUST be released exactly once
- **AND** the original cancellation or exception MUST propagate unchanged

#### Scenario: Ambiguous direct WebSocket send is not replayed
- **WHEN** a direct Responses WebSocket raises after its send operation has started
- **AND** the service cannot prove that the request was not accepted upstream
- **THEN** the service MUST fail that pending request and close the ambiguous upstream
- **AND** it MUST NOT submit the same logical request on a replacement WebSocket

#### Scenario: Direct WebSocket replay preserves its deadline
- **WHEN** a direct Responses WebSocket request is safely replayable before submission
- **THEN** the replay MUST retain the request's original absolute deadline
- **AND** reconnect or replay MUST NOT grant a fresh request budget
