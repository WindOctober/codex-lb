# Design

## Current Behavior
Live bridge-session reuse updates `last_used_at` and then awaits a durable lease renewal while `_http_bridge_lock` is held. The durable renewal opens a database session and commits an updated lease. Any slow commit therefore extends both the current request's session lookup and the lock wait of unrelated requests.

## Decision
Treat ordinary lease renewal as a coalesced heartbeat rather than request-critical persistence.

- A reuse updates `last_used_at` immediately and schedules a heartbeat without awaiting it.
- Each session tracks the next time at which a heartbeat may be scheduled and its current heartbeat task.
- The scheduling interval is one third of the durable lease TTL. With the current 30-second lease this is 10 seconds, leaving two missed intervals before expiry while preventing per-request writes.
- A session can have at most one heartbeat task in flight. Reuses during that task or before the next eligible time do not create more work.
- Durable claim, turn-state alias persistence, and completed-response persistence remain synchronous because they carry correctness-critical continuity metadata.
- Successful correctness-critical persistence also advances the heartbeat deadline because those writes renew the same lease.
- Session close waits for an existing heartbeat task before releasing the durable lease. Epoch fencing remains the persistence-level backstop, but ordering the operations avoids an unnecessary renew/release race.

## Failure Handling
Heartbeat failures remain non-fatal and are logged. The scheduling deadline is advanced when an attempt is created so a failing database is not hammered by every request. A later reuse after the interval can schedule another attempt.

## Non-Goals
- No database migration.
- No change to lease TTL or ring membership timing.
- No asynchronous handling of response IDs, input fingerprints, or turn-state aliases.
- No Caddy or gateway change.
