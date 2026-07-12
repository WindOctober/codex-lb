# Proxy Upstream WebSocket Runtime Context

## Purpose and Scope

HTTP bridge session creation/reconnection and downstream WebSocket connection policy share one upstream socket-creation lifecycle: reject unsupported provider transports, decrypt the selected account token, derive provider-specific connection parameters, acquire local connection admission, call the replaceable socket factory within the remaining budget, and release admission on every terminal path. This capability gives that lifecycle one typed owner outside the `ProxyService` facade.

It does not define account selection, routing, refresh, 401 retry, failover, account-health mutation, egress selection, handshake headers, or upstream protocol parsing.

## Decisions and Constraints

- The runtime lives at `_service/upstream_websocket.py` rather than under the downstream-only WebSocket package because HTTP bridge is an equal consumer.
- A structural protocol exposes only encryption, work admission, inherited open behavior, and the replaceable socket-factory capability; the runtime has no repository, routing, or balancer access.
- Canonical provider helpers continue to derive account headers, base URLs, wire APIs, and WebSocket support.
- `ProxyService` retains one local static compatibility method so replacements of its module-level `connect_responses_websocket` binding remain visible at call time and legacy fakes may omit newer optional keywords.
- The core WebSocket client continues to own per-connection egress choice and real transport/handshake behavior. The shared runtime performs one factory call per admitted attempt and adds no replay.
- Internal runtime modules must not import the service facade.

## Failure Modes

- An API-key provider configured with the Codex wire API rejects WebSocket transport before token decryption, admission, or a socket attempt.
- Local connection admission may surface the existing overload error without invoking or penalizing an upstream account.
- Factory exceptions and cancellation propagate unchanged after the connection lease is released exactly once.
- Budget exhaustion cancels the in-flight creation path, releases its lease, and maps to the existing `upstream_unavailable` proxy-budget error.
- Removing or bypassing the service-level factory seam can silently break test and local factory injection even when the core client remains correct.

## Concrete Example

An HTTP bridge follow-up selects an OpenAI OAuth account and asks the inherited service method to open an upstream socket. The runtime verifies WebSocket support, decrypts the account token, derives the ChatGPT account header, acquires one local WebSocket-connect lease, and calls the current service factory with `base_url=None` and `wire_api=codex`. Whether the factory returns, raises, or is cancelled, the runtime releases the lease; selection and follow-up failover remain with HTTP bridge.

## Operational Notes

Validate changes with direct parameter, provider-gate, timeout, exception, cancellation, and lease tests plus focused downstream WebSocket, HTTP bridge reconnect, core transport, and egress tests. Runtime preflight uses isolated backend/mock ports and must not restart or drain the primary backend or alter Caddy.
