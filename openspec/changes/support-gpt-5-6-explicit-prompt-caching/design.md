## Context

The proxy already accepts `prompt_cache_key`, forwards it, and resolves it through bounded sticky-session affinity. The deployed 2455 path currently returns `400 Unsupported parameter: prompt_cache_options`; that end-to-end observation alone does not identify which layer rejected the field. The pre-change request models used permissive extras and therefore did not express or test the GPT-5.6 cache contract. Input content is represented as JSON values, and response usage parsing preserves unknown details but did not type `cache_write_tokens`.

GPT-5.6 explicit caching is an upstream KV-prefix cache. Cache correctness depends on preserving the rendered prefix, breakpoint position, request policy, key, upstream account, and upstream usage response. It must not be implemented as local response memoization.

## Goals / Non-Goals

**Goals:**

- Give Responses and Responses Compact requests a typed GPT-5.6 cache-policy contract.
- Validate breakpoint shape and location without flattening or reordering input content.
- Preserve bounded affinity while isolating internal mappings by tenant and normalized request model.
- Fail closed unless the selected/reused upstream implements the native Responses cache-control contract.
- Preserve cache-read and cache-write token counts in SSE, collected JSON, request logs, and API-key settlement.
- Price GPT-5.6 variants and cache writes without double charging input or token quotas.
- Measure live A1/A2/B1/no-breakpoint behavior on an isolated backend.

**Non-Goals:**

- Add a local prompt or response cache.
- Return an earlier response for a matching key.
- Translate GPT-5.6 controls into `prompt_cache_retention`.
- Implement client-side Semia sharding or rewrite Semia's `instructions` string.
- Promise a cache hit when the upstream evicts a prefix, routes fail over, or the rendered prefix differs.

## Decisions

### Use typed request-level policy models

Add a strict `PromptCacheOptions` value object with optional `mode` (`implicit` or `explicit`) and optional `ttl` (`30m`). Omitted members remain optional, while explicit JSON `null` is invalid rather than silently disappearing during serialization. Attach it to canonical and `/v1` Responses and Compact request models so FastAPI validation reports invalid fields before account selection.

Alternative considered: continue relying on `extra="allow"`. This forwards valid values but also accepts invalid modes and provides no stable OpenAPI or regression contract, which is the deployed failure mode the change must eliminate.

### Validate breakpoints in place

Inspect the canonical Responses input after message coercion. A `prompt_cache_breakpoint` is valid only on `input_text`, `input_image`, or `input_file` content blocks and must validate as `{ "mode": "explicit" }`. Return the original JSON structure after validation so ordering and unknown supported block fields remain unchanged.

The proxy continues to reject `input_file.file_id`. Those identifiers are scoped to the upstream project/account that accepted the upload, while current sticky selection has no authoritative file-owner mapping; merely selecting any native Responses provider could route the identifier to the wrong project. Routable `file_url` and `file_data` forms still preserve explicit breakpoints. Supporting `file_id` requires a unique provider binding or durable upload-owner mapping and is outside this change.

Alternative considered: convert every content block to a new Pydantic union. That would broaden this change into a complete Responses input-schema rewrite and risk dropping content variants already carried through as JSON.

### Gate new controls to GPT-5.6 and later

Requests using request-level options or explicit breakpoints must target a versioned GPT model at GPT-5.6 or later. Older-model requests fail with the existing OpenAI-style invalid-payload response and are never rewritten to `prompt_cache_retention`.

Alternative considered: forward controls for every model and let upstream reject them. Local gating gives deterministic behavior across accounts and prevents mixed-model compatibility builders from accidentally emitting 5.6-only fields.

### Namespace bounded sticky-session affinity

Do not add a second consistent-hash router. A non-empty `prompt_cache_key` becomes `StickySessionKind.PROMPT_CACHE` and is forwarded byte-for-byte. The sticky-table key is a versioned digest of API-key tenant (or the shared unauthenticated tenant), the request model after trimming and lower-casing, and the stripped client key. When Codex session affinity is disabled, that explicit key takes precedence over per-connection generated turn-state. Old raw-key rows cold-rebind under the v2 namespace.

Alternative considered: hash directly over the currently eligible account list. Membership changes would remap many keys and would compete with durable sticky state and existing failure accounting.

### Require a capable native Responses upstream

Cache controls require an API-key provider whose configured wire API is `responses` or `v1`. Capability filtering happens before sticky selection. The requirement is propagated through direct streaming, Compact, direct WebSocket connect/reuse, and HTTP bridge create/reconnect so an existing OAuth session cannot receive a later cache-control request. If no eligible provider remains, the proxy returns a deterministic local capability error without penalizing unrelated account health.

Compact provider URLs use `/v1/responses/compact`; the ChatGPT Codex-only assumption is not reused for native providers.

### Persist and price cache-write usage

Add `cache_write_tokens` to `ResponseUsageDetails` and carry it through Direct SSE, WebSocket, Compact, request logging, model rewrites, and API-key success/failed settlement. Add a nullable request-log column without historical backfill.

Define explicit GPT-5.6 Sol/Terra/Luna prices. Standard short-context rates are respectively `(5, 0.5, 30)`, `(2.5, 0.25, 15)`, and `(1, 0.1, 6)` USD per million input/cached/output tokens. Flex is half, Priority is double, and standard/Flex input above 272K uses doubled input/cached rates and 1.5x output. Cache writes use the effective input rate times 1.25. Since write tokens are already included in input tokens, cost calculation adds only `(1.25 - 1.0)` uplift and clamps inconsistent cached/write overlap.

API-key cost reservation assumes all 8,192 reserved input tokens might be GPT-5.6 cache writes. Final settlement uses authoritative write usage for successful and failed/incomplete terminal responses. Direct retries retain an immutable charge record for each attempt; token classifications are summed, while cost is calculated per attempt using its own effective model and service tier before summation. If all attempts fail after reporting usage, the reservation is failed-settled rather than released. Token quota deltas remain the sum of each attempt's original `input + output`, with cache-write tokens never added a second time.

Unknown future models do not inherit legacy GPT-5 prices. Because resolving an unknown price to zero would bypass a COST_USD limit, API-key reservation and settlement retain a conservative two-dollar charge per authoritative unknown-model attempt until an explicit price is registered. Request-log cost remains unknown rather than claiming that conservative quota charge is an observed provider price.

### Measure treatment against a frozen baseline

Record the deployed 2455 end-to-end rejection as the before-state without attributing it to a specific layer. Run A1/A2/B1/no-breakpoint requests only against an isolated 3456 backend until all local gates pass. Use unique stable keys and prefixes longer than 1,024 rendered tokens, report reads/writes and latency, then stop 3456 explicitly.

## Risks / Trade-offs

- [Upstream account does not support the new field] → Exclude it before selection/reuse and return a local capability error; do not silently strip the requested policy or mark the account unhealthy.
- [Failover reduces cache locality] → Preserve availability semantics while recording that a key moved; do not pin an unhealthy account merely for cache reuse.
- [Over-validation rejects future content blocks] → Validate a breakpoint only when the marker is present and leave unmarked JSON content untouched.
- [Explicit writes increase cost without reuse] → Report `cache_write_tokens` beside subsequent `cached_tokens` and keep no-breakpoint explicit mode available.
- [Live measurements are nondeterministic due to eviction] → Use immediate paired requests, unique keys, identical stable prefixes, and describe results as observations rather than unit-test invariants.

## Migration Plan

1. Add schema and pass-through tests without touching the running 2455/2456 services.
2. Validate the complete working tree and both active hardening changes.
3. Start an isolated backend on 3456, run contract and cache measurements, and stop it by explicit PID/port.
4. Fold this change into the existing production-gated proxy deployment only after the prior hardening audit is READY.
5. Apply the nullable request-log migration after confirming a single Alembic head. Historical rows remain `NULL` and existing costs are not rewritten.
6. Roll back application code and downgrade the single nullable column if required; no cache data itself is stored locally.

## Open Questions

- The prompt-caching guide currently says cache reads consider the latest 50 breakpoints while the Responses API reference search text says 80. The proxy will not impose a local read-lookback limit and will defer that evolving behavior to upstream.
