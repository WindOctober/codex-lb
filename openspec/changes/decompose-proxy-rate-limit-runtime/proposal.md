## Why

Rate-limit header construction, dashboard payload aggregation, usage refresh, latest-model substitution, and additional-quota projection remain embedded in `ProxyService` even though they form an independent read-model responsibility. This keeps repository-heavy reporting logic coupled to request routing and transport orchestration.

## What Changes

- Introduce a typed rate-limit runtime mixin and narrow service capability protocol.
- Move cached header construction, dashboard payload construction, usage refresh, usage-row lookup, and additional-quota projection out of `ProxyService`.
- Preserve the existing public service methods and inherited monkeypatch behavior.
- Preserve all account selection, latest-model quota substitution, credit, reset-time, and additional-limit semantics.
- Ratchet architecture tests to prohibit reintroduction into the service facade.

## Capabilities

### New Capabilities

- `proxy-rate-limit-runtime`: Defines the internal responsibility boundary and behavior-preservation contract for proxy rate-limit read models.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, a focused internal rate-limit module, architecture tests, rate-limit/additional-quota unit tests, and proxy API integration tests.
- No endpoint, schema, database, refresh cadence, routing, account state, header, or dashboard payload changes.
