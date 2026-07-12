## Why

Request-log persistence and stream preflight error projection remain embedded in `ProxyService` even though they form an independent, repository-backed observability responsibility. Their current placement also makes a generic session-ID normalizer appear to belong to WebSocket event handling, creating an avoidable dependency from HTTP and logging paths into the WebSocket module.

## What Changes

- Introduce a typed request-logging runtime mixin with a narrow repository-factory capability protocol.
- Move request-log persistence, stream preflight error logging, and completed-request model rewriting out of `ProxyService` without changing method signatures, retry timing, or log fields.
- Move session-ID normalization to the shared affinity boundary while preserving the former WebSocket import path as a compatibility re-export.
- Preserve cancellation shielding, persistence failure isolation, latency calculation, and transport defaults.
- Ratchet architecture tests to prevent request-log persistence from returning to the service facade.

## Capabilities

### New Capabilities

- `proxy-request-logging-runtime`: Defines the internal responsibility boundary and behavior-preservation contract for proxy request-log persistence.

### Modified Capabilities

None.

## Impact

- Affected code: proxy service, affinity helpers, WebSocket event compatibility imports, a focused request-logging module, architecture tests, and request-log/transport regression tests.
- No endpoint, schema, database, routing, account-selection, retry, settlement, or externally visible request-log contract changes.
