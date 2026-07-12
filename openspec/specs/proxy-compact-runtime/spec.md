# proxy-compact-runtime Specification

## Purpose

Define the typed internal boundary and behavior-preservation contract for compact response orchestration.

## Requirements

### Requirement: Compact orchestration has a typed runtime boundary

The compact request state machine MUST be implemented outside the proxy facade behind an explicit typed service capability boundary, while `ProxyService.compact_responses` remains callable with the existing signature.

#### Scenario: Existing compact caller
- **WHEN** an existing API route invokes `ProxyService.compact_responses`
- **THEN** method lookup, arguments, return type, and raised proxy errors MUST remain compatible

#### Scenario: Runtime dependencies
- **WHEN** compact orchestration needs settings, encryption, admission, account selection, refresh, accounting, or logging
- **THEN** it MUST use explicit service capabilities or existing pure helpers without importing the proxy facade back into the runtime module

### Requirement: Compact account and retry semantics are preserved

The extracted compact runtime MUST preserve affinity, account selection, request-budget, retry, refresh, failover, and provider compatibility behavior.

#### Scenario: Successful compact request
- **WHEN** an eligible account returns a successful compact response
- **THEN** the runtime MUST record account success, settle API-key usage, and return the response without selecting another account

#### Scenario: First authentication failure
- **WHEN** the upstream returns 401 before the forced-refresh retry has been used
- **THEN** the runtime MUST force-refresh the same account once and retry within the remaining request budget

#### Scenario: Repeated transient server failure
- **WHEN** an account repeatedly returns HTTP 500 through the configured same-account retry count
- **THEN** the runtime MUST record the transient failures, exclude that account, and attempt another eligible account when the failover budget allows

#### Scenario: Same-contract retry
- **WHEN** an upstream transport error is marked retryable on the same contract and the safe retry budget remains
- **THEN** the runtime MUST retry without prematurely changing account or surfacing the error

#### Scenario: Unsupported provider wire API
- **WHEN** the selected provider does not expose the Codex compact wire API
- **THEN** the runtime MUST fail closed with the existing not-implemented proxy error and MUST NOT synthesize compact output through a different transport

### Requirement: Compact accounting and observability are preserved

The extracted compact runtime MUST preserve API-key reservation settlement, request logging, service-tier attribution, and error reporting on every terminal path.

#### Scenario: Successful accounting
- **WHEN** a compact response contains usage and service-tier data
- **THEN** the runtime MUST settle usage and log token counts, latency, requested tier, actual tier, and successful status using the existing rules

#### Scenario: Terminal failure accounting
- **WHEN** all retries fail or a non-retryable error is surfaced
- **THEN** the runtime MUST release or settle the reservation according to existing API-key semantics and write the terminal request log from the `finally` path

#### Scenario: Local admission overload
- **WHEN** local compact response-create admission rejects work
- **THEN** the runtime MUST surface the existing local overload error without penalizing the selected upstream account

### Requirement: Compact attempt resources remain request-scoped

The extracted compact runtime MUST restore attempt-local timeout override state on every terminal path.

#### Scenario: Failure before the upstream compact call
- **WHEN** provider validation or local admission terminates an attempt before the upstream compact call starts
- **THEN** the runtime MUST leave compact timeout overrides at their pre-attempt values
