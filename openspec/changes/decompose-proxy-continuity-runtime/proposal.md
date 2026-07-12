## Why

Durable bridge account binding and previous-response owner resolution remain embedded in `ProxyService` even though both implement continuity-to-account resolution, model compatibility validation, scoped cache behavior, repository fallback, and fail-closed policy. Keeping them split across the facade obscures one critical continuity responsibility.

## What Changes

- Introduce a typed continuity runtime mixin for durable account binding and previous-response owner resolution.
- Move the bounded owner cache, scoped/general cache fallback, request-log lookup, durable account validation, and fail-closed error projection out of `ProxyService`.
- Preserve cache insertion/eviction order, disabled negative caching, session scoping, repository fallback, account cloning, active-account checks, and model support semantics.
- Re-export the existing cache-limit constant from `service.py` and preserve all inherited method names and monkeypatch surfaces.
- Ratchet architecture ownership and dependency direction.

## Capabilities

### New Capabilities

- `proxy-continuity-runtime`: Defines the internal responsibility boundary and behavior-preservation contract for resolving durable and previous-response continuity to accounts.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, a focused continuity runtime module, architecture tests, HTTP bridge continuity tests, and WebSocket/ordinary previous-response owner tests.
- No endpoint, cache size, database query, account routing, durable ownership, model compatibility, retry, or public error contract changes.
