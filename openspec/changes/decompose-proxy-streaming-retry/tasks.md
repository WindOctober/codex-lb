## 1. Streaming Decomposition

- [x] 1.1 Add canonical proxy runtime/dashboard settings adapters.
- [x] 1.2 Add a monkeypatch-compatible upstream SSE factory adapter.
- [x] 1.3 Extract streaming-only retry, suppression, timeout, and latency helpers.
- [x] 1.4 Extract `_stream_with_retry` and `_stream_once` behind a typed capability protocol.
- [x] 1.5 Preserve account, continuity, API-key, request-log, admission, and settlement behavior.
- [x] 1.6 Ratchet the proxy service architecture boundary.

## 2. Verification

- [x] 2.1 Run formatting, lint, compilation, type-boundary, and architecture checks.
- [x] 2.2 Run focused ordinary streaming, transient retry, affinity, request-log, and Responses contract tests.
- [x] 2.3 Run broader HTTP bridge and WebSocket regressions affected by shared helpers.
- [x] 2.4 Validate on an isolated backend and issue one low-cost real request before any primary restart.
- [x] 2.5 Re-check the primary backend and gateway after a backend-only restart.
- [x] 2.6 Validate OpenSpec artifacts if the CLI is available.
