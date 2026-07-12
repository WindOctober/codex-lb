## Why

The immutable HTTP bridge runtime contracts and pure aggregation already live in `http_bridge/runtime.py`, but `ProxyService` still owns all state collection, health repository access, capacity account lookup, and egress projection. This leaves several hundred lines of dashboard-only observability orchestration inside the request transport facade.

## What Changes

- Introduce a typed HTTP bridge runtime collection mixin over the existing pure runtime model.
- Move live request status lookup, per-account runtime collection, complete dashboard snapshot collection, latency health loading, account capacity loading, and egress projection out of `ProxyService`.
- Preserve lock scope, immutable observations, grouping inputs, fallback behavior, health classification, sampling, and all serialized fields.
- Preserve existing service method names and runtime type compatibility exports.
- Ratchet architecture tests to prevent stateful runtime collection from returning to the service facade.

## Capabilities

### New Capabilities

- `http-bridge-runtime-collection`: Defines the internal state-collection boundary and behavior-preservation contract for HTTP bridge runtime observability.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, a focused HTTP bridge runtime collection module, architecture/runtime tests, dashboard API tests, and account runtime consumers.
- No endpoint, schema, database, bridge lifecycle, account selection, routing, locking, health threshold, or dashboard contract changes.
