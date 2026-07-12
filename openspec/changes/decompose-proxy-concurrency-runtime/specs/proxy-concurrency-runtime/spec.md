## ADDED Requirements

### Requirement: Local account/model concurrency has a typed policy boundary

Limit lookup, saturation projection, lease acquisition and release, bounded bridge-connect waiting, and local overload projection MUST be implemented outside the proxy transport facade behind explicit typed service capabilities.

#### Scenario: Ordinary request budget

- **WHEN** an ordinary request attempts to acquire an account/model lease
- **THEN** the runtime MUST use the configured ordinary account/model limit and shared request/connect limiter
- **AND** it MUST return `None` and preserve saturation logging when the limit is full

#### Scenario: HTTP bridge connect budget

- **WHEN** HTTP bridge connection establishment attempts to acquire a lease
- **THEN** the runtime MUST use the configured connect limit on the shared request/connect limiter

#### Scenario: HTTP bridge session budget

- **WHEN** a durable HTTP bridge session attempts to acquire a session lease
- **THEN** the runtime MUST use the configured session limit on the dedicated session limiter

### Requirement: Concurrency wait and release behavior is preserved

The extracted runtime MUST preserve request-budget termination, lease release semantics, and the existing local overload contract.

#### Scenario: Connect slot becomes available

- **WHEN** a bridge connect waits on a full local limit and a slot becomes available before the deadline
- **THEN** the runtime MUST acquire and return the released capacity without changing accounts or models

#### Scenario: Connect wait exhausts request budget

- **WHEN** no bridge connect slot becomes available before the request deadline
- **THEN** the runtime MUST return the existing proxy request budget exhausted error

#### Scenario: Request state releases a lease

- **WHEN** a request-state concurrency lease is finalized
- **THEN** the runtime MUST clear the request-state reference before releasing the lease
- **AND** repeated release attempts MUST remain no-ops

#### Scenario: Local overload response

- **WHEN** every eligible account is saturated for a model
- **THEN** the runtime MUST preserve the existing HTTP 429 local-overload error code and model-specific message

#### Scenario: Existing service caller

- **WHEN** ordinary, HTTP bridge, or WebSocket code calls concurrency methods on `ProxyService`
- **THEN** method lookup and monkeypatch compatibility MUST remain intact through inheritance
