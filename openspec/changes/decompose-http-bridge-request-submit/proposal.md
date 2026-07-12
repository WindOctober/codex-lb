## Why

HTTP bridge request submission, admission cleanup, request detachment, prewarm, replay, and reconnect behavior are embedded in the proxy orchestration service. The shared state and key operations now have independent module ownership, so request-submit behavior can be decomposed without importing the service monolith.

## What Changes

- Introduce a typed HTTP bridge request-submit mixin.
- Move submit cleanup and detachment first, then migrate submission, prewarm, and replay operations in verified slices.
- Move only directly owned pure helpers needed by the request-submit boundary.
- Preserve method names through `ProxyService` inheritance and preserve service-module helper exports.

## Impact

- Affected code: `app/modules/proxy/service.py`, `app/modules/proxy/_service/http_bridge/request_submit.py`, shared support helpers, focused bridge tests.
- No API, database, routing, account selection, queue, admission, replay, or error-contract changes.
