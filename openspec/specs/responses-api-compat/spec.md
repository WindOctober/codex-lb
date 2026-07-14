# responses-api-compat Specification

## Purpose

See context docs for background.
## Requirements
### Requirement: Use prompt_cache_key as OpenAI cache affinity
For OpenAI-style `/v1/responses`, `/v1/responses/compact`, and chat-completions requests mapped onto Responses, the service MUST treat a non-empty `prompt_cache_key` as a bounded upstream account affinity key for prompt-cache correctness. This affinity MUST apply even when dashboard `sticky_threads_enabled` is disabled, the service MUST continue forwarding the same `prompt_cache_key` upstream unchanged, and the stored affinity MUST expire after the configured freshness window so older keys can rebalance. The freshness window MUST come from dashboard settings so operators can adjust it without restart.

#### Scenario: dashboard prompt-cache affinity TTL is applied
- **WHEN** an operator updates the dashboard prompt-cache affinity TTL
- **THEN** subsequent OpenAI-style prompt-cache affinity decisions use the new freshness window

### Requirement: HTTP bridge bounds response creation startup

The HTTP bridge MUST apply a configurable startup deadline after a `response.create` send completes and before the upstream emits `response.created`. Upstream metadata and downstream keepalives MUST NOT satisfy this deadline. Once `response.created` assigns a response ID, the startup deadline MUST stop applying and existing total and idle response budgets MUST remain authoritative.

#### Scenario: metadata does not mask a missing response
- **WHEN** an HTTP bridge request has been sent upstream
- **AND** the upstream emits metadata but no `response.created` before the configured startup deadline
- **THEN** the bridge classifies the request as a `response_created_timeout`
- **AND** it does not wait for the full proxy request budget before starting recovery

#### Scenario: long response continues after creation
- **WHEN** the upstream emits `response.created` before the configured startup deadline
- **AND** model reasoning continues without text beyond that startup interval
- **THEN** the startup deadline does not terminate the request
- **AND** the normal total and stream-idle budgets continue to govern it

### Requirement: HTTP bridge startup replay is proof-gated

The HTTP bridge MUST transparently replay a pre-created request at most once only when exactly one request is pending and the request is either unanchored or carries a proxy-injected anchor backed by a retained, prefix-verified full-resend body. A replay of a proxy-injected anchor MUST use the retained unanchored body and MUST avoid the stalled account on first account selection. The bridge MUST NOT transparently replay client-owned continuations or any request that has produced downstream-visible response output.

#### Scenario: verified full resend recovers transparently
- **WHEN** exactly one pending request times out before `response.created`
- **AND** its `previous_response_id` was injected by the proxy
- **AND** the retained unanchored full-resend body is marked retry-safe
- **THEN** the bridge reconnects with the stalled account excluded
- **AND** it clears the injected anchor and submits the retained body exactly once

#### Scenario: client continuation remains fail-closed
- **WHEN** a request carrying a client-owned `previous_response_id` times out before `response.created`
- **THEN** the bridge does not automatically resubmit that continuation
- **AND** it returns a phase-specific terminal failure and retires the stale bridge

#### Scenario: multiplexed timeout retires ambiguous bridge
- **WHEN** one pre-created request reaches its startup deadline while another request remains pending on the same upstream WebSocket
- **THEN** the bridge does not transparently replay the timed-out request
- **AND** it retires the upstream bridge so a late anonymous `response.created` cannot be assigned to a sibling

### Requirement: HTTP bridge startup failure respects stream commitment

The HTTP bridge MUST preserve an upstream startup failure as an HTTP error when no downstream event has been emitted, and MUST emit a terminal `response.failed` SSE event instead of raising through the ASGI server when a keepalive has already committed the stream.

#### Scenario: Startup fails before stream commitment
- **WHEN** bridge session acquisition fails before any downstream event is emitted
- **THEN** the original proxy error status and error envelope remain available to the API response layer

#### Scenario: Startup fails after keepalive
- **WHEN** the bridge emits a startup keepalive and session acquisition later fails
- **THEN** the stream emits one terminal `response.failed` event carrying the normalized failure detail
- **AND** the failure does not escape as an ASGI application exception

#### Scenario: Downstream abandons startup wait
- **WHEN** the downstream closes after a startup keepalive while session acquisition is still pending
- **THEN** the bridge cancels and settles its owned acquisition task
- **AND** a later task failure does not appear as an unretrieved exception
