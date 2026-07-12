# defer-durable-bridge-lease-renewal

## Why
HTTP bridge session reuse currently renews the durable ownership lease synchronously while holding the global bridge lock. The renewal performs a database read, commit, and refresh, so storage latency can delay the current request and serialize unrelated bridge lookups behind the same lock.

## What Changes
- Keep the in-memory bridge session reuse path free of synchronous durable heartbeat I/O.
- Schedule durable lease heartbeats in the background with a per-session minimum interval.
- Coalesce concurrent heartbeat requests so one session has at most one renewal in flight.
- Preserve synchronous persistence for turn-state aliases and completed response metadata.
- Settle an in-flight heartbeat before releasing a durable session lease.

## Impact
- Affected code: HTTP bridge session lifecycle and targeted bridge tests.
- Affected APIs: no public API or schema change.
- Operational impact: lower bridge-lock contention and lower request-path tail latency during database stalls.
