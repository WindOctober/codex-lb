## Context

The HTTP bridge sends `response.create` over a persistent upstream WebSocket and relays upstream events to an HTTP SSE client. Downstream keepalives protect long requests, but timeout selection currently distinguishes only the total request budget and generic stream-idle budget. A request that has been sent but never receives `response.created` can therefore hold bridge state until the full budget expires.

The bridge already retains an unanchored full-resend request when it injects `previous_response_id` and verifies the stored input prefix. That retained request is explicitly marked retry-safe, but the timeout replay path currently rejects every request carrying `previous_response_id` before consulting that safety state.

## Goals / Non-Goals

**Goals:**

- Bound the interval between a completed upstream send and `response.created` without shortening established long-running responses.
- Transparently recover one provably replay-safe full resend on a fresh upstream connection and avoid the stalled account first.
- Keep client-owned continuations and ambiguous multiplexed requests fail-closed.
- Produce phase-specific diagnostics and terminal errors.

**Non-Goals:**

- Retrying a request after text, tool calls, or other response-visible output.
- Providing general upstream idempotency or replaying arbitrary client `previous_response_id` continuations.
- Changing total request budgets, routing strategy, Caddy, or downstream Codex retry policy.

## Decisions

### The bridge reader owns the startup deadline

The receive timeout selector will compare the existing request/idle deadlines with the earliest startup deadline for pending requests whose upstream send completed, whose `response_id` is still absent, and which are still awaiting `response.created`. The canonical timeout starts at `http_bridge_send_completed_at`; queueing, account selection, admission, and socket-send time remain governed by their existing budgets.

Metadata such as `codex.rate_limits` does not satisfy the startup contract. Only assignment of a response ID through `response.created` removes the startup deadline.

Alternative considered: enforce the deadline in each downstream SSE generator. This creates competing owners for one upstream session and can leave the reader, gate, or sibling requests alive after the downstream generator exits.

### Startup timeout identifies exact request generations

The receive-timeout value will carry each expired request ID together with the send-completion timestamp used to compute its deadline. The reader revalidates both values before replay, and again immediately before sending, so a detached request or a newer send generation cannot be replayed by a stale timeout. Timeout settlement and bridge retirement run under the session lifecycle lock, preventing a new request from being admitted between expiration and retirement. If a timed-out request has a sibling on the same multiplexed WebSocket, the bridge retires and fails remaining siblings because a late anonymous `response.created` could otherwise be assigned to the wrong request.

Alternative considered: mark the timeout as `fail_all_pending`. That loses the phase-specific error for the request that actually stalled and hides the correlation risk.

### Transparent replay remains proof-gated and single-shot

The pre-created replay path requires exactly one pending request and at most one replay. A request with `previous_response_id` is replayable only when the ID was proxy-injected, the original unanchored full-resend request is retained, and prefix verification marked that request retry-safe. Replay uses the retained unanchored body, clears the injected anchor, reconnects with the stalled account excluded, resets the startup send timestamp, and sends once.

Client-owned continuations, session anchors without a verified full body, requests with siblings, and requests already replayed are never automatically resent.

### Keep total and idle budgets unchanged

The startup timeout defaults to 120 seconds. Once `response.created` is observed, existing `proxy_request_budget_seconds` and `stream_idle_timeout_seconds` remain authoritative so long reasoning is not mistaken for startup failure.

## Risks / Trade-offs

- [An upstream request may have been accepted despite no `response.created`] -> Replay is restricted to a pre-visible, proxy-injected full resend; this can consume duplicate upstream compute but cannot duplicate an already delivered tool side effect.
- [A late response arrives after one multiplexed request times out] -> Retire the entire bridge rather than risk assigning that response to a sibling.
- [A legitimate upstream startup exceeds 120 seconds] -> Surface `response_created_timeout` and let Codex retry; keep the setting configurable and separate from long-response budgets.
- [A replay loses prompt-cache locality by changing accounts] -> Prefer correctness and recovery; only the stalled account is excluded for the first retry.

## Migration Plan

1. Add the positive timeout setting with a 120-second default.
2. Deploy request-state, timeout-selection, targeted-settlement, and replay changes together.
3. Validate with focused unit tests and an isolated backend before restarting only the primary backend.
4. Roll back the code if startup failures increase; no database or API migration is required.

## Open Questions

None.
