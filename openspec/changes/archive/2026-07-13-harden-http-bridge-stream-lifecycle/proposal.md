## Why

Recent production logs show that HTTP bridge recovery can start two readers on the same upstream WebSocket, while session-acquisition keepalives can commit an HTTP 200 before a later connection failure escapes as an ASGI exception. These races evict otherwise reusable bridges, trigger client reconnects, and emit misleading unretrieved-task errors during upstream outages.

## What Changes

- Preserve exactly one receive owner when an HTTP bridge reconnect is initiated by its current reader.
- Replace shield-wrapped session-acquisition polling with task polling that does not create abandoned shield futures.
- After a keepalive has committed a streaming response, convert a later bridge-startup failure into a terminal Responses SSE event instead of raising through Starlette.
- Add regression coverage for reader-owned reconnects, startup failure after keepalive, task cleanup, and client-visible error semantics.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `responses-api-compat`: Define terminal stream behavior when HTTP bridge startup fails after a keepalive.
- `proxy-upstream-websocket-runtime`: Require one receive owner across reader-initiated bridge reconnects.
- `proxy-runtime-observability`: Prevent expected startup failure handling from producing duplicate ASGI and unretrieved-task errors.

## Impact

The change affects HTTP bridge stream orchestration, bridge reconnection, and focused proxy tests. It does not change public request schemas, persistence, routing policy, Caddy configuration, or direct non-bridge streaming.
