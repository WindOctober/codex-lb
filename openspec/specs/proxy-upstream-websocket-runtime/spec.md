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

The extracted runtime MUST preserve connection-budget enforcement, local admission ordering, and exactly-once release of the acquired connection lease on every terminal factory path.

#### Scenario: Successful creation
- **WHEN** connection admission succeeds and the socket factory returns an upstream WebSocket
- **THEN** the runtime MUST return that WebSocket and release the connection lease

#### Scenario: Factory failure or cancellation
- **WHEN** the socket factory raises or is cancelled after connection admission succeeds
- **THEN** the runtime MUST release the connection lease and propagate the original terminal condition

#### Scenario: Connection budget exhaustion
- **WHEN** upstream WebSocket creation exceeds its remaining timeout
- **THEN** the runtime MUST raise the existing proxy-budget-exhausted error
