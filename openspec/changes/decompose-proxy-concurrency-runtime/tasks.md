## 1. Concurrency Runtime Extraction

- [x] 1.1 Add a typed concurrency runtime mixin over the existing request/connect and session limiters.
- [x] 1.2 Move limit lookup, saturation projection, and ordinary/connect/session lease acquisition without semantic changes.
- [x] 1.3 Move bounded connect waiting, request-state lease release, and local overload construction.
- [x] 1.4 Inherit the mixin from `ProxyService`, remove local methods, and retain limiter initialization and compatibility capabilities.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.

## 2. Verification

- [x] 2.1 Run the complete account-model concurrency limiter and service suite.
- [x] 2.2 Run representative ordinary streaming, HTTP bridge capacity/session, and WebSocket finalization tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.
