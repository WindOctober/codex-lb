## Why

HTTP bridge request preparation, affinity-key construction, durable continuation recovery, session acquisition, submission, keepalive streaming, and terminal cleanup remain interleaved in one large `ProxyService` method. The method is difficult to review as a single state machine and keeps pure bridge policy coupled to the proxy service monolith.

## What Changes

- Introduce canonical modules for HTTP bridge stream policy and stream orchestration.
- Move affinity-independent bridge key, resend, continuation recovery, and timeout policy out of the proxy service.
- Move the HTTP bridge request streaming state machine behind a typed capability boundary while retaining the existing inherited method name.
- Keep tested service-module facade names and settings injection points explicit.
- Ratchet the proxy service architecture limit after extraction.

## Impact

- Affected code: proxy service, affinity, observability, HTTP bridge keys/runtime/stream policy/stream orchestration, architecture tests, and focused bridge tests.
- No API, routing, account-selection, durable continuity, timeout, keepalive, SSE, WebSocket, request-budget, or error-contract changes.
