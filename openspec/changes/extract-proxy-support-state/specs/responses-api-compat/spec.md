## ADDED Requirements

### Requirement: Shared proxy request state has one canonical definition

HTTP bridge, streaming, and websocket implementations MUST use one canonical set of request, session, settlement, and transport-control state types.

#### Scenario: State shared across proxy transports

- **GIVEN** a request moves through bridge submission, upstream event processing, and downstream streaming
- **WHEN** each implementation slice accesses request state
- **THEN** each slice observes the same state object and field definitions
- **AND** no transport-specific duplicate state model is introduced

### Requirement: Support-state extraction preserves compatibility

Moving shared proxy state MUST preserve dataclass field order and defaults, exception attributes, affinity strength derivation, and the existing `app.modules.proxy.service` import path.

#### Scenario: Existing service import

- **GIVEN** existing code constructs `_HTTPBridgeSessionKey` or `_WebSocketRequestState` from `app.modules.proxy.service`
- **WHEN** support state is extracted
- **THEN** construction behavior and resulting type identity remain compatible
