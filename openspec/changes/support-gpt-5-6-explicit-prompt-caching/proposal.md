## Why

GPT-5.6 adds request-level prompt-cache policy and explicit content-block breakpoints. The frozen pre-change 2455 observation returned `Unsupported parameter: prompt_cache_options`, preventing clients with large stable prefixes from controlling cache writes and measuring reuse. The proxy must expose the wire contract, route only to an upstream that actually supports it, keep repeated tenant/model/key requests local to one cache space, and account for the reported write cost without introducing response memoization.

## What Changes

- Accept and validate GPT-5.6-and-later `prompt_cache_options` with `mode` set to `implicit` or `explicit` and `ttl` set to `30m`.
- Preserve supported `prompt_cache_breakpoint: {"mode": "explicit"}` markers on Responses input content blocks and forward them unchanged.
- Keep `prompt_cache_key` unchanged on the upstream wire while deriving a versioned internal sticky key from tenant, normalized request model, and client key.
- Route cache-control requests only to eligible native Responses API-key providers and fail closed when no capable upstream exists, including reused HTTP bridge and WebSocket sessions.
- Preserve `cached_tokens` and `cache_write_tokens` in streamed and collected response usage, request logs, and API-key settlement.
- Price GPT-5.6 Sol, Terra, and Luna (including service tiers and long-context rules) and apply only the 25% cache-write uplift so write tokens are not double charged.
- Keep GPT-5.6 cache controls separate from the deprecated `prompt_cache_retention` field and from older-model request behavior.
- Add deterministic contract tests plus isolated-backend A1/A2/B1/no-breakpoint measurements for cache reads and writes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: Extend the Responses request, routing, and usage contracts for GPT-5.6 explicit prompt caching.

## Impact

- Request and usage schemas under `app/core/openai/`.
- Responses normalization and forwarding paths under `app/modules/proxy/`.
- Prompt-cache affinity, provider capability filtering, direct WebSocket reuse, and HTTP bridge reconnect behavior.
- Usage pricing, API-key reservation settlement, account cost rollups, and per-request dashboard observability.
- A nullable `request_logs.cache_write_tokens` migration; historical rows remain `NULL` because cache writes cannot be reconstructed.
- Unit/integration tests and isolated backend validation artifacts; no local prompt cache or response-memoization store is added.
