## Why

HTTP bridge upstream receive, timeout, terminal matching, previous-response recovery, event forwarding, and settlement are embedded in the proxy orchestration service. Their pure event operations and request state now have canonical module ownership, so the receive state machine can have an explicit boundary without importing the service monolith.

## What Changes

- Introduce a typed HTTP bridge upstream-events mixin.
- Move the upstream receive loop and event processing state machine out of `ProxyService`.
- Consolidate WebSocket error parsing, request matching, previous-response rewriting, and failover classification in a shared event module.
- Consolidate bridge latency and continuity observability in the observability module.
- Preserve inherited service method names and tested compatibility adapters.

## Impact

- Affected code: proxy service, HTTP bridge upstream-events, shared WebSocket event helpers, observability helpers, and focused tests.
- No API, database, routing, queue, account-selection, continuity, event, or error-contract changes.
