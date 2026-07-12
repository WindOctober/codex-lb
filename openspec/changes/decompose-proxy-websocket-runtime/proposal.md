## Why

Responses WebSocket client orchestration, account selection, upstream connection failover, downstream relay, event settlement, and request lifecycle remain spread across a large set of methods inside `ProxyService`. The connection, relay, and orchestration state machines cannot currently be reviewed or tested as independent responsibilities.

## What Changes

- Introduce typed WebSocket connection, relay, and orchestration implementation boundaries.
- Move account selection, connection budget, 401 refresh, and deterministic failover into a connection module.
- Move upstream message relay, event matching, request finalization, and terminal emission into a relay module.
- Move client WebSocket lifecycle and request preparation into an orchestration module.
- Preserve tested settings, continuity, metrics, and upstream connection adapters.
- Ratchet the proxy service architecture limit after each slice.

## Impact

- Affected code: proxy service, WebSocket internal modules, architecture tests, WebSocket unit/integration tests, and shared account helpers.
- No API, model, routing, affinity, account-selection, retry, timeout, continuity, WebSocket frame, request-log, or usage-settlement changes.
