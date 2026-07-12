## Tasks

- [x] Add OpenSpec requirement for routable HTTP bridge pressure capacity.
- [x] Reuse load-balancer eligibility data to count budget-safe routable accounts.
- [x] Pass the routable capacity hint into pressure eviction without doing DB work under the bridge lock.
- [x] Add focused regression coverage for capacity counting and pressure eviction.
- [x] Protect handed-out HTTP bridge sessions from idle, LRU, and pressure eviction until submit queues or fails.
- [x] Add regression coverage for pressure eviction racing with submit admission.
- [x] Protect handed-out HTTP bridge sessions from stale replacement and soft prompt-cache reallocation.
- [x] Fail over HTTP bridge fresh reconnect when websocket connect raises retryable `ProxyResponseError`.
- [x] Add regression coverage for stale replacement and reconnect failover races.
- [x] Reacquire missing HTTP bridge session leases during fresh reconnect before opening upstream websockets.
- [x] Serialize submit reconnect recovery with upstream-disconnect lifecycle cleanup.
- [x] Restore hard-key reconnect sessions over idle conflicting bridge sessions.
- [x] Keep previous-response owner account pinned while waiting for local bridge session capacity.
- [x] Reuse idle busy-parallel prompt-cache slots instead of returning busy 503 when all slot indexes exist.
- [x] Allow first-turn soft prompt-cache batch requests to use busy-parallel sessions.
- [x] Treat previous-response owner-unavailable stream errors as eligible for local recovery.
- [x] Prevent proxy API key validation database errors from surfacing as unhandled 500s when stale cache is usable.
- [x] Run targeted backend tests.
