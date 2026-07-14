## Context

The HTTP bridge has one long-lived reader task per upstream Responses WebSocket. Retry handling runs inside that reader and can replace the socket. The current reconnect helper always starts a new reader when `restart_reader=True`, even when the caller is the existing reader, so both tasks continue against the replacement socket. Production then reports `websockets.exceptions.ConcurrencyError` and evicts the bridge.

Session acquisition is also polled with `wait_for(shield(task))` so the downstream can receive keepalives while account selection or WebSocket creation is slow. Python 3.14 can report a later task failure through the abandoned shield future. Because a keepalive has already committed HTTP 200, propagating that failure through Starlette produces an ASGI exception rather than a valid terminal Responses event.

## Goals / Non-Goals

**Goals:**

- Keep exactly one receive owner per upstream bridge WebSocket.
- Preserve startup and recovery keepalives without creating shield-future warnings.
- Preserve HTTP error status when startup fails before any downstream event.
- Emit one valid terminal SSE failure when startup fails after a keepalive.
- Deterministically settle an abandoned session-acquisition task.

**Non-Goals:**

- Change routing, account failover policy, request budgets, or retry eligibility.
- Retry a request after it may have reached the upstream.
- Change direct non-bridge streaming or Caddy behavior.
- Solve the independent host-to-upstream network outage seen in the same log window.

## Decisions

### Poll the owned task with `asyncio.wait`

Session acquisition remains a single owned task, but heartbeat polling uses `asyncio.wait({task}, timeout=...)`. Unlike `wait_for(shield(task))`, this neither cancels the task nor creates an outer shield future whose exception can go unobserved. A done callback retrieves any exception if the downstream abandons the stream before consuming the task result; normal consumers still observe the same exception from `task.result()`.

### Convert only post-commit startup failures to SSE

Before the first keepalive, a `ProxyResponseError` continues to propagate so the API can return its HTTP status. Once a keepalive has been yielded, the HTTP status is committed; a later startup failure is converted to `response.failed` using the original upstream error code, message, type, and parameter. This avoids an invalid mid-stream exception without hiding the failure.

### Let a reconnecting reader retain ownership

The reconnect helper records whether the current task is the registered reader. External reconnect callers still cancel and await the old reader before creating a replacement. A reader-initiated reconnect replaces the socket but retains the current task as `session.upstream_reader`, so the existing loop becomes the sole reader for the new socket.

### Defer broad async-generator cleanup changes

`aclose(): asynchronous generator is already running` warnings are correlated with downstream cancellation but are not yet tied to a request failure. This change removes the duplicate-reader source and makes acquisition-task cleanup deterministic. Remaining generator-finalization warnings will be reassessed from fresh production logs before changing the API-wide streaming wrappers.

## Risks / Trade-offs

- [Risk] A reader-initiated reconnect could accidentally leave no reader if the current loop exits immediately. -> The reconnect path keeps the session open and tests verify that no replacement is spawned and the current task remains registered.
- [Risk] Converting a post-keepalive error could lose structured fields. -> The conversion preserves normalized code, message, type, and parameter from the `ProxyResponseError` envelope.
- [Risk] An abandoned acquisition task may ignore cancellation. -> Cleanup remains bounded and the done callback retrieves any eventual exception so it cannot become an unhandled loop error.
