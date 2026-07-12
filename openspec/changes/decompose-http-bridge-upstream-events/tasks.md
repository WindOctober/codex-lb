## 1. Upstream Event Decomposition

- [x] 1.1 Extract canonical WebSocket event parsing and request matching helpers.
- [x] 1.2 Extract continuity and bridge latency observability helpers.
- [x] 1.3 Add a typed upstream-events mixin and service capability protocol.
- [x] 1.4 Move the HTTP bridge receive loop and event processor.
- [x] 1.5 Preserve required service-module compatibility patch points.

## 2. Verification

- [x] 2.1 Add focused event matching and architecture tests.
- [x] 2.2 Run HTTP bridge, streaming, WebSocket, and continuity regressions.
- [x] 2.3 Run formatting, lint, compilation, architecture, and diff checks.
- [x] 2.4 Validate on an isolated backend before restarting the primary backend.
- [x] 2.5 Validate OpenSpec artifacts if the CLI is available.
- [x] 2.6 Finalize terminal request state before closing the downstream event queue.
