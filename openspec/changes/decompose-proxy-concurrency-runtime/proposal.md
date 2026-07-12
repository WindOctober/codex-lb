## Why

Account/model request, HTTP bridge connect, and HTTP bridge session concurrency policies already use focused limiter objects, but their limit lookup, saturation checks, lease acquisition/release, wait loop, and overload projection remain embedded in `ProxyService`. This obscures one cohesive local-admission responsibility and couples it to endpoint orchestration.

## What Changes

- Introduce a typed proxy concurrency runtime mixin over the existing `AccountModelConcurrencyLimiter` state.
- Move limit lookup, full-account projections, request/session/connect lease operations, bounded connect waiting, request-state release, and local overload construction out of `ProxyService`.
- Preserve separate request, bridge-connect, and bridge-session budgets and their existing limiter sharing.
- Reuse existing settings and remaining-budget compatibility capabilities so current patch points remain valid.
- Preserve method names for transport mixins and tests, and ratchet architecture ownership.

## Capabilities

### New Capabilities

- `proxy-concurrency-runtime`: Defines the internal responsibility boundary and behavior-preservation contract for local account/model concurrency budgets.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, a focused concurrency runtime module, architecture tests, and account-model/HTTP-bridge concurrency tests.
- No endpoint, setting, default limit, account selection, limiter algorithm, request budget, HTTP status, retry, or routing behavior changes.
