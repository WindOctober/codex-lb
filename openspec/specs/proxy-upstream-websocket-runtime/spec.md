# proxy-upstream-websocket-runtime Specification

## Purpose

Define the typed shared boundary and behavior-preservation contract for admitted, budgeted upstream Responses WebSocket creation.
## Requirements
### Requirement: Upstream WebSocket creation has a shared typed runtime boundary

Upstream Responses WebSocket creation shared by HTTP bridge and downstream WebSocket orchestration MUST be implemented outside the proxy facade behind an explicit typed service capability boundary, while the existing `ProxyService` method names and signatures remain callable.

#### Scenario: HTTP bridge consumer
- **WHEN** HTTP bridge session creation or reconnection opens an upstream WebSocket
- **THEN** it MUST use the inherited shared runtime method with the existing call signature

#### Scenario: Downstream WebSocket consumer
- **WHEN** downstream WebSocket connection policy opens an upstream WebSocket
- **THEN** it MUST use the inherited shared runtime method with the existing call signature

#### Scenario: Dependency direction
- **WHEN** the runtime consumes encryption, admission, provider transforms, or the replaceable socket factory
- **THEN** it MUST use explicit service capabilities or canonical helpers without importing the proxy facade

### Requirement: Provider and socket-factory semantics are preserved

The extracted runtime MUST preserve provider transport gating, token decryption, upstream account-header derivation, base URL and wire API forwarding, and service-module socket-factory replacement behavior.

#### Scenario: Supported provider connection
- **WHEN** a supported account opens an upstream WebSocket
- **THEN** the runtime MUST invoke the current socket factory exactly once with the existing headers, decrypted token, derived account header, base URL, and wire API

#### Scenario: Unsupported provider transport
- **WHEN** an account configuration does not support WebSocket transport
- **THEN** the runtime MUST return the existing `unsupported_transport` error before acquiring connection admission or invoking the socket factory

#### Scenario: Legacy replacement factory
- **WHEN** a service-module replacement socket factory omits a newer optional keyword
- **THEN** the runtime MUST retain the existing compatible dispatch behavior

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

### Requirement: HTTP bridge WebSocket has one receive owner

Each live HTTP bridge upstream WebSocket MUST have exactly one registered reader task, including while the bridge replaces its upstream connection during transparent recovery.

#### Scenario: Reader initiates reconnect
- **WHEN** the registered upstream reader initiates a bridge reconnect
- **THEN** the current reader retains ownership of the replacement WebSocket
- **AND** the bridge does not create a second reader task

#### Scenario: External task initiates reconnect
- **WHEN** a task other than the registered reader initiates a reconnect that requires a reader restart
- **THEN** the old reader is cancelled and settled before a replacement reader is created

#### Scenario: Old reader cannot stop
- **WHEN** an external reconnect cannot settle the old reader within the cancellation budget
- **THEN** the bridge is marked closed and the replacement WebSocket is not installed
