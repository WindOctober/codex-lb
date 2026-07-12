## ADDED Requirements

### Requirement: HTTP bridge pressure evicts idle prompt-cache batch sessions

When the HTTP bridge pool approaches configured capacity, the service MUST prefer reclaiming idle soft `prompt_cache` Codex bridge sessions that belong to a large parallel creation batch before reclaiming hard-continuity sessions.

#### Scenario: batch prompt-cache sessions are reclaimed under pressure

- **GIVEN** the HTTP bridge pool has reached the pressure threshold
- **AND** there are idle soft `prompt_cache` Codex bridge sessions from a large parallel batch
- **WHEN** a new bridge needs capacity
- **THEN** the service evicts eligible batch prompt-cache sessions before creating the new bridge
- **AND** busy sessions are not evicted by pressure reclamation
- **AND** hard `turn_state_header` or `session_header` sessions are not evicted by pressure reclamation

#### Scenario: interactive bridge sessions are preserved under pressure

- **GIVEN** the HTTP bridge pool has reached the pressure threshold
- **AND** there are idle soft `prompt_cache` bridge sessions created by an interactive VS Code or code-server client
- **AND** there are idle soft `prompt_cache` bridge sessions created by batch CLI traffic
- **WHEN** pressure reclamation chooses sessions to evict
- **THEN** the service preserves the interactive sessions
- **AND** the service prefers evicting idle batch CLI prompt-cache sessions.

### Requirement: HTTP bridge connect-time forbidden is retryable

When an upstream WebSocket connection attempt is rejected with a retryable connect-time forbidden error, the HTTP bridge MUST NOT expose a downstream 403 response after failover candidates are exhausted.

#### Scenario: exhausted connect-time forbidden returns retryable upstream unavailable

- **GIVEN** the HTTP bridge is creating or reattaching an upstream WebSocket
- **AND** eligible upstream accounts reject connection attempts with retryable forbidden errors
- **WHEN** no failover candidate remains
- **THEN** the service responds with `upstream_unavailable`
- **AND** the downstream HTTP status is 502 rather than 403.
