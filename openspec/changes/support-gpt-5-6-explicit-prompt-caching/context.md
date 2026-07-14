# GPT-5.6 Explicit Prompt Caching Context

## Purpose and Scope

This change exposes the GPT-5.6 prompt-prefix cache controls through the OpenAI-compatible Responses proxy. It covers request validation and pass-through, stable upstream account affinity, response usage preservation, and a controlled cache-read/write measurement. It does not add a local cache and does not return an earlier model response.

## Decisions and Rationale

- `prompt_cache_options.mode="explicit"` is the recommended Semia policy because member and iteration suffixes change frequently; only marked stable prefixes should incur cache writes.
- The bounded sticky-session table remains the routing source of truth, but its internal key is `prompt-cache:v2:<digest(tenant, normalized-model, client-key)>`, where model normalization trims surrounding whitespace and lower-cases the value. The client key is never rewritten on the upstream wire. This prevents two API-key tenants or two model variants from sharing an account binding accidentally.
- Requests that contain `prompt_cache_options` or a breakpoint require a native Responses-capable API-key provider. ChatGPT OAuth is not assumed to support the Platform contract. A reused OAuth WebSocket/HTTP bridge cannot bypass this gate.
- `prompt_cache_options` and `prompt_cache_breakpoint` are controls for versioned GPT models at GPT-5.6 or later. They are not translated into `prompt_cache_retention`; the fields express different policies.
- Cache metrics remain upstream-authored usage fields. The proxy preserves them but does not synthesize hits or writes.
- `cache_write_tokens` is an input-token classification. It receives the GPT-5.6 1.25x write rate by adding only a 25% uplift to the ordinary uncached-input charge; it is not added again to token quotas.
- Historical request logs keep `cache_write_tokens=NULL`. Zero means an observed zero write, while `NULL` means the old row did not record the metric.

## Constraints and Failure Modes

- `prompt_cache_options.mode` accepts only `implicit` and `explicit`; `ttl` accepts only `30m`. Omitted members are valid, but an explicit JSON `null` is rejected.
- A content breakpoint accepts only `mode="explicit"` and is valid only on supported Responses content blocks.
- `input_file.file_id` remains unsupported because it is scoped to the upstream project that owns the upload and the proxy has no file-owner map. Routable `file_url`/`file_data` blocks retain breakpoints; native capability alone is not enough to choose the correct file-owning provider.
- Message-compatibility coercion must preserve markers on representable user content. It rejects markers on system/developer messages that would otherwise be flattened into `instructions`, and validation runs before sanitization so unsupported marked blocks cannot disappear silently.
- Explicit mode without any breakpoint must remain a valid request and must not silently add a breakpoint.
- Older model routes must not receive GPT-5.6-only controls through a compatibility rewrite.
- API-key model enforcement is applied after initial request parsing, so a key that forces a GPT-5.6 request onto an older model must reject the incompatible controls instead of forwarding them after mutation.
- A shared tenant/model/key that is routed to different upstream accounts loses cache locality even when the payload is valid; account failover may intentionally trade locality for availability. When Codex session affinity is disabled, an explicit client key takes precedence over generated per-WebSocket turn-state.
- Sticky rows created before the `prompt-cache:v2` namespace are not reused. The first request after upgrade performs a cold rebind, which is safer than cross-tenant/model reuse.
- Oversized historical inline images may be replaced by an omission notice for upstream WebSocket limits; when the image block carried a breakpoint, the notice retains the marker so the cache boundary cannot disappear silently.
- Cacheable prefixes must be at least 1,024 rendered tokens upstream. Tests of pass-through can use small fixtures, but live cache measurements must exceed that threshold.
- Cache writes cost more than ordinary uncached input, so a write without later reads is not an optimization.
- Hidden retry and failover attempts can incur usage even when their terminal is not shown downstream. Their token classifications must be aggregated and their costs computed at each attempt's own effective tier; an all-failed request with authoritative usage must not release that incurred charge.
- GPT-5.6 pricing aliases are explicit for Sol/Terra/Luna and their snapshots. Unknown future minors such as GPT-5.7 do not silently fall back to legacy GPT-5 pricing.
- An unknown model is not free under an API-key cost limit: reservation settlement retains a conservative two-dollar quota charge per authoritative attempt while request-log price remains unknown.

## Concrete Example

```json
{
  "model": "gpt-5.6-sol",
  "prompt_cache_key": "semia:s2:rules-v3:shard-03",
  "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
  "input": [
    {
      "role": "system",
      "content": [
        {
          "type": "input_text",
          "text": "A stable schema and checker prefix of at least 1,024 tokens.",
          "prompt_cache_breakpoint": {"mode": "explicit"}
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "Dynamic iteration facts."}
      ]
    }
  ]
}
```

## Validation and Operations

- A1 writes a stable system plus skill-A prefix.
- A2 reuses the same key and prefix with a different dynamic suffix and should read cached tokens.
- B1 uses a different skill prefix and may reuse only the earlier global prefix when that prefix has its own breakpoint.
- Explicit mode without breakpoints should report zero cache reads and writes.
- At 96-way concurrency, clients should deterministically distribute members over at least eight stable keys to keep each key near the documented 15 requests/minute guidance.
- Record status, selected model, `cached_tokens`, `cache_write_tokens`, latency, and the selected upstream account identifier when available without exposing credentials.

### Isolated live observation (2026-07-14)

- The then-current working-tree 3456 backend accepted and serialized the new request fields, proving the local request contract and pass-through. The earlier 2455 end-to-end rejection did not by itself identify the rejecting layer.
- The currently available `chatgpt.com/backend-api/codex/responses` OAuth upstream rejected both direct HTTP and WebSocket-derived requests with `Unsupported parameter: prompt_cache_options`; therefore it produced no explicit-cache usage to compare.
- A forced-HTTP `prompt_cache_key`-only control succeeded, but two immediate 5,223-input-token requests using the same key each reported `cached_tokens=0` and `cache_write_tokens=0` (0/2 observed reads).
- The proxy does not strip the unsupported fields, synthesize usage, or translate them to `prompt_cache_retention`. The current implementation now fails such requests locally unless a capable native Responses provider is eligible. A live explicit-cache rate remains unmeasurable on the available OAuth pool; the recorded 0/2 control is a historical baseline, not evidence that the new provider gate is ineffective.
- The credential-free measurement record is stored under `var/codex-background/gpt56-prompt-cache-measurement.json` and the isolated backend was stopped after collection.
