## Why

High-concurrency Codex workloads can concentrate many HTTP Responses requests onto one HTTP bridge session through prompt-cache or session affinity. The current bridge applies two local per-session limits: an 8-request queue cap and a single in-flight `response.create` gate. Under burst load those local controls produce `HTTP responses session bridge queue is full` and request-budget timeouts even when upstream accounts are still serving other requests.

## What Changes

- Make HTTP bridge per-session queue admission unlimited by default.
- Stop serializing HTTP bridge `response.create` sends behind a per-session gate.
- Shard fresh soft prompt-cache HTTP bridge traffic across multiple bridge sessions once a shard has pending work.
- Preserve an operator escape hatch: positive queue-limit configuration values still enforce a bounded queue.
- Keep broader process safety controls such as global work admission, bulkheads, request budgets, and upstream timeouts unchanged.

## Impact

- High concurrency against one prompt-cache/session bridge key no longer fails locally solely because the per-session bridge queue has eight pending requests.
- Fresh prompt-cache bursts are spread across multiple upstream websockets instead of concentrating all pending requests on one bridge session.
- Requests may now create more simultaneous bridge sessions and pending bridge states, so operators should monitor memory, upstream stream stability, active bridge session count, and `pending_request_count`.
