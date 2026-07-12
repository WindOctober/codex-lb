# Proxy Response-Create Runtime Context

## Purpose and Scope

HTTP bridge and downstream WebSocket requests share one preparation lifecycle: build a transport-aware request state, serialize the upstream `response.create` payload, apply canonical slimming and proxy size policy, then acquire a per-session gate followed by shared work admission. This capability gives that lifecycle one typed runtime owner outside the `ProxyService` facade.

It does not define public schemas, payload-slimming policy, maximum sizes, transport selection, account routing, continuity, or API-key reservation rules.

## Decisions and Constraints

- Preparation and admission live together because they create and mutate the same `_WebSocketRequestState` across both transports.
- The runtime remains separate from `_service/response_create.py`, which owns diagnostic size enforcement and filesystem dumps rather than orchestration.
- Pure metadata, fingerprint, service-tier, slimming, and gate-release helpers stay in their canonical modules.
- `WorkAdmissionController` remains owned by `ProxyService`; a structural protocol exposes only the capability the runtime consumes.
- Dynamic service hooks resolve maximum-size and enforcement globals at call time so existing threshold and dump monkeypatch seams remain compatible.
- Internal runtime modules must not import the service facade.

## Failure Modes

- An oversized request is slimmed under the existing policy and then rejected with the existing 413 contract if it remains too large.
- If shared work admission fails after the per-session gate is acquired, cleanup releases the gate and clears the associated request-state fields.
- Cancellation while waiting for shared admission follows the same `BaseException` cleanup path and propagates cancellation.
- Removing a legacy service re-export can break tests and local integrations even when inherited methods still work; architecture ratchets preserve the known response-create seams.

## Concrete Example

An HTTP bridge request with one input item and turn metadata is prepared through the inherited service method. The runtime assigns a unique internal request ID, retains the current request-log ID, merges turn metadata into `client_metadata`, records the input count and canonical fingerprint, emits compact JSON with `type=response.create`, enforces the current service thresholds, acquires the session gate, and only then waits for shared response-create admission.

## Operational Notes

Validate changes with focused metadata, state, fingerprint, slimming, threshold-replacement, overload, cancellation, HTTP bridge, and WebSocket tests. Runtime preflight uses an isolated backend and mock upstream and must not restart or drain the primary backend or alter Caddy.
