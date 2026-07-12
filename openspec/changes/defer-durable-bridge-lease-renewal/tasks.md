# Tasks

- [x] 1. Add per-session heartbeat scheduling state and a lease-derived minimum interval.
- [x] 2. Replace synchronous reuse renewals with coalesced background scheduling outside the request-critical path.
- [x] 3. Preserve correctness-critical durable writes and order session release after any in-flight heartbeat.
- [x] 4. Add regression tests for non-blocking reuse, throttling, coalescing, retry after failure, and close ordering.
- [x] 5. Run targeted tests and isolated backend preflight.
- [ ] 6. Restart backend 2456 during an idle window and verify primary gateway/backend health.
