## ADDED Requirements

### Requirement: Continuity-to-account resolution has a typed boundary

Durable account validation and previous-response owner resolution MUST be implemented outside the proxy transport facade behind an explicit typed repository and cache capability boundary.

#### Scenario: Durable account remains valid

- **WHEN** a durable bridge lookup names an active account that supports the requested model
- **THEN** the runtime MUST return that account binding after cloning the account before repository scope ends

#### Scenario: Durable account is missing or incompatible

- **WHEN** the persisted account is missing, inactive, or does not support the requested model
- **THEN** the runtime MUST preserve the existing empty binding and model-support indicator semantics

#### Scenario: Durable validation repository failure

- **WHEN** durable account validation cannot query the repository
- **THEN** the runtime MUST preserve the durable account ID and optimistic model-support result while logging the validation failure

### Requirement: Previous-response owner cache and fail-closed behavior is preserved

The continuity runtime MUST preserve bounded scoped caching, request-log fallback, disabled negative caching, and fail-closed behavior when ownership cannot be resolved safely.

#### Scenario: Scoped cache hit

- **WHEN** a previous response has a cached owner for the API-key and session scope
- **THEN** the runtime MUST return that owner without querying request logs and record a request-cache hit

#### Scenario: Scoped lookup falls back to general cache

- **WHEN** no scoped cache entry exists but an unscoped owner is cached and request-log lookup misses or fails
- **THEN** the runtime MUST return the cached general owner and record the fallback source

#### Scenario: Request-log owner hit

- **WHEN** request logs resolve an account for the response, API key, and session scope
- **THEN** the runtime MUST cache both applicable key forms and return the account

#### Scenario: Owner lookup fails without fallback

- **WHEN** request-log lookup raises and no cached owner is available
- **THEN** the runtime MUST record fail-closed observability and return the existing 502 owner-lookup error

#### Scenario: Cache exceeds its bound

- **WHEN** inserting owner keys exceeds the configured cache limit
- **THEN** the runtime MUST evict the oldest insertion while retaining refreshed and newest keys

#### Scenario: Owner miss is remembered

- **WHEN** a caller reports a previous-response owner miss
- **THEN** the runtime MUST remain a no-op and MUST NOT install negative cache state

#### Scenario: Existing service consumer

- **WHEN** ordinary, HTTP bridge, or WebSocket code calls continuity methods on `ProxyService`
- **THEN** method lookup, cache introspection, and constant imports MUST remain compatible
