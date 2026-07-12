# Proxy API-Key Usage Runtime Context

## Purpose and Scope

Ordinary streaming, compact Responses, HTTP bridge, and downstream WebSocket requests share one repository-backed API-key usage lifecycle: reserve capacity before upstream work, then finalize complete usage or release the reservation on incomplete terminal paths. The shared runtime keeps those accounting invariants outside transport orchestration while preserving the methods exposed through `ProxyService`.

This capability does not define API-key authentication, policy refresh, quota values, pricing, persistence schema, routing, retries, or transport response behavior.

## Decisions and Constraints

- Reservation, release, compact settlement, and stream settlement live together because they form one quota-accounting lifecycle and depend only on `_repo_factory`.
- Compact and stream settlement remain separate: compact receives typed response usage directly, while stream settlement consumes `_StreamSettlement` and reports persistence success.
- Limit enforcement and persistence stay inside `CancelScope(shield=True)` to preserve quota correctness across request cancellation.
- Rate-limit and invalid-key repository errors retain their existing proxy error translations, including the reset timestamp for rate limits.
- Complete token usage finalizes a reservation; absent, incomplete, or unsuccessful usage releases it.
- Persistence failures remain warning-only for the proxied request, while stream settlement retains its boolean result.
- Internal runtime code must not import the service facade, and the facade must not regain local implementations of the four methods.

## Failure Modes

- Returning from cancellation before reservation release or settlement can leak reserved capacity.
- Finalizing incomplete usage can charge an incorrect amount; releasing a complete response can undercount usage.
- Losing the response/request service-tier fallback can attribute usage to the wrong pricing tier.
- Raising settlement persistence failures into a completed upstream response changes the public transport contract.
- Moving methods without retaining inherited names breaks transport protocols and tests that replace them on a service instance.

## Concrete Example

A compact request reserves API-key capacity for a requested model and tier. If the upstream response contains complete input and output token counts, the runtime finalizes that reservation with cached tokens and the effective response tier. If upstream work fails before a complete response exists, the same runtime releases the reservation. Either persistence operation is shielded from caller cancellation, and a persistence error is logged without replacing the upstream result.

## Operational Notes

Validate the lifecycle with direct reservation/error/release and compact/stream settlement tests, then exercise representative ordinary streaming, compact, HTTP bridge, and WebSocket callers. Runtime preflight uses isolated backend/mock ports and must not drain or restart the primary backend, modify Caddy, or retarget the gateway.
