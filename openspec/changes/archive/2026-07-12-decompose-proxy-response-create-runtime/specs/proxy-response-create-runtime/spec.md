## ADDED Requirements

### Requirement: Response-create preparation has a typed runtime boundary

Shared HTTP bridge and WebSocket `response.create` request preparation MUST be implemented outside the proxy facade behind an explicit typed service capability boundary, while existing `ProxyService` method names and signatures remain callable.

#### Scenario: HTTP bridge preparation
- **WHEN** HTTP bridge orchestration prepares a Responses request
- **THEN** the runtime MUST produce the same request state and upstream JSON text through the inherited service method

#### Scenario: WebSocket preparation
- **WHEN** downstream WebSocket orchestration prepares a Responses request
- **THEN** the runtime MUST produce the same request state and upstream JSON text through the inherited service method

#### Scenario: Dependency direction
- **WHEN** the preparation runtime consumes admission, size enforcement, or pure payload policy
- **THEN** it MUST use explicit service capabilities or canonical helpers without importing the proxy facade

### Requirement: Response-create payload and state semantics are preserved

The extracted runtime MUST preserve request IDs, request-log IDs, metadata, input fingerprints, service-tier fields, reasoning effort, previous-response ownership fields, event-queue attachment, JSON serialization, canonical slimming, and size enforcement.

#### Scenario: Prepared request state
- **WHEN** a valid Responses payload is prepared for HTTP bridge or WebSocket transport
- **THEN** transport-specific type, queue, metadata, session, and request-state fields MUST match the existing behavior

#### Scenario: Oversized payload
- **WHEN** serialized `response.create` data exceeds the replaceable service maximum
- **THEN** the runtime MUST apply the existing canonical slimming and size-enforcement behavior using the current service-level threshold and dump seams

### Requirement: Response-create admission cleanup is preserved

The extracted runtime MUST preserve response-create gate ordering, work-admission acquisition, timing fields, and cleanup on every terminal acquisition path.

#### Scenario: Successful admission
- **WHEN** a response-create gate and work-admission capacity are available
- **THEN** the runtime MUST acquire the gate before work admission and record the existing acquisition state and timestamps

#### Scenario: Admission failure or cancellation
- **WHEN** work-admission acquisition raises or is cancelled after the response-create gate is acquired
- **THEN** the runtime MUST release the gate exactly once and propagate the original terminal condition
