## Why

After canonical payload policy and transport orchestration were extracted, `ProxyService` still owns the shared `response.create` request-state construction, serialization, slimming, size enforcement, and two-layer admission cleanup used by HTTP bridge and WebSocket paths. Moving this cohesive preparation lifecycle behind a typed runtime boundary removes cross-transport implementation detail from the facade without changing request behavior.

## What Changes

- Add a dedicated response-create runtime mixin with an explicit protocol for service-owned admission and compatibility seams.
- Move HTTP bridge request preparation, shared bridge request-state construction, and response-create admission acquisition out of `service.py` as one responsibility.
- Preserve request IDs, metadata, fingerprints, service-tier fields, payload serialization and slimming, size/dump thresholds, gate ordering, admission cleanup, and monkeypatch behavior.
- Keep dynamic service adapters for replaceable size-enforcement constants and prohibit the extracted runtime from importing the service facade.
- Add architecture ownership ratchets and run focused preparation, oversized-payload, admission, HTTP bridge, and WebSocket regressions.

## Capabilities

### New Capabilities

- `proxy-response-create-runtime`: Defines the internal request-preparation and admission boundary shared by HTTP bridge and WebSocket response creation.

### Modified Capabilities

None.

## Impact

- Affected code: `app/modules/proxy/service.py`, a new module under `app/modules/proxy/_service/`, proxy architecture tests, and focused response-create/transport tests.
- Public routes, payload contracts, account routing, continuity, persistence, deployment, and runtime configuration remain unchanged.
- No new runtime dependency is introduced.
