## Boundaries

Connection owns account selection for one upstream WebSocket, budget checks, token freshness, one forced refresh after 401, and deterministic account failover.

Relay owns receiving upstream frames, matching frames to pending requests, normalizing errors, retrying only precreated requests when safe, emitting downstream frames, and terminal settlement.

Orchestration owns the downstream WebSocket lifetime, request preparation, client receive/send coordination, task cancellation, and final cleanup.

Repositories, load-balancer mutation, API-key accounting, durable owner lookup, request-log persistence, and upstream socket creation remain explicit service capabilities where they are shared with other transports.

## Compatibility

`ProxyService` inherits all extracted methods. Settings, remaining-budget, continuity metrics, and upstream socket factory replacements remain explicit adapters. Extracted modules never import or dynamically inspect `app.modules.proxy.service`.

## Failure Modes

- A selected-model capacity or account rate-limit failure MUST rotate accounts while retaining the requested model.
- A previous-response owner MUST remain fail-closed when unavailable.
- A 401 MUST perform at most the existing forced-refresh retry before surfacing or failing over.
- Cancellation or disconnect MUST release admission, API-key, response-create, and account-concurrency leases exactly once.

## Example

When the first account rejects an upstream WebSocket handshake with a retryable selected-model capacity error, connection policy records the same account-health result, excludes that account, and opens the next eligible account for the same model. Relay and client orchestration remain unchanged by that decision.
