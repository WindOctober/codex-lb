## Why

Operators can currently infer HTTP bridge bottlenecks from logs such as `busy_recreate_deferred`, but the dashboard does not expose the in-memory bridge pool state that drives those failures. This makes it hard to confirm whether prompt-cache shards, Codex-promoted sessions, pending requests, or account concentration are limiting concurrency.

## What Changes

- Add a dashboard-authenticated read-only HTTP bridge runtime metrics endpoint.
- Expose bridge pool capacity, pending/queued work, Codex-promoted sessions, shard distribution, and account/model/affinity groupings.
- Add a dashboard panel that refreshes with the existing dashboard view so operators can see whether bridge load is spreading after routing changes.

## Impact

- No durable schema changes.
- No proxy routing behavior changes.
- Runtime metrics are process-local because HTTP bridge sessions live in memory.
