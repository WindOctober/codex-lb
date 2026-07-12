## ADDED Requirements

### Requirement: HTTP bridge pressure uses routable budget-safe capacity

When no explicit HTTP bridge max-session cap is configured, pressure eviction MUST estimate bridge capacity from the currently routable budget-safe accounts for the requested model and API-key scope rather than only from accounts already represented in the local bridge session pool. If at least one budget-safe routable account exists, accounts above the configured budget threshold MUST NOT contribute to that pressure capacity. If no budget-safe routable account exists or the routable account set cannot be resolved, the proxy MAY fall back to the existing local session-pool capacity estimate.

#### Scenario: unused eligible accounts delay prompt-cache pressure eviction

- **GIVEN** the local bridge session pool contains sessions for only a subset of routable budget-safe accounts
- **AND** the pool count would reach the pressure threshold if capacity were calculated only from that subset
- **WHEN** a new HTTP bridge request needs capacity
- **THEN** pressure eviction calculates capacity from the larger routable budget-safe account set
- **AND** idle soft prompt-cache sessions are not evicted solely because unused eligible accounts have not yet entered the bridge session pool

### Requirement: HTTP bridge eviction preserves handed-out sessions

HTTP bridge idle eviction, LRU eviction, pressure eviction, stale replacement, and soft prompt-cache reallocation MUST NOT retire or unregister a session that has been handed out for request submission but has not yet been counted as queued or pending. The handoff protection MUST end once the request is queued or when submission fails before queueing.

#### Scenario: pressure eviction races with submit admission

- **GIVEN** a soft prompt-cache HTTP bridge session was selected for a request
- **AND** that request has not yet entered the session pending queue
- **WHEN** another request triggers HTTP bridge pressure eviction
- **THEN** the handed-out session is not selected as an idle pressure-eviction candidate
- **AND** the original submit path does not fail because its session was retired after admission

#### Scenario: stale replacement races with submit admission

- **GIVEN** a soft prompt-cache HTTP bridge session was handed out to a request
- **AND** that request has not yet entered the session pending queue
- **WHEN** another request for the same affinity key decides the existing session should be replaced or reallocated
- **THEN** the handed-out session is treated as busy
- **AND** the replacement request uses a parallel soft prompt-cache session when allowed instead of retiring the handed-out session

### Requirement: HTTP bridge reconnect fails over connection-layer errors

When an HTTP bridge session reconnects to a fresh upstream after a local websocket disconnect or send failure, retryable connection-layer failures MUST be eligible for account failover before a downstream 5xx is returned. The reconnect path MUST NOT restrict failover handling to refresh and aiohttp timeout errors when the websocket client wraps connection reset, rejected handshake, or upstream unavailable as `ProxyResponseError`.

#### Scenario: fresh reconnect switches account after websocket reset

- **GIVEN** an HTTP bridge request attempts to reconnect a session on a fresh upstream
- **AND** the preferred account's websocket connect fails with a retryable upstream-unavailable error
- **WHEN** another routable account remains available within the request budget
- **THEN** reconnect excludes the failed account and attempts another account
- **AND** the downstream request is not failed solely because the first reconnect account reset the websocket connection

#### Scenario: fresh reconnect reacquires released session capacity

- **GIVEN** an HTTP bridge session was closed by upstream-disconnect handling
- **AND** the close path released the session's account-model session lease
- **WHEN** a subsequent request reconnects that session to a fresh upstream
- **THEN** reconnect reacquires account-model session capacity before opening the new upstream websocket
- **AND** successful reconnect is not followed by a restore failure solely because the previous close path released the lease

#### Scenario: submit reconnect waits for disconnect cleanup

- **GIVEN** an HTTP bridge upstream reader is closing a session after an upstream disconnect
- **AND** a request for the same handed-out session starts submit recovery while that close is still in progress
- **WHEN** the submit path reconnects the session to a fresh upstream
- **THEN** reconnect waits for the session lifecycle close section before reopening upstream state
- **AND** the old close path does not release the new upstream lease or make restore fail after reconnect succeeds

#### Scenario: hard-key reconnect restore replaces idle conflicting session

- **GIVEN** a hard-affinity HTTP bridge session reconnects successfully
- **AND** the bridge key has been reoccupied by another idle local session while the reconnecting session was closed
- **WHEN** the reconnecting session is restored into the local bridge pool
- **THEN** the idle conflicting session is closed and replaced atomically
- **AND** restore does not fail solely because the key was temporarily reoccupied

#### Scenario: previous-response owner waits for local bridge capacity

- **GIVEN** an HTTP bridge request has a `previous_response_id` whose owner account is known
- **AND** continuity requires the request to stay on that owner account
- **AND** the owner account is temporarily at the local HTTP bridge account-model session limit
- **WHEN** another local account could accept a new bridge session
- **THEN** the request waits for owner-account bridge capacity within the request budget
- **AND** it does not exclude the owner account and fail closed solely because another account was selected

#### Scenario: busy prompt-cache family reuses an idle parallel slot

- **GIVEN** a soft prompt-cache HTTP bridge family has a busy base session
- **AND** all configured busy-parallel slot indexes already have local sessions
- **AND** at least one of those parallel sessions is idle and has not been handed out for submission
- **WHEN** another request for that prompt-cache family needs a parallel bridge session
- **THEN** the bridge selects an idle existing parallel slot for replacement
- **AND** the request does not fail with a busy 503 solely because every slot index already exists

#### Scenario: first-turn prompt-cache batch requests use busy parallel slots

- **GIVEN** a soft prompt-cache HTTP bridge session is busy with a first-turn batch request
- **AND** the session has no previous-response, turn-state, or completed-response continuity markers yet
- **WHEN** another first-turn request with the same prompt-cache affinity arrives
- **THEN** the bridge may create or reuse a busy-parallel prompt-cache session for that request
- **AND** the request does not fail with a busy 503 solely because the existing prompt-cache session has no continuity marker

#### Scenario: previous-response owner unavailable enters local recovery

- **GIVEN** an HTTP bridge request carries a `previous_response_id`
- **AND** the previous response owner account is temporarily unavailable
- **WHEN** the bridge receives an owner-unavailable upstream error before yielding any downstream content
- **THEN** the error is treated as eligible for local previous-response recovery
- **AND** the bridge attempts the existing local rebind recovery path instead of surfacing an immediate owner-unavailable 5xx

### Requirement: Proxy API key validation degrades without unhandled 500s

Proxy API key validation MUST NOT surface raw database connection failures as unhandled 500 responses on Codex proxy routes. When a previously validated API key is present in the local API key cache and the cached API key itself has not expired, a transient database validation failure MAY use that stale cached key. If no usable stale key exists, the proxy MUST return a handled service-unavailable error instead of an unhandled server error.

#### Scenario: expired cache TTL with transient validation database failure

- **GIVEN** a proxy API key was previously validated and cached
- **AND** the local cache TTL has expired but the API key's own expiry has not elapsed
- **WHEN** the validation database lookup fails transiently
- **THEN** the proxy uses the stale cached key
- **AND** the request does not fail with an unhandled 500

### Requirement: HTTP bridge pressure uses one routing scope consistently

When HTTP bridge pressure eviction uses capacity derived from a routed account scope, it MUST calculate current session count and eviction candidates from the same account scope. A scoped capacity estimate MUST NOT be compared against global bridge session count, and scoped pressure eviction MUST NOT evict sessions outside that scoped account set.

#### Scenario: single-account API key does not trigger global eviction

- **GIVEN** one API key can route to only one account
- **AND** many bridge sessions exist globally for other API keys or account scopes
- **WHEN** pressure capacity is calculated from that single routable account
- **THEN** pressure compares the capacity only with sessions owned by that account
- **AND** sessions from other account scopes are not selected as pressure-eviction candidates
