## ADDED Requirements

### Requirement: HTTP bridge session management has typed responsibility boundaries

HTTP bridge policy, lifecycle, capacity, creation, and acquisition logic MUST be implemented outside the proxy orchestration service behind explicit typed capability boundaries.

#### Scenario: Existing session reuse

- **GIVEN** an eligible live session matches request affinity, API-key scope, model support, and continuity ownership
- **WHEN** session acquisition handles the request
- **THEN** it MUST return the same session without opening another upstream connection
- **AND** it MUST preserve submit leases, indexes, and durable ownership behavior

#### Scenario: Bootstrap registry has no model verdict

- **GIVEN** the dynamic model registry has no snapshot
- **AND** the requested model is absent from the bundled bootstrap catalog
- **WHEN** an otherwise compatible live bridge session already owns the request model
- **THEN** session reuse MUST treat model support as unknown rather than unsupported
- **AND** it MUST apply the same conservative eligibility semantics as fresh account selection

#### Scenario: New session creation

- **GIVEN** no reusable session exists and capacity is available
- **WHEN** session acquisition creates a session
- **THEN** it MUST select and refresh an eligible account, acquire one account-model lease, open one upstream socket, register the session and aliases, and start one upstream reader
- **AND** every failure path MUST release acquired resources

#### Scenario: Same-key session replacement

- **GIVEN** an existing bridge session must be replaced under the same durable session key
- **WHEN** the old session is detached for background socket cleanup
- **THEN** its durable ownership MUST be released before the replacement claims that key
- **AND** reader and socket cleanup MAY continue asynchronously after ownership release

#### Scenario: Capacity pressure

- **GIVEN** the bridge is at configured session capacity
- **WHEN** a request requires another session
- **THEN** only sessions eligible under the canonical pressure policy MAY be reclaimed
- **AND** busy, leased, interactive, or continuity-critical sessions MUST retain their existing protections

### Requirement: Session compatibility hooks remain explicit and minimal

Internal replacement points required by focused tests or runtime integrations MUST be represented by explicit adapters. Extracted session-management modules MUST NOT import or dynamically inspect the proxy service module.

#### Scenario: Session integration replaces a declared adapter

- **GIVEN** a focused integration needs to replace session policy or lifecycle behavior
- **WHEN** it configures the session-management boundary
- **THEN** it MUST use a declared typed adapter
- **AND** the extracted module MUST NOT discover replacement behavior through proxy-service introspection
