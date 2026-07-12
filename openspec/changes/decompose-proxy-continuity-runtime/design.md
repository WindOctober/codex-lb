## Context

Two account-continuity mechanisms remain in `ProxyService`. HTTP bridge durable lookup validates that a persisted account is still active and supports the requested model. Ordinary, HTTP bridge, and WebSocket previous-response requests resolve an owner through a bounded in-memory cache followed by request-log lookup, with scoped fallback and fail-closed behavior.

Both mechanisms depend on the repository factory and produce an account binding used by downstream routing. They share continuity observability and must preserve account ownership rather than silently reallocating a continuation.

## Goals / Non-Goals

**Goals:**

- Move durable account validation and previous-response owner resolution behind one typed continuity boundary.
- Preserve cache state ownership in `ProxyService.__init__` and method names through inheritance.
- Preserve session-scoped and unscoped cache keys, insertion order, bounded eviction, repository lookup arguments, fallback behavior, account cloning, and fail-closed errors.
- Keep the cache-size constant compatible through a service-module re-export.

**Non-Goals:**

- Change durable owner election, sticky-session routing, or account selection.
- Add negative caching, cache persistence, TTLs, or a different eviction algorithm.
- Change request-log schemas or continuity metrics.
- Merge upstream error handling or account health mutation into this module.

## Decisions

### Use one `_ContinuityRuntimeMixin`

Durable binding and previous-response lookup are distinct inputs to the same continuity-to-account decision. One module makes their repository and fail-closed invariants explicit while avoiding two tiny modules.

Alternative considered: place durable binding in HTTP bridge stream and owner cache in WebSocket events. That would duplicate continuity ownership across transport-specific modules even though ordinary HTTP also consumes previous-response ownership.

### Keep the cache dictionary on the service instance

The extraction moves cache operations but not initialization, lifetime, or object identity. This avoids migration of live state and keeps existing introspection tests valid.

### Keep negative caching disabled

`_remember_websocket_previous_response_owner_miss()` remains an intentional no-op because concurrent sessions can make misses stale. This policy is documented and tested rather than removed as apparently dead code.

### Import canonical observability directly

The continuity module records owner lookup and fail-closed outcomes through the canonical observability helpers. It does not import the service facade; unrelated service-level wrappers remain for existing HTTP bridge forwarding paths.

## Risks / Trade-offs

- [Risk] Scoped lookup falls back to the wrong account. -> Test scoped hit, general fallback, DB hit/miss, and DB failure separately.
- [Risk] Cache eviction order changes. -> Preserve pop-and-reinsert semantics and test the exact oldest key removed at the configured limit.
- [Risk] ORM accounts escape a closed session. -> Retain cloning before leaving the repository context and run the detached-account test.
- [Risk] Repository failure becomes a silent reallocation. -> Preserve cached fallback only when present; otherwise emit observability and raise the same 502 fail-closed envelope.

## Migration Plan

1. Add the typed continuity module and move the cache constant, helper envelope, durable binding, and owner cache/lookup methods.
2. Re-export the constant, inherit the mixin, remove local implementations, and keep cache initialization unchanged.
3. Ratchet architecture ownership and run focused HTTP bridge, ordinary, and WebSocket continuity tests.
4. Run static and isolated-backend verification without touching the primary backend or Caddy.

Rollback is a source-level move back into `service.py`; no persisted state or configuration migration is involved.

## Open Questions

None.
