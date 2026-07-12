## Why

API-key usage reservation, release, and transport settlement remain embedded in `ProxyService` even though they form one independent accounting responsibility. Keeping this repository-backed lifecycle beside account routing and WebSocket ownership obscures its invariants and expands the transport facade.

## What Changes

- Introduce a typed API-key usage runtime mixin with a narrow repository-factory capability protocol.
- Move WebSocket/HTTP-bridge reservation and release plus compact and stream settlement out of `ProxyService`.
- Preserve request-limit enforcement, error translation, cancellation shielding, finalize-versus-release decisions, service-tier attribution, and settlement warning behavior.
- Preserve the existing method names so transport mixins and tests retain their current call and monkeypatch surfaces.
- Ratchet architecture tests to prevent accounting lifecycle methods from returning to the service facade.

## Capabilities

### New Capabilities

- `proxy-api-key-usage-runtime`: Defines the internal responsibility boundary and behavior-preservation contract for proxy API-key usage reservation and settlement.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, a focused API-key usage runtime module, architecture tests, and API-key accounting/transport regression tests.
- No endpoint, schema, database, quota rule, reservation model, routing, retry, or externally visible error contract changes.
