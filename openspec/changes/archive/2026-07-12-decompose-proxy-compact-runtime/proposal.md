## Why

`ProxyService.compact_responses` currently owns the complete compact request state machine inside the transport facade, combining affinity, account selection, refresh, admission, retry, failover, usage settlement, and request logging in one large method. Moving that cohesive orchestration behind a typed runtime boundary reduces facade coupling while preserving the already well-tested compact behavior.

## What Changes

- Add a dedicated compact runtime mixin with an explicit protocol for the service capabilities it consumes.
- Move compact request orchestration out of `service.py` without changing public method lookup or request/response contracts.
- Preserve account selection, sticky affinity, request budgets, 401 refresh, transient same-account retry, deterministic failover, API-key settlement, service-tier accounting, and request logging semantics.
- Add architecture ownership checks and run the existing compact, transient retry, sticky-session, and API-key regressions.

## Capabilities

### New Capabilities

- `proxy-compact-runtime`: Defines the internal compact orchestration boundary and the behavior that must remain stable during extraction.

### Modified Capabilities

None.

## Impact

- Affected code: `app/modules/proxy/service.py`, a new module under `app/modules/proxy/_service/`, and proxy architecture tests.
- Public HTTP routes, payloads, account routing, persistence schema, and deployment configuration remain unchanged.
- No new runtime dependency is introduced.
