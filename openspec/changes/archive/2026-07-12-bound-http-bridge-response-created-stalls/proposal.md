## Why

HTTP bridge requests can remain pending for the full proxy request budget when an upstream WebSocket accepts a send but never emits `response.created`. The current bridge keeps the downstream stream alive but lacks a startup-phase deadline and does not reuse its existing retry-safe full-resend state during timeout recovery, turning a recoverable upstream stall into repeated eight-minute Codex reconnects.

## What Changes

- Add a configurable startup deadline for HTTP bridge requests that have been sent upstream but have not received `response.created`.
- Let the bridge reader own startup timeout detection so upstream metadata and downstream keepalives cannot mask a missing `response.created`.
- Transparently replay one proxy-injected, prefix-verified full-resend request on a fresh upstream connection while avoiding the stalled account first.
- Preserve fail-closed behavior for client-owned continuations or any request that cannot be proven safe to replay.
- Emit phase-specific timeout diagnostics while retaining the existing total request budget for responses that have already started.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: Bound pre-created HTTP bridge stalls and define when transparent replay is safe.

## Impact

- Affected runtime: HTTP bridge request state, upstream receive timeout selection, pre-created replay, and structured bridge diagnostics.
- Affected configuration: one canonical positive timeout setting for the `response.created` startup phase.
- Affected tests: focused HTTP bridge timeout, replay-safety, metadata, and long-running response regressions.
- No API schema, database schema, Caddy configuration, or routing-strategy change.
