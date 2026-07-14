## ADDED Requirements

### Requirement: GPT-5.6-and-later request-level prompt-cache policy is preserved
The service MUST accept `prompt_cache_options` for versioned GPT-5.6-and-later Responses and Responses Compact requests, MUST restrict `mode` to `implicit` or `explicit`, MUST restrict `ttl` to `30m`, and MUST forward the validated object without translating it to `prompt_cache_retention`.

#### Scenario: Explicit cache policy is forwarded
- **WHEN** a GPT-5.6 request supplies `prompt_cache_options` with `mode` set to `explicit` and `ttl` set to `30m`
- **THEN** the upstream request contains the same policy values
- **AND** the upstream request does not add or rewrite `prompt_cache_retention`

#### Scenario: Explicit mode without a breakpoint remains uncached by policy
- **WHEN** a GPT-5.6 request supplies `prompt_cache_options.mode` set to `explicit`
- **AND** the request contains no `prompt_cache_breakpoint`
- **THEN** the service forwards the request without inserting an implicit or explicit breakpoint

#### Scenario: Invalid request-level cache policy is rejected
- **WHEN** a request supplies an unsupported prompt-cache mode, TTL, or extra policy field
- **THEN** the service returns an OpenAI-compatible invalid-request response before upstream execution

#### Scenario: Explicit null policy values are rejected
- **WHEN** a request supplies `prompt_cache_options` as JSON `null`
- **OR** explicitly supplies a `null` mode or TTL member
- **THEN** the service rejects the request instead of silently omitting that value during serialization

#### Scenario: Older model does not receive GPT-5.6 cache controls
- **WHEN** a request targeting an older model supplies `prompt_cache_options` or `prompt_cache_breakpoint`
- **THEN** the service rejects the incompatible request
- **AND** it does not translate or silently strip the GPT-5.6 control

#### Scenario: API-key model enforcement cannot bypass model isolation
- **WHEN** a request is valid for GPT-5.6 cache controls at parse time
- **AND** API-key policy enforces an older model before upstream execution
- **THEN** the service rejects the incompatible enforced route
- **AND** the older model does not receive the GPT-5.6-only fields

#### Scenario: API-key enforcement may upgrade the effective cache model
- **WHEN** a request names an older model and contains structurally valid GPT-5.6 cache controls
- **AND** API-key policy enforces a GPT-5.6-or-later model before upstream execution
- **THEN** model compatibility is evaluated against the enforced effective model
- **AND** the service accepts and forwards the cache controls instead of rejecting the pre-enforcement model

### Requirement: Explicit prompt-cache breakpoints retain content boundaries
The service MUST accept `prompt_cache_breakpoint` on GPT-5.6-and-later Responses `input_text`, `input_image`, and `input_file` content blocks, MUST require breakpoint mode `explicit`, and MUST preserve the containing input order and content fields when forwarding.

#### Scenario: Text breakpoint is preserved
- **WHEN** a GPT-5.6 request marks an `input_text` block with `prompt_cache_breakpoint.mode` set to `explicit`
- **THEN** the upstream payload contains the marker on the same content block
- **AND** all content before and after the marked block retains its original order

#### Scenario: Image and file breakpoints are preserved
- **WHEN** a GPT-5.6 request marks a supported `input_image` or `input_file` block with an explicit breakpoint
- **THEN** the upstream payload preserves the marker and the block's other fields

#### Scenario: Project-scoped file reference remains rejected
- **WHEN** a request marks an `input_file` block that uses a project-scoped `file_id`
- **AND** the proxy has no authoritative upload-owner mapping for that file
- **THEN** the service retains the existing unsupported-file-reference rejection
- **AND** routable `file_url` or `file_data` blocks may still preserve an explicit breakpoint

#### Scenario: Invalid breakpoint is rejected
- **WHEN** a breakpoint uses a mode other than `explicit`, contains an unsupported field, or appears on an unsupported block type
- **THEN** the service returns an OpenAI-compatible invalid-request response before upstream execution

#### Scenario: Compatibility normalization cannot silently remove a breakpoint
- **WHEN** a supported user content block passes through `messages` compatibility coercion
- **THEN** the service preserves its breakpoint on the normalized content block
- **AND** when a marked block cannot be represented after coercion or sanitization, the service rejects the request instead of removing the marker

#### Scenario: Oversized historical image slimming retains the boundary
- **WHEN** a marked historical inline `input_image` is replaced with an omission notice to satisfy the upstream WebSocket size limit
- **THEN** the replacement content block retains the explicit breakpoint
- **AND** no other breakpoint is inserted

### Requirement: Prompt-cache key controls upstream account locality
The service MUST forward a non-empty client `prompt_cache_key` unchanged and MUST apply bounded prompt-cache sticky-session affinity to GPT-5.6 explicit-cache requests while the selected account remains eligible. The internal affinity identity MUST include the API-key tenant scope, normalized request model (trimmed and lower-cased), and client key and MUST NOT expose that internal identity on the upstream wire.

#### Scenario: Repeated key reuses eligible account
- **WHEN** two GPT-5.6 explicit-cache requests have the same non-empty `prompt_cache_key`
- **AND** the first selected account remains eligible
- **THEN** both requests select the same upstream account
- **AND** both upstream payloads contain the unchanged client key

#### Scenario: Tenant and model scopes do not share an internal binding
- **WHEN** two requests use the same client prompt-cache key but have different API-key tenant scopes or normalized models
- **THEN** they use different internal sticky identities
- **AND** each upstream payload still contains the same unchanged client key supplied by that request

#### Scenario: Pre-namespace row cold-rebinds
- **WHEN** only a legacy raw-key prompt-cache sticky row exists
- **THEN** a new request does not reuse that unscoped row
- **AND** it creates or resolves a versioned tenant/model-scoped binding

#### Scenario: Explicit key survives per-connection turn state
- **WHEN** two WebSocket connections use the same explicit `prompt_cache_key`
- **AND** Codex session affinity is disabled while prompt-cache affinity is enabled
- **THEN** generated per-connection turn-state does not replace the prompt-cache affinity key
- **AND** the client key remains unchanged in both upstream payloads

#### Scenario: Availability can supersede cache locality
- **WHEN** the account bound to a prompt-cache key becomes ineligible or fails with a retryable account-scoped failure
- **THEN** the service may move the request to another eligible account according to existing failover rules
- **AND** it does not change the client prompt-cache key

#### Scenario: Alpha search shares an eligible parent binding
- **WHEN** an alpha-search request ID equals the parent Responses turn's client prompt-cache key
- **AND** both requests have the same API-key tenant scope and normalized model
- **AND** the bound account supports the Codex search wire API
- **THEN** alpha search resolves the same versioned internal prompt-cache identity
- **AND** it prefers the parent account while retaining the raw request ID on the upstream wire

#### Scenario: Search capability supersedes parent locality
- **WHEN** the parent prompt-cache binding points to an upstream that does not support the Codex search wire API
- **THEN** alpha search does not consume an upstream attempt on that account
- **AND** it selects or rebinds to an eligible Codex search upstream according to existing routing rules

### Requirement: Cache controls require a capable native Responses upstream
The service MUST select an API-key provider configured for the native Responses wire API whenever a request contains `prompt_cache_options` or `prompt_cache_breakpoint`, and MUST enforce the same requirement when reusing or reconnecting a Direct WebSocket or HTTP bridge session.

#### Scenario: Mixed pool selects a capable provider
- **WHEN** the eligible pool contains ChatGPT OAuth accounts and a native Responses API-key provider
- **AND** a GPT-5.6 request contains cache controls
- **THEN** selection excludes the OAuth accounts before sticky resolution
- **AND** the request is sent through the native Responses provider

#### Scenario: No capable provider fails locally
- **WHEN** no eligible native Responses provider exists for a cache-control request
- **THEN** the service returns a deterministic capability-unavailable response before upstream execution
- **AND** it does not mark an otherwise healthy OAuth account as failed

#### Scenario: Reused transport cannot bypass capability
- **WHEN** an existing Direct WebSocket or HTTP bridge session is attached to an upstream that lacks native Responses capability
- **AND** a later request introduces cache controls
- **THEN** the session is not used to send that request
- **AND** the request reconnects to a capable provider or fails locally

#### Scenario: Compact uses the provider endpoint
- **WHEN** a capable provider handles a Responses Compact request
- **THEN** the upstream path is `/v1/responses/compact`

### Requirement: Prompt-cache usage remains upstream-authored
The service MUST preserve upstream `usage.input_tokens_details.cached_tokens` and `usage.input_tokens_details.cache_write_tokens` in streamed terminal events and collected Responses JSON, and MUST NOT synthesize a cache hit, cache write, or cached model response.

#### Scenario: Streamed usage retains cache reads and writes
- **WHEN** upstream emits a `response.completed` SSE event containing cached-token and cache-write-token counts
- **THEN** the downstream terminal event contains the same counts

#### Scenario: Collected usage retains cache reads and writes
- **WHEN** a non-streaming Responses request is collected from an upstream terminal event containing cache metrics
- **THEN** the JSON response contains the same cached-token and cache-write-token counts

#### Scenario: Cache key does not memoize a response
- **WHEN** two requests reuse a prompt-cache key with different dynamic suffixes
- **THEN** the service executes both upstream requests
- **AND** each request returns its own newly generated response

### Requirement: Cache-write usage is financially and historically observable
The service MUST persist observed cache-write token counts on request logs, MUST distinguish historical unknown values from observed zero, MUST apply GPT-5.6 cache-write pricing as a 1.25x effective uncached-input rate, and MUST NOT add cache-write tokens a second time to token quotas.

#### Scenario: Observed write usage is persisted and exposed
- **WHEN** a Direct SSE, WebSocket, or Compact terminal response reports `cache_write_tokens`
- **THEN** API-key settlement and the request log receive the same observed count
- **AND** the per-request API exposes that count

#### Scenario: Historical row remains unknown
- **WHEN** the nullable request-log migration upgrades a row created before cache-write recording
- **THEN** its `cache_write_tokens` value remains `NULL`
- **AND** the migration does not infer or backfill zero

#### Scenario: Write pricing adds only the uplift
- **WHEN** GPT-5.6 reports cache-write tokens that are already included in input tokens
- **THEN** the ordinary uncached-input charge remains counted once
- **AND** the additional write charge is 25 percent of the effective input rate for those write tokens
- **AND** overlapping or invalid cached/write details cannot produce a negative or 2.25x input charge

#### Scenario: Cost and token quotas use different classifications
- **WHEN** an API-key request reports GPT-5.6 cache-write tokens
- **THEN** the cost quota includes the cache-write uplift for success or failed/incomplete authoritative usage
- **AND** input and total-token quotas count the original input tokens exactly once

#### Scenario: Hidden retry attempts remain billable
- **WHEN** one or more Direct Responses attempts report authoritative terminal usage before the proxy retries or fails over
- **THEN** final API-key settlement includes every authoritative attempt's input, output, cached, and cache-write classifications
- **AND** each attempt's cost is calculated using that attempt's effective model and service tier before costs are summed
- **AND** token quotas count each attempt's original input and output tokens once

#### Scenario: Exhausted attempts with usage are failed-settled
- **WHEN** all Direct Responses attempts fail and at least one attempt reports authoritative usage
- **THEN** the reservation is failed-settled with the aggregate usage and per-attempt cost
- **AND** the service does not release the incurred usage

#### Scenario: Failure before authoritative usage releases reservation
- **WHEN** a request fails before any upstream attempt reports authoritative usage
- **THEN** the service releases the unused reservation according to the existing fallback path

#### Scenario: Future model is not silently priced as legacy GPT-5
- **WHEN** an unknown future GPT-5 minor model is not present in the pricing registry
- **THEN** the service does not resolve it through the legacy `gpt-5` wildcard
- **AND** API-key cost settlement retains a conservative unknown-model charge instead of releasing the cost reservation to zero
