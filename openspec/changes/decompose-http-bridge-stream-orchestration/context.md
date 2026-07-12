## Boundary

The stream-orchestration slice owns one HTTP bridge request from prepared downstream payload through owner forwarding or local session acquisition, upstream submission, SSE delivery, recovery decisions, and terminal cleanup. Account selection, session creation, durable persistence, request submission, event relay, API-key accounting, and owner resolution remain typed service capabilities supplied by existing modules.

Pure stream policy owns bridge key construction, full-resend detection, request-stage classification, continuation recovery predicates, payload trimming helpers, and effective idle lifetime. General prompt-cache affinity remains in the affinity module, and request-shape logging remains in observability.

## Compatibility

`ProxyService` inherits the extracted orchestration method. Service-level wrappers remain only for tested settings injection or legacy import compatibility. Internal modules do not import or dynamically inspect `app.modules.proxy.service`.

## Failure Modes

- A forwarded request MUST retain its signed affinity key and owner routing behavior.
- A local continuation MUST preserve fail-closed owner lookup and same-account recovery behavior.
- Full-resend trimming and context-overflow rollover MUST keep the existing hard-affinity protections.
- Cancellation and terminal errors MUST release API-key reservations and detach the same request exactly once.

## Example

A Codex follow-up with `x-codex-turn-state` first resolves durable ownership. If the owner is local, orchestration reuses or creates the same bridge session, submits once, and streams existing keepalive and SSE frames. If the active owner is remote, it forwards once using the existing forwarding contract. The refactor changes only where this state machine is implemented.
