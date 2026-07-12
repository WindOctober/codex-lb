## ADDED Requirements

### Requirement: Responses WebSocket runtime has typed responsibility boundaries

Responses WebSocket connection, relay, and downstream orchestration MUST be implemented outside the proxy orchestration service behind explicit typed capability boundaries.

#### Scenario: Upstream connection

- **GIVEN** a downstream WebSocket request and eligible accounts
- **WHEN** the connection slice opens an upstream socket
- **THEN** it MUST preserve account selection, request budget, token refresh, same-model failover, and downstream error behavior

#### Scenario: Upstream relay

- **GIVEN** an established upstream socket and pending requests
- **WHEN** upstream frames arrive or the socket terminates
- **THEN** relay MUST preserve event matching, transparent replay safety, settlement, and terminal frame ordering

#### Scenario: Downstream lifecycle

- **GIVEN** a connected client WebSocket
- **WHEN** the orchestration slice receives requests and the client or upstream disconnects
- **THEN** it MUST preserve request preparation, concurrent task coordination, cancellation, and resource cleanup

### Requirement: WebSocket compatibility seams remain explicit

Settings, remaining-budget, continuity metric, and upstream socket replacement points required by focused tests MUST remain explicit service adapters. Extracted modules MUST NOT import or inspect the proxy service module.

#### Scenario: WebSocket integration replaces a declared adapter

- **GIVEN** a focused integration replaces settings, budget, continuity, or upstream socket behavior
- **WHEN** the extracted WebSocket runtime executes
- **THEN** it MUST consume the replacement through a declared service adapter
- **AND** it MUST NOT inspect the proxy service module
