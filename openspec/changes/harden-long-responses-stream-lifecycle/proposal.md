## Why

Long `gpt-5.6-sol` Responses turns are being terminated at exactly the general 480-second proxy budget even after upstream has created a response, while upstream codex-lb already assigns HTTP Responses and HTTP bridge turns a dedicated 7200-second budget. Fresh production logs also confirm an unretrieved `aclose(): asynchronous generator is already running` failure when nested public stream wrappers are finalized after downstream cancellation.

## What Changes

- Add dedicated long-turn budgets for HTTP Responses streams and HTTP bridge streams, leaving the general proxy budget responsible for shorter non-Responses work and connection/retry bounds.
- Use the bridge-specific budget for local bridge receive deadlines and owner-forwarded bridge streams.
- Preserve each bridge request's original hard deadline across reconnect/replay and expire only the request whose deadline elapsed on a shared bridge.
- Make each public async stream wrapper deterministically close the child iterator it owns when normal completion, cancellation, or generator close ends the wrapper, including while the downstream cancellation scope is active.
- Preserve the most recent HTTP bridge connection failure when account failover attempts fail in different ways.
- Add regression tests for budget selection, bridge receive deadlines, lease lifetime, and nested stream cancellation cleanup.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: HTTP Responses and bridge turns use explicit long-turn budgets rather than the general proxy request budget.
- `proxy-runtime-observability`: downstream stream cancellation completes nested iterator cleanup without unretrieved async-generator close failures.
- `proxy-upstream-websocket-runtime`: bridge startup reports the final failed connection attempt after mixed account failures.

## Impact

Affected code is limited to runtime settings, HTTP Responses budget selection, bridge owner forwarding, receive deadlines and reconnect accounting, public Responses stream wrappers, connection-failure bookkeeping, and focused tests. Endpoint shapes, account routing order, Caddy, and post-send retry safety do not change.
