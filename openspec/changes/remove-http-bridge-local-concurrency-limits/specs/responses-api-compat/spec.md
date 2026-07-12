## ADDED Requirements

### Requirement: HTTP bridge does not locally cap per-session concurrency by default

HTTP `/v1/responses` and `/backend-api/codex/responses` bridge sessions MUST NOT reject or serialize compatible requests solely because multiple requests share the same prompt-cache, session-header, or turn-state bridge key. A configured positive bridge queue limit MAY still enforce a local queue cap for operators that opt into bounded per-session admission.

#### Scenario: default bridge accepts more than the historical queue cap

- **WHEN** more than eight compatible HTTP Responses requests target the same active bridge session
- **AND** the bridge queue limit is unset or configured as zero
- **THEN** the proxy accepts the requests into the bridge session instead of returning `rate_limit_exceeded` with `HTTP responses session bridge queue is full`

#### Scenario: explicit positive queue cap remains available

- **WHEN** an operator configures a positive HTTP bridge queue limit
- **AND** the number of queued requests for one bridge session reaches that limit
- **THEN** the proxy rejects additional requests locally with `rate_limit_exceeded`

### Requirement: HTTP bridge does not locally cap the session pool by default

HTTP `/v1/responses` and `/backend-api/codex/responses` bridge session creation MUST NOT reject new bridge sessions solely because the local in-memory bridge session pool reaches a default fixed size. A configured positive bridge session limit MAY still enforce a local pool cap for operators that opt into bounded bridge session count.

#### Scenario: default bridge pool accepts more than the historical session cap

- **WHEN** the configured HTTP bridge max sessions value is zero
- **AND** all currently open bridge sessions have pending work
- **THEN** the proxy creates another bridge session instead of returning `rate_limit_exceeded` with `HTTP responses session bridge has no idle capacity`

#### Scenario: explicit positive bridge pool cap remains available

- **WHEN** an operator configures a positive HTTP bridge max sessions value
- **AND** the bridge session pool reaches that limit with no idle evictable sessions
- **THEN** the proxy rejects additional bridge session creation locally with `rate_limit_exceeded`

### Requirement: Fresh soft prompt-cache bridge requests are sharded under pending load

HTTP bridge routing MUST spread fresh soft prompt-cache requests across multiple bridge session keys when the current shard already has pending work. The proxy MUST NOT shard requests that carry hard continuity identifiers such as turn-state/session affinity or `previous_response_id`.

#### Scenario: busy prompt-cache shard creates another bridge session

- **WHEN** a fresh HTTP Responses request has only soft prompt-cache affinity
- **AND** the current prompt-cache bridge shard already has at least the configured pending threshold
- **THEN** the proxy routes the request to another prompt-cache bridge shard instead of reusing the busy shard

#### Scenario: promoted prompt-cache shard creates another bridge session

- **WHEN** a fresh HTTP Responses request has only soft prompt-cache affinity
- **AND** the base prompt-cache bridge shard has been promoted to a Codex continuity session
- **AND** the promoted shard has pending work
- **THEN** the proxy routes the request to another prompt-cache bridge shard instead of returning a local busy-session error

#### Scenario: hard continuity requests are not sharded

- **WHEN** a request includes a turn-state/session affinity or `previous_response_id`
- **THEN** the proxy preserves the existing hard-continuity bridge route instead of deriving a soft prompt-cache shard

### Requirement: Busy hard-continuity bridge sessions are not torn down for conflicting recreates

When a request would require replacing an active hard-continuity HTTP bridge session, the proxy MUST preserve that session if it still has pending downstream requests. When local bridge capacity is available, the proxy MUST place the conflicting request on an isolated parallel bridge key instead of returning a local busy error. If no isolated bridge key or bridge capacity is available, the conflicting request MAY receive a retryable local error. Existing pending requests MUST NOT be failed by closing the busy session.

#### Scenario: conflicting recreate sees a busy session

- **WHEN** an active hard-continuity HTTP bridge session has pending requests
- **AND** a later request for the same bridge key cannot safely reuse that session
- **AND** bridge capacity is available
- **THEN** the proxy creates or reuses an isolated parallel bridge key for the later request
- **AND** it does not close the active bridge session or fail its pending requests

### Requirement: Busy continuity-bearing prompt-cache sessions use isolated parallel bridge keys

When a soft prompt-cache HTTP bridge session carries Codex continuity evidence, including Codex affinity, previous-response aliases, turn-state aliases, or a current continuity request, the proxy MUST preserve the active session if it still has pending downstream requests and a later continuity request for the same prompt-cache route cannot safely reuse it. When local bridge capacity is available, the proxy MUST place the later request on an isolated parallel bridge key instead of returning a local busy error. Existing pending requests MUST NOT be failed by closing the busy prompt-cache continuity session.

#### Scenario: conflicting continuity request sees a busy promoted prompt-cache session

- **WHEN** an active soft prompt-cache HTTP bridge session has been promoted to Codex continuity
- **AND** the session has pending requests
- **AND** a later continuity request for the same prompt-cache bridge route cannot safely reuse that session
- **AND** bridge capacity is available
- **THEN** the proxy creates or reuses an isolated parallel bridge key for the later request
- **AND** it does not close the active bridge session or fail its pending requests

#### Scenario: previous-response prompt-cache session sees a busy continuity request

- **WHEN** an active soft prompt-cache HTTP bridge session has previous-response continuity state
- **AND** the session has pending requests
- **AND** a later continuity request for the same prompt-cache bridge route cannot safely reuse that session
- **AND** bridge capacity is available
- **THEN** the proxy creates or reuses an isolated parallel bridge key for the later request
- **AND** it does not close the active bridge session or fail its pending requests

### Requirement: Connect-phase websocket forbidden responses can fail over

When opening an upstream websocket for a proxy websocket or HTTP bridge session, a generic upstream `403` forbidden handshake failure MUST be treated as a connect-phase transient for failover purposes. The proxy MUST preserve non-connect `403` permission failures as non-retryable.

#### Scenario: bridge creation retries another account after forbidden handshake

- **WHEN** HTTP bridge session creation receives an upstream websocket handshake `403` with a generic forbidden permission code
- **AND** another eligible account can be selected
- **THEN** the proxy retries bridge creation with another account instead of surfacing the first `403`

#### Scenario: first-event forbidden remains terminal

- **WHEN** an upstream response emits a `403` permission failure after request execution is visible
- **THEN** the proxy treats that failure as non-retryable

### Requirement: HTTP bridge rejects stale sessions before upstream submit

HTTP bridge request submission MUST verify that the selected local bridge session is still live and still registered as the current session for its bridge key after response-create admission is acquired. If the session has been closed, unregistered, or replaced before the request is sent upstream, the proxy MUST reject the request with a retryable upstream-unavailable error instead of sending `response.create` to the stale websocket.

#### Scenario: replaced bridge session is not used for submit

- **WHEN** a request is admitted for an HTTP bridge session
- **AND** that session has been replaced in the local bridge registry before `response.create` is sent
- **THEN** the proxy returns `upstream_unavailable`
- **AND** it does not send the request to the replaced websocket
- **AND** it releases the request's local admission and concurrency leases

### Requirement: HTTP bridge reclaims idle sessions for account-model capacity

When HTTP bridge session creation selects an account that is at the local per-account/model bridge session budget, the proxy MUST reclaim an idle local bridge session for that account and model before trying other accounts, waiting, or surfacing local overload. Reclaimed sessions MUST have no pending requests, and active pending sessions MUST NOT be closed for this purpose.

#### Scenario: idle bridge session frees a full account-model slot

- **WHEN** the load balancer selects an account that is at the local HTTP bridge account/model session budget
- **AND** at least one existing local bridge session for that account and requested model has no pending requests
- **THEN** the proxy closes one idle bridge session from that selected account
- **AND** it retries account selection and bridge session creation
- **AND** it does not wait for global bridge capacity exhaustion while reclaimable idle capacity exists for the selected account

#### Scenario: busy bridge sessions are not reclaimed

- **WHEN** every eligible account is at the local HTTP bridge account/model session budget
- **AND** all existing local bridge sessions for the requested model have pending requests
- **THEN** the proxy preserves those busy sessions instead of reclaiming them for new session creation

### Requirement: HTTP bridge connect-slot pressure reselects another account

When HTTP bridge session creation encounters a selected account that is already at the local per-account/model connect budget, the proxy MUST prefer another eligible account instead of waiting on that selected account's connect slot. If no eligible alternate account can be selected within the request budget, the proxy MAY surface the normal local overload or upstream-unavailable response.

#### Scenario: connect-full account is skipped when another account can serve

- **WHEN** HTTP bridge session creation selects an account at the local HTTP bridge account/model connect budget
- **AND** another eligible account can be selected for the requested model
- **THEN** the proxy excludes the connect-full account and retries account selection
- **AND** it opens the bridge session with the alternate account instead of waiting for the first account's connect slot

### Requirement: Precreated HTTP bridge retries avoid the disconnected account first

When an HTTP bridge upstream websocket disconnects before `response.created` and exactly one pending request can be safely replayed, the transparent precreated retry MUST first exclude the account whose websocket just disconnected. If another eligible account is available, the replay MUST reconnect through that alternate account instead of repeatedly reusing the same failing account.

#### Scenario: precreated retry fails over after upstream keepalive timeout

- **WHEN** a soft prompt-cache HTTP bridge session has one pending request that has not received `response.created`
- **AND** the upstream websocket disconnects with a keepalive timeout before `response.completed`
- **AND** another eligible account can serve the requested model
- **THEN** the bridge replay reconnects with the disconnected account excluded from first-choice selection
- **AND** it resends the original `response.create` payload on the alternate upstream websocket
