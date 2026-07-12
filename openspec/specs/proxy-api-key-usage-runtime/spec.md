# proxy-api-key-usage-runtime Specification

## Purpose

Define the typed proxy responsibility boundary and behavior-preservation contract for API-key usage reservation and transport settlement.

## Requirements

### Requirement: Proxy API-key usage accounting has a typed responsibility boundary

API-key usage reservation, release, compact settlement, and stream settlement MUST be implemented outside the proxy transport orchestration service behind an explicit typed capability boundary.

#### Scenario: Reservation succeeds

- **WHEN** an authenticated request reserves API-key usage capacity
- **THEN** the runtime MUST enforce the existing model and service-tier limits within a cancellation-shielded repository scope
- **AND** it MUST return the existing reservation contract

#### Scenario: Reservation is rejected

- **WHEN** reservation enforcement raises an API-key rate-limit or invalid-key error
- **THEN** the runtime MUST preserve the existing proxy rate-limit or authentication error mapping and reset-time message

#### Scenario: Reservation is released

- **WHEN** a caller releases an existing reservation
- **THEN** the runtime MUST release the reservation within a cancellation-shielded repository scope
- **AND** a missing reservation MUST remain a no-op

### Requirement: API-key usage settlement preserves transport semantics

The extracted runtime MUST preserve the distinct compact and stream settlement contracts and MUST depend only on an explicit repository-factory capability plus shared typed request data.

#### Scenario: Successful compact or stream response

- **WHEN** a terminal response contains valid input and output token usage
- **THEN** the runtime MUST finalize the reservation with the existing model fallback, cached token, and effective service-tier values

#### Scenario: Missing or unsuccessful usage

- **WHEN** a compact response is absent, usage is incomplete, or a stream settlement is not successful
- **THEN** the runtime MUST release the reservation instead of finalizing it

#### Scenario: Settlement persistence fails

- **WHEN** compact or stream settlement raises an exception
- **THEN** the runtime MUST preserve existing warning behavior without raising into the proxied request
- **AND** stream settlement MUST return the existing failure indicator

#### Scenario: Existing transport caller

- **WHEN** ordinary streaming, compact, HTTP bridge, or WebSocket code invokes the accounting methods on `ProxyService`
- **THEN** method lookup and monkeypatch compatibility MUST remain intact through inheritance
