## ADDED Requirements

### Requirement: Dashboard exposes HTTP bridge runtime metrics

The dashboard API MUST expose a read-only, dashboard-authenticated snapshot of the current process HTTP bridge runtime state. The snapshot MUST include pool capacity, effective available parallel capacity, free account/model session slots, reclaimable idle sessions, in-flight bridge creations, pending and queued request counts, Codex-promoted session counts, reconnect counts, and groupings by account, affinity kind, model, and prompt-cache shard family.
The snapshot MUST also include an operator-facing health summary with recent first-token latency percentiles, endpoint ping latency, seven-day success availability, and a 60-point one-minute history suitable for dashboard rendering.

#### Scenario: operator inspects bridge pool bottlenecks

- **WHEN** an authenticated dashboard client requests HTTP bridge runtime metrics
- **THEN** the service returns current process-local bridge capacity and pending work totals
- **AND** the response identifies how many additional parallel bridge sessions can be accepted from the active account pool after counting both immediately free account/model session slots and idle sessions that can be reclaimed
- **AND** the response includes per-account, per-affinity-kind, per-model, shard-family, and session sample details
- **AND** raw bridge affinity keys are represented only as stable hashes

#### Scenario: operator checks latency health

- **WHEN** an authenticated dashboard client requests HTTP bridge runtime metrics
- **THEN** the response includes recent first-token latency p50, p95, and p99 values when request log samples are available
- **AND** the response includes the dashboard endpoint ping latency measured for the snapshot request
- **AND** the response includes seven-day success availability counts and percentage
- **AND** the response includes 60 one-minute history buckets with per-bucket status values of `ok`, `warning`, `critical`, or `empty`
- **AND** the dashboard refreshes this bridge runtime snapshot no more frequently than once per minute by default
