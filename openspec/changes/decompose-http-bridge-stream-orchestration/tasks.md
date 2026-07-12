## 1. Stream Policy Decomposition

- [x] 1.1 Move prompt-cache affinity policy to the canonical affinity module with explicit settings injection.
- [x] 1.2 Move bridge key, resend, request-stage, timeout, and recovery policy to focused bridge modules.
- [x] 1.3 Move request-shape observability to the canonical observability module with explicit settings injection.
- [x] 1.4 Retain only required service-level compatibility facades.

## 2. Stream Orchestration Decomposition

- [x] 2.1 Extract `_stream_via_http_bridge` behind a typed capability protocol.
- [x] 2.2 Preserve owner forwarding, durable continuation, API-key reservation, request submission, SSE keepalive, recovery, and cleanup behavior.
- [x] 2.3 Ratchet the proxy service architecture boundary and prohibit reintroduction.

## 3. Verification

- [x] 3.1 Run formatting, lint, compilation, type-boundary, and architecture checks.
- [x] 3.2 Run focused and complete HTTP bridge regressions against the existing baseline.
- [x] 3.3 Run ordinary streaming and WebSocket regressions affected by shared affinity and observability helpers.
- [x] 3.4 Validate on an isolated backend and issue one low-cost real request before any primary restart.
- [x] 3.5 Re-check the primary backend and gateway after a backend-only restart.
- [x] 3.6 Validate OpenSpec artifacts if the CLI is available.
