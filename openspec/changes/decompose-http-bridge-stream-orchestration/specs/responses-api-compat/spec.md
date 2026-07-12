## ADDED Requirements

### Requirement: HTTP bridge stream orchestration has a typed implementation boundary

HTTP bridge request preparation, ownership dispatch, local session acquisition, submission, downstream event streaming, recovery, and terminal cleanup MUST be implemented outside the proxy orchestration service behind an explicit typed capability boundary.

#### Scenario: Local bridge request

- **GIVEN** an HTTP Responses request resolves to the local bridge owner
- **WHEN** stream orchestration handles the request
- **THEN** it MUST preserve the same affinity key, account binding, session acquisition, request submission, SSE output, and cleanup behavior
- **AND** it MUST reserve and release API-key usage exactly as before

#### Scenario: Remote bridge owner

- **GIVEN** durable continuity resolves to an active remote bridge owner
- **WHEN** stream orchestration handles the request
- **THEN** it MUST forward the request through the existing owner-forwarding contract
- **AND** it MUST NOT create a conflicting local bridge session

#### Scenario: Continuation recovery

- **GIVEN** a continuation encounters an eligible owner, previous-response, bootstrap, or context-overflow condition
- **WHEN** recovery policy permits another local attempt
- **THEN** orchestration MUST preserve existing retry limits, hard-affinity protections, payload transformations, and fail-closed errors

### Requirement: HTTP bridge stream policy has one canonical implementation

Bridge key construction, full-resend detection, input-prefix matching, request-stage classification, effective idle lifetime, continuation recovery predicates, and bridge runtime configuration MUST each have one canonical internal implementation.

#### Scenario: Tested facade compatibility

- **GIVEN** focused tests or runtime integrations replace an existing service-module settings or registry hook
- **WHEN** the extracted policy is invoked through the service facade
- **THEN** the replacement MUST still be observed
- **AND** extracted modules MUST NOT import or dynamically inspect the service module
