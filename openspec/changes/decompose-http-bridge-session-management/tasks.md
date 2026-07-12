## 1. Session Management Decomposition

- [x] 1.1 Extract canonical pure session and capacity policy helpers.
- [x] 1.2 Extract registry and durable lifecycle operations behind a typed capability protocol.
- [x] 1.3 Extract capacity accounting, reclamation, and shard selection behind a typed capability protocol.
- [x] 1.4 Extract single-session creation and resource cleanup.
- [x] 1.5 Decompose session acquisition, reuse, alias resolution, and in-flight creation coordination.
- [x] 1.6 Retain only required service-level compatibility adapters.

## 2. Verification

- [x] 2.1 Run focused architecture, concurrency, policy, and HTTP bridge tests against the existing baseline.
- [x] 2.2 Run complete HTTP bridge, streaming, WebSocket, continuity, and integration regressions.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate on an isolated backend and issue one low-cost real request before restarting the primary backend.
- [x] 2.5 Re-check primary backend and gateway health after a backend-only restart.
- [x] 2.6 Validate OpenSpec artifacts if the CLI is available.
- [x] 2.7 Keep bootstrap-registry unknown-model reuse consistent with fresh account selection.
- [x] 2.8 Release durable ownership before asynchronously closing a replaced same-key session.
