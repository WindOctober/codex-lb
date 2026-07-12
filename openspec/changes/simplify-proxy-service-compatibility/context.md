## Audit Summary

The current local `app/modules/proxy/service.py` contains roughly 14,000 lines. Upstream has already decomposed the same ownership area into HTTP bridge, streaming, websocket, request-log, rate-limit, compact, and support slices. The local tree also carries substantial custom routing, egress, runtime observability, and durable-continuity work, so transplanting the upstream decomposition wholesale would create a large behavioral merge risk.

This cleanup intentionally removes only symbols proven unreferenced across `app/` and `tests/`, and centralizes compatibility behavior without changing call contracts.

## Removed Legacy Categories

- Active-session balancing helpers superseded by waterline routing and explicit account/model session budgets.
- A pressure-threshold predicate no longer used by the active pressure-eviction implementation.
- Sticky-key wrappers superseded by `_resolve_prompt_cache_key` and affinity-policy constructors.
- A previous-response error override helper superseded by terminal-event rewrite paths.
- An unused response-history omission formatter and its private constant.

## Retained Controls

- Pressure eviction remains active and is not legacy.
- Account/model request, connect, and session leases remain active.
- Soft sharding and busy-parallel bridge keys remain active.
- Durable bridge ownership, lease renewal, full-resend trimming, and owner forwarding remain active.
- Retry safety distinctions between pre-created, no-text, and downstream-visible responses remain active.

## Follow-up Boundary

The next architecture change should decompose `ProxyService` in behavior-preserving stages, starting with pure support types/helpers, followed by HTTP bridge runtime observability, request submit, upstream events, streaming retry, and websocket handling. Existing module-level names should be re-exported during migration because the current test suite and operational tooling patch some of those seams.
