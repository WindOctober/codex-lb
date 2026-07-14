## Context

Image-capable requests bypass the durable HTTP bridge and use the direct Responses WebSocket client. That client independently applies the shorter `upstream_connect_timeout_seconds` handshake timeout, but its raw `TimeoutError` is currently handled as exhaustion of the total request timeout. Production failures therefore terminate after about 20 seconds despite a 480-second request budget. Because the client emits a terminal SSE event instead of raising the pre-first-event transport failure, the account failover layer cannot retry it.

The shared HTTP bridge WebSocket runtime separately wraps connection creation in an AnyIO request-budget scope. Nested socket or replacement-factory timeouts and outer budget expiry also arrive as `TimeoutError`, so this wrapper needs explicit ownership detection even though the standard Codex socket factory normally translates its own timeout.

HTTP bridge session creation occurs before `response.create` is sent, so changing the selected account after a transient connection failure is safe for ordinary first turns and soft affinity. The current transport-exception branch only retries when the failed account was an explicit preference; an ordinary first selection fails immediately.

## Goals / Non-Goals

**Goals:**

- Report request-budget exhaustion only when the outer budget scope actually expires.
- Preserve nested socket timeout exceptions while budget remains.
- Let direct pre-first-event connection failures reach the existing account failover policy.
- Use another eligible account for safe pre-send bridge connection retries.
- Keep retry count, admission, affinity, and total elapsed time bounded.
- Preserve hard continuity constraints for requests tied to upstream account state.

**Non-Goals:**

- Changing configured timeout durations, routing scores, or the Caddy gateway.
- Retrying a request after `response.create` may have reached upstream.
- Hiding a sustained network or OpenAI outage by waiting indefinitely.

## Decisions

### Inspect the outer cancellation scope

Capture the cancellation scope returned by `anyio.fail_after()`. On `TimeoutError`, map to the proxy-budget error only when that scope reports its own cancellation; otherwise re-raise the nested timeout unchanged. This keeps one deadline source of truth without introducing a new exception hierarchy.

Alternative: compare elapsed time with the supplied timeout. Rejected because scheduler jitter and nested timeouts near the deadline make elapsed-time inference ambiguous.

### Normalize direct WebSocket handshake timeout as a connection failure

Translate the direct aiohttp WebSocket handshake timeout to a dedicated `aiohttp.ConnectionTimeoutError` subtype and emit `upstream_connect_timeout` instead of the false request-budget error. The retry layer classifies that first event as transient and selects another account before yielding it downstream. If no candidate remains, the final failure preserves the existing HTTP 200 streaming contract and exposes the accurate terminal SSE event.

Alternative: raise an HTTP 502 before the stream is committed. Rejected because the existing Responses endpoint contract returns transport failures as terminal SSE and integration clients depend on that behavior. Adding `upstream_request_timeout` to transient codes was also rejected because that code still represents a genuinely exhausted outer deadline.

### Retry only before upstream request submission

HTTP bridge session creation may exclude a transiently failing account and select another candidate because no `response.create` has been sent on a socket that failed to connect. Retry remains bounded by the existing account-attempt limit and outer request deadline.

An explicitly required preferred account is retried once on the same account and then surfaced as unavailable; it is not rebound to another account because `previous_response_id` or hard affinity may be account-scoped. A soft preference may fall back after its one same-account retry.

Alternative: retry all WebSocket failures globally. Rejected because post-send retries can duplicate model work and continuity-bound responses cannot safely move accounts.

### Keep connection failures account-neutral

A handshake timeout excludes the account only for the current request. It does not persist a rate-limit or quota penalty because the underlying fault may be a shared egress or upstream network event.

## Risks / Trade-offs

- **Correlated network outage can consume several 20-second attempts** -> Existing maximum-attempt and 480-second request budgets cap the delay; the final error reports a connection failure instead of a false budget exhaustion.
- **Soft affinity can move after a failed handshake** -> Movement occurs before request submission or downstream visibility, so there is no duplicate request; hard continuity remains fail-closed.
- **Timeout races near the outer deadline** -> The cancellation scope is authoritative, so a deadline-triggered cancellation remains classified as budget exhaustion.

## Migration Plan

Run focused unit and integration tests, then start a backend-only instance on port 3456 and exercise health, dashboard, and a low-cost Responses request. After validation, restart only port 2456 and monitor new request logs for false 20-second `upstream_request_timeout` records. Rollback is a code-only revert and backend restart.

## Open Questions

None.
