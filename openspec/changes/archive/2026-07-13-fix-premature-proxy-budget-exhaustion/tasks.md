## 1. Timeout Semantics

- [x] 1.1 Classify direct WebSocket handshake timeout as a retryable first-event failure while preserving the streaming status contract and true total-budget behavior.
- [x] 1.2 Distinguish outer request-budget cancellation from a nested bridge WebSocket timeout.
- [x] 1.3 Add focused tests for direct error propagation, nested timeout propagation, true deadline exhaustion, and admission cleanup.

## 2. Safe HTTP Bridge Failover

- [x] 2.1 Retry ordinary pre-send connection failures on another eligible account within existing bounds.
- [x] 2.2 Preserve required-account continuity and add failover/bounded-failure regression tests.

## 3. Validation And Deployment

- [x] 3.1 Run focused proxy tests, static checks, and OpenSpec validation.
- [x] 3.2 Validate a backend-only instance on port 3456, including a low-cost Responses request.
- [x] 3.3 Restart only backend port 2456, verify both health endpoints, and monitor error classification.
