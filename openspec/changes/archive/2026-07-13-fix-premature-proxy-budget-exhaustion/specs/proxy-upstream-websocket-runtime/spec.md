## MODIFIED Requirements

### Requirement: WebSocket connection budget and admission cleanup are preserved

The extracted runtime MUST preserve outer request-budget enforcement, nested connection-timeout semantics, local admission ordering, and exactly-once release of the acquired connection lease on every terminal factory path.

#### Scenario: Successful creation
- **WHEN** connection admission succeeds and the socket factory returns an upstream WebSocket
- **THEN** the runtime MUST return that WebSocket and release the connection lease

#### Scenario: Factory failure or cancellation
- **WHEN** the socket factory raises or is cancelled after connection admission succeeds
- **THEN** the runtime MUST release the connection lease and propagate the original terminal condition

#### Scenario: Outer connection budget exhaustion
- **WHEN** the outer WebSocket creation budget expires before the socket factory completes
- **THEN** the runtime MUST raise the existing proxy-budget-exhausted error

#### Scenario: Nested connect timeout before budget exhaustion
- **WHEN** the socket client raises its shorter per-attempt connect timeout while outer request budget remains
- **THEN** the runtime MUST preserve that transient connect failure
- **AND** it MUST NOT report that the full proxy request budget was exhausted

## ADDED Requirements

### Requirement: Direct Responses connect failure remains retryable before first event

The direct Responses WebSocket path MUST preserve the distinction between a per-attempt handshake failure and total request-budget exhaustion, and MUST expose a pre-first-event handshake timeout to the account retry layer before emitting a terminal downstream event.

#### Scenario: Direct handshake timeout with remaining budget
- **WHEN** the direct Responses WebSocket handshake reaches its per-attempt timeout while total request budget remains
- **THEN** the attempt MUST be classified as `upstream_connect_timeout`
- **AND** it MUST NOT emit `Proxy request budget exhausted`

#### Scenario: First-event classification enables account failover
- **WHEN** `upstream_connect_timeout` is the first event and another eligible account remains
- **THEN** the account retry layer MUST select another eligible account before the failure event is visible downstream

#### Scenario: Final candidate preserves streaming contract
- **WHEN** the final eligible account also reaches its handshake timeout
- **THEN** the client MUST receive an `upstream_connect_timeout` terminal SSE event
- **AND** the endpoint MUST preserve its existing streaming HTTP status contract

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

#### Scenario: Required continuity account fails
- **WHEN** a bridge request requires a preferred account because its upstream response state cannot safely move
- **AND** the bounded same-account retry also fails
- **THEN** bridge startup MUST surface the connection failure without rebinding the request to another account
