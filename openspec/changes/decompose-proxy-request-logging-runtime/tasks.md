## 1. Request-Logging Runtime Extraction

- [x] 1.1 Move session-ID normalization to the shared affinity boundary and preserve the WebSocket compatibility import.
- [x] 1.2 Add a typed request-logging mixin and repository-factory capability protocol.
- [x] 1.3 Move request-log persistence and stream preflight error projection without changing contracts.
- [x] 1.4 Inherit the mixin from `ProxyService` and remove local method bodies while preserving method lookup.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.
- [x] 1.6 Move completed-request model rewriting into the request-logging runtime without changing retry behavior.

## 2. Verification

- [x] 2.1 Run focused session normalization, request-log repository, and service behavior tests.
- [x] 2.2 Run representative HTTP, HTTP bridge, and WebSocket request-log path tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
- [x] 2.6 Run focused rewrite retry/no-op/failure tests and revalidate the expanded change.
