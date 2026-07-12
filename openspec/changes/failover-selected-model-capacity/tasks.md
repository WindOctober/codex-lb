## 1. Capacity Error Failover

- [x] 1.1 Classify the selected-model capacity upstream message as a rate-limit failure and `server_is_overloaded` as retryable transient capacity pressure.
- [x] 1.2 Retry HTTP bridge pre-created requests on a fresh upstream when that failure arrives before downstream visibility.
- [x] 1.3 Retry direct stream and websocket pre-created requests on a fresh upstream when that failure arrives before downstream visibility.
- [x] 1.4 Add regression coverage for classification, bridge account failover, direct stream account failover, websocket pre-created replay, and server-overloaded capacity failover.

## 2. Verification

- [x] 2.1 Run targeted unit and integration tests.
- [ ] 2.2 Run OpenSpec validation if the CLI is available. (`openspec` is not installed in PATH or `.venv/bin`.)
