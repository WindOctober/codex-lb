## Why

HTTP bridge and downstream WebSocket orchestration share the same upstream WebSocket creation lifecycle, but its provider gate, credential resolution, connection admission, budget mapping, and socket-factory dispatch still live on `ProxyService`. Extracting that cohesive lifecycle gives the shared capability one typed owner without changing routing, failover, egress, or handshake behavior.

## What Changes

- Add a dedicated upstream WebSocket runtime mixin with an explicit protocol for service-owned encryption, admission, and the replaceable socket factory.
- Move budgeted and unbudgeted upstream WebSocket creation out of `service.py` while preserving provider checks, account-derived parameters, lease cleanup, and timeout errors.
- Keep a local `ProxyService` compatibility method that resolves the service-module `connect_responses_websocket` binding at call time and supports legacy test factories without `wire_api`.
- Keep account selection, token refresh, failover, account health, egress selection, and transport handshake behavior in their current owners.
- Add architecture ownership ratchets plus direct and consumer-level focused regressions.

## Capabilities

### New Capabilities

- `proxy-upstream-websocket-runtime`: Defines the shared typed lifecycle for opening an admitted, budgeted upstream Responses WebSocket.

### Modified Capabilities

None.

## Impact

- Affected code: `app/modules/proxy/service.py`, a new module under `app/modules/proxy/_service/`, proxy architecture tests, and focused upstream-connection tests.
- Public APIs, payloads, account routing and switching, continuity, streaming semantics, WebSocket protocols, persistence, deployment, and runtime configuration remain unchanged.
- No new dependency is introduced.
