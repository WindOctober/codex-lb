## ADDED Requirements

### Requirement: HTTP bridge runtime state collection has a typed responsibility boundary

Live bridge session sampling, request-status lookup, health repository access, account-capacity lookup, and egress-state projection MUST be implemented outside the proxy transport orchestration service behind an explicit typed capability boundary.

#### Scenario: Live request status lookup

- **WHEN** diagnostics query a request, response, or previous-response identifier
- **THEN** the runtime MUST preserve match priority, state classification, session metadata, request age, and the existing `None` result for blank or missing identifiers

#### Scenario: Account runtime lookup

- **WHEN** account APIs request current HTTP bridge occupancy
- **THEN** the runtime MUST preserve per-account session, pending, queued, busy, Codex, and reconnect-requested counts

#### Scenario: Complete dashboard snapshot

- **WHEN** the dashboard requests an HTTP bridge runtime snapshot
- **THEN** the runtime MUST preserve configuration, session observations, grouping, capacity, health, egress, sampling, and inflight creation fields

### Requirement: Runtime collection preserves concurrency and fallback semantics

The collection boundary MUST preserve existing lock scope, repository failure fallbacks, and dependency direction to the pure runtime calculation module.

#### Scenario: Concurrent live-state collection

- **WHEN** runtime data is collected while bridge sessions are active
- **THEN** the global session maps MUST be copied under the bridge lock
- **AND** each session's pending state MUST be copied under that session's pending lock
- **AND** repository I/O MUST NOT occur while the global bridge lock is held

#### Scenario: Health repository failure

- **WHEN** latency health loading fails
- **THEN** the runtime MUST return the existing empty health snapshot and derived status while isolating the repository exception

#### Scenario: Capacity account lookup failure

- **WHEN** active account loading fails
- **THEN** the runtime MUST preserve the existing fallback to accounts represented by active session counts

#### Scenario: Existing service consumer

- **WHEN** dashboard, accounts, or request-log diagnostics call runtime methods on `ProxyService`
- **THEN** method lookup and returned contracts MUST remain compatible through inheritance
