## ADDED Requirements

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
