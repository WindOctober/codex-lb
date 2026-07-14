## Context

The local fork applies `proxy_request_budget_seconds` to every HTTP Responses and HTTP bridge turn. Production is configured for 480 seconds, and three `gpt-5.6-sol` max-reasoning turns were terminated at 480004-480011 ms after upstream response creation. Upstream codex-lb addressed this with separate 7200-second HTTP Responses and bridge budgets in commits `3993c9ce` and `ff029236`.

The public Responses route also nests the service stream inside a normalization generator and a prepend-first generator. Neither wrapper closes its child iterator. On Python 3.14, downstream cancellation can therefore schedule independent finalizers for related parent and child generators, producing an unretrieved `aclose(): asynchronous generator is already running` exception.

## Goals / Non-Goals

**Goals:**

- Preserve short general budgets while allowing legitimate long HTTP Responses turns.
- Apply one bridge long-turn budget consistently across local bridge receive and owner-forward paths.
- Keep stream-idle timeout as an independent finite stalled-upstream guard.
- Preserve one absolute hard deadline per bridge request across reconnect and replay.
- Isolate identified hard-deadline expiry to the expired request on shared bridges and retire transports whose expired request is still unidentifiable.
- Give each public async stream wrapper deterministic ownership of child iterator closure.
- Eliminate unretrieved async-generator finalizer failures on downstream cancellation.
- Recompute receive deadlines when an idle bridge gains pending work.
- Preserve admission and account-concurrency accounting across transparent replay.
- Surface the most recent HTTP bridge connection failure after mixed failover attempts.

**Non-Goals:**

- Retrying requests after upstream submission may have occurred.
- Making streams unbounded or removing idle timeout protection.
- Changing routing scores, account eligibility, or Caddy.
- Refactoring all proxy streaming surfaces.

## Decisions

### Port the upstream dedicated budgets

Add `http_responses_stream_request_budget_seconds` and `http_responses_session_bridge_request_budget_seconds`, both defaulting to 7200 seconds as upstream does. HTTP request transport uses the former in direct streaming; bridge receive deadlines and owner forwarding use the latter. Other transports retain `proxy_request_budget_seconds`, and reconnect-specific startup logic retains its current bounded behavior.

Alternative: increase `proxy_request_budget_seconds` globally. Rejected because it would also lengthen unrelated selection, refresh, compact, and failure paths.

### Retain a separate idle guard

The total long-turn budget remains finite, while `stream_idle_timeout_seconds` continues to terminate an upstream that emits no application activity. For this deployment, raise the idle timeout from 480 to the upstream long-turn default of 7200 seconds because 480-second silent max-reasoning turns are observed in production. This is an operator setting rather than a new endpoint contract.

Alternative: reset the total deadline after every event. Rejected because continuous metadata could keep a broken request alive indefinitely and would diverge from upstream semantics.

### Make wrappers own child closure

Both `_normalize_public_responses_stream` and `_prepend_first` close their child iterator in `finally` through one helper. The helper shields the awaited close from the active AnyIO cancellation scope. Closing is idempotent for async generators, and ownership keeps child references alive until the parent has settled instead of allowing independent garbage-collector finalizers to race.

Alternative: catch and ignore `RuntimeError("aclose(): ... already running")`. Rejected because it hides the race without establishing deterministic cleanup and can leak request reservations or bridge detach work.

### Preserve absolute request deadlines across bridge reconnect

Each bridge request receives one absolute long-turn deadline when it enters the bridge. A reconnect uses the earlier of that remaining hard deadline and a fresh reconnect-phase deadline, without mutating the request's hard deadline. Replay therefore cannot renew a nearly exhausted request for another full long-turn budget.

Owner-forward fallback and local recovery can reconstruct `_WebSocketRequestState` objects for the same logical request. These replacements inherit the original `started_at`, configured request budget, and absolute deadline before submission. The bridge receive path initializes a budget only when a state has no existing deadline.

Alternative: reset the long-turn deadline after reconnect. Rejected because repeated replay can extend a nominally finite request indefinitely and contradicts the hard-ceiling contract.

### Isolate shared-bridge request expiry

When the receive timer wakes for an ordinary request-budget deadline, the bridge removes and fails only request states whose own deadlines have elapsed when every expired state already has a response ID. Removal and queued-count adjustment occur atomically under the pending lock. Identified response IDs from expired states remain as session-local tombstones until an identified terminal event arrives, preventing a late terminal event from falling through to a newer request.

An expired request without a response ID is different: a later `response.created` cannot be attributed safely after that state is removed. The bridge therefore retires the shared upstream transport and fails its remaining pending requests instead of allowing an ambiguous late event to bind to newer work. Stream-idle timeout and response-created startup timeout retain the same bridge-retirement semantics because they also indicate an unhealthy or ambiguous shared transport.

Alternative: retire the bridge whenever its oldest request expires. Rejected because a fresh request may already have a valid upstream response on the same multiplexed socket.

### Protect shared transport before failover

Reconnect validates the original request deadline immediately before every upstream send and before cancelling the existing reader or closing the upstream socket. The final sibling check and the lifecycle mutation are serialized so a concurrent submission cannot enter between them. No-text failover is disabled while any sibling request remains pending on the bridge, whether or not that sibling already has a response ID.

Alternative: reconnect first and report timeout afterward. Rejected because a request that is already terminal must not disrupt unrelated work sharing the socket.

### Never replay ambiguous transport loss after send start

Starting a `response.create` send transfers the request into an ambiguous upstream-acceptance state. A successful transport send or HTTP response-start acknowledgement confirms only that the request may have reached upstream; neither proves rejection when the socket closes or the `response.created` deadline expires. Direct and shared WebSocket paths therefore emit a stable terminal and retire the affected transport without replay after send start.

A definitive provider error event may still use the existing bounded cross-account failover policy when its classified failure proves rejection before downstream visibility. This preserves safe rate-limit/transient failover without treating silent transport loss as permission to duplicate execution.

Alternative: retain response-created startup replay after `send_text()` returns. Rejected because account A may already be executing when account B receives the replay, causing duplicate work and usage.

### Wake idle readers when work arrives

Bridge and direct WebSocket readers keep one persistent upstream receive task and wait concurrently for either that task or a pending-state change notification. A submission signals the notification. This lets an otherwise idle reader recompute response-created, idle, and hard deadlines without cancelling and recreating the underlying WebSocket receive operation.

Alternative: poll by repeatedly cancelling `receive()`. Rejected because cancellation safety differs between WebSocket implementations and can corrupt the receive loop.

### Preserve replay accounting

A transparent pre-created or no-text replay is a new upstream submission attempt. Before replay sends, it reacquires response-create admission and ensures the account-concurrency lease belongs to the new upstream account. Any previous account lease is released exactly once, and all newly acquired resources are released on cancellation or failure. The request hard deadline is checked again after every awaited admission or lifecycle step and immediately before send.

Alternative: retain the original account lease across account failover. Rejected because it undercounts the new account while continuing to charge an account that no longer carries the request.

### Make cancellation cleanup ownership explicit

Startup, reconnect, expiry, and owner-forward paths transfer ownership of submit leases and child iterators only after the receiver has accepted them. If cancellation occurs before transfer, the creating scope closes or releases the resource under shielded cleanup. Expiry cleanup that follows atomic pending removal is also shielded so queue, admission, account, API-key, event-queue, and log resources cannot remain detached from both the bridge and the downstream request.

Background bridge close ownership covers reader and socket cleanup even when durable ownership release cooperatively accepts shutdown cancellation. Direct WebSocket replay owns the old-socket close before waiting on it and observes that close only for the smaller of a short close window and the request's remaining absolute deadline; unfinished close work remains tracked while failover proceeds.

The public route is validated through a real ASGI disconnect, not only direct generator closure. The response body iterator remains the sole owner of its active child iterator until shielded closure completes.

### Keep the latest connection failure

HTTP bridge session creation keeps one mutually exclusive last-failure record. A raw transport failure clears an older structured connection error, and a structured connection error clears an older raw transport message. If account candidates are exhausted, the error from the final attempted connection is surfaced.

### Authenticate cross-instance deadline and reservation ownership

Owner forwarding carries a signed wall-clock expiry because monotonic clocks cannot be compared across processes. The owner converts the remaining wall-clock duration to its local monotonic deadline before any durable lookup, capacity acquisition, connection, or send. The origin also bounds connection and response-header receipt by the same remaining duration.

The internal stream begins with an owner-accepted control event that is consumed by the forwarding layer and never exposed publicly. Before that acknowledgement, a definitive connection failure leaves ownership at the origin and the reservation is released before local fallback. After acknowledgement, or after an ambiguous transport failure that may have reached the owner, the origin does not issue a second upstream request; the owner-side request lifecycle owns settlement or release.

### Treat post-submit detach as a correlation boundary

Detaching an unsent request only releases local resources. Detaching a submitted request with a known response ID atomically records a tombstone before removing pending state. Detaching a submitted request without a response ID retires the transport because a later `response.created` cannot be correlated safely. While any identified tombstone remains unresolved, an anonymous event is also ambiguous and retires the transport instead of being assigned to a sibling.

## Risks / Trade-offs

- [Risk] Long failed streams consume capacity longer. -> The response-created startup deadline, bounded account attempts, 7200-second idle guard, and 7200-second hard ceiling remain active.
- [Risk] Shielded wrapper cleanup delays cancellation while child cleanup completes. -> The child owns finite service cleanup, and cancellation-scope tests require completion without independent finalizers.
- [Risk] Expiring one multiplexed request leaves the shared reader active. -> The expired state is removed under the pending lock and finalized through the existing request-failure helper before the receive loop continues.
- [Risk] A response tombstone remains if upstream never sends a terminal event. -> Tombstones are scoped to the bridge session and are released when that finite session closes.
- [Risk] New settings are omitted from a deployment. -> Typed defaults match upstream and require no migration.

## Migration Plan

Run focused unit/integration tests, validate a backend-only instance on port 3456, and send one low-cost Responses request. Set the local idle timeout to 7200 seconds, restart only backend port 2456, and monitor new request logs for 480-second budget failures and runtime logs for unretrieved async-generator errors. Rollback restores the previous settings fields and wrapper behavior, then restarts only 2456.

## Open Questions

None.
