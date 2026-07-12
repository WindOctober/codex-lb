## Why

`response.create` size enforcement, historical image/tool-output slimming, image-capability detection, and oversized-request diagnostics are pure payload policy but still occupy roughly four hundred lines in `ProxyService`. Keeping these helpers in the orchestration monolith couples every transport to a service implementation detail and makes the shared HTTP bridge/WebSocket request contract harder to review.

## What Changes

- Introduce one focused internal module for `response.create` payload size, slimming, image detection, and diagnostic-dump policy.
- Preserve existing `app.modules.proxy.service` helper exports for tests and compatibility callers.
- Make HTTP bridge, ordinary streaming, and WebSocket code consume the same canonical policy implementation.
- Ratchet the architecture boundary so these helpers cannot return to the service monolith.
- Preserve all payload transformations, byte thresholds, error envelopes, dump formats, and logging behavior.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: Require one typed internal boundary for shared `response.create` payload policy while preserving existing transport behavior.

## Impact

- Affected code: `app/modules/proxy/service.py`, a new internal payload-policy module, architecture tests, and focused payload/transport regressions.
- No API, schema, database, routing, account-selection, retry, timeout, SSE, or WebSocket contract changes.
